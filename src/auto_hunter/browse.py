"""Read-only local web viewer over the collected data (`auto-hunter browse`).

Pages:
- /               listings table with filters and sorting; shortlist order by default
- /listing/<id>   one listing: photos, specs, assessment, price history, original text
- /overview       asking-price spread, recent changes, health of collection runs

Standard library only: server-rendered HTML with inline CSS and SVG. Each request opens
its own read-only connection, so a `collect` running meanwhile is never affected.
The UI is English; listing text (titles, descriptions, sellers) stays as published.
"""

from __future__ import annotations

import json
import logging
import traceback
import unicodedata
import webbrowser
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from auto_hunter.config import PRIORITIES, Config
from auto_hunter.db import Database
from auto_hunter.report import market_groups, rank_key, recent_changes

log = logging.getLogger(__name__)

PAGE_SIZE = 100
STATUSES = ("active", "unmatched", "removed", "all")
# Query parameters the listings page understands; anything else is ignored.
FILTERS = ("status", "q", "source", "profile", "model", "gen", "engine", "tier", "conf",
           "flag", "price_min", "price_max", "km_max", "year_min", "year_max", "dropped",
           "dup", "sort", "dir")
# Sortable columns: (value of a listing entry, default direction).
SORTS = {
    "price": (lambda e: e["price_czk"], "asc"),
    "year": (lambda e: e["year"], "desc"),
    "km": (lambda e: e["mileage_km"], "asc"),
    "power": (lambda e: e["power_kw"], "desc"),
    "drop": (lambda e: e["drop_pct"], "desc"),
    "first_seen": (lambda e: e["first_seen_at"], "desc"),
    "last_seen": (lambda e: e["last_seen_at"], "desc"),
}
# Listings stored before they were ever assessed (shouldn't happen, but don't crash).
_NO_ASSESSMENT = {"generation": None, "generation_candidates": [], "engine": None,
                  "engine_candidates": [], "engine_status": None, "price_tier": None,
                  "confidence": "low", "flags": []}


# --- formatting ---------------------------------------------------------------------

def _e(value) -> str:
    return escape("" if value is None else str(value))


# Seznam's CDN (sauto) answers 401 to a bare image URL and serves it only through one of
# a whitelist of `fl=` transforms; these are the ones sauto.cz itself uses.
_SDN_TRANSFORMS = {
    "thumb": "res,320,240,1|jpg,80,,1",
    "full": "exf|crr,1.33333,2|res,1024,768,1|wrm,/watermark/sauto.png,10,10|jpg,80,,1",
}


def _img(url: str, size: str) -> str:
    """Image URL the browser can load; `size` is "thumb" or "full"."""
    if ".sdn.cz/" in url and "?" not in url:
        return f"{url}?fl={_SDN_TRANSFORMS[size]}"
    return url


def _price(p: int | None) -> str:
    return f"{p:,} Kč".replace(",", " ") if p is not None else "?"


def _km(km: int | None) -> str:
    return f"{km:,} km".replace(",", " ") if km is not None else "?"


def _date(ts: str | None) -> str:
    return ts[:10] if ts else "–"


def _when(ts: str | None) -> str:
    return ts[:16].replace("T", " ") if ts else "–"


def _yes_no(v: int | None) -> str:
    return "–" if v is None else ("yes" if v else "no")


def _fold(text: str) -> str:
    """Case- and accent-insensitive form, so "skoda" finds "Škoda"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


def _int(value: str | None) -> int | None:
    try:
        return int(value.replace(" ", "")) if value else None
    except ValueError:
        return None


def _gen_label(a: dict, config: Config) -> str:
    gens = config.catalog.generations
    if a["generation"]:
        return gens[a["generation"]].label if a["generation"] in gens else a["generation"]
    cands = [gens[g].label if g in gens else g for g in a["generation_candidates"]]
    return " / ".join(cands) + " ?" if cands else "?"


def _engine_label(a: dict, config: Config) -> str:
    engines = config.catalog.engines
    if a["engine"]:
        return engines[a["engine"]].label if a["engine"] in engines else a["engine"]
    cands = [engines[e].label if e in engines else e for e in a["engine_candidates"]]
    return " / ".join(cands) + " ?" if cands else "?"


def _badge(text: str, kind: str = "", title: str = "") -> str:
    t = f' title="{_e(title)}"' if title else ""
    return f'<span class="badge {kind}"{t}>{_e(text)}</span>'


def _status_badge(status: str) -> str:
    return _badge(status, {"active": "good", "removed": "muted"}.get(status, "warn"))


def _href(params: dict, **changes) -> str:
    merged = {k: v for k, v in {**params, **changes}.items() if v not in (None, "")}
    return "/?" + urlencode(merged) if merged else "/"


# --- listings page ------------------------------------------------------------------

def _load_entries(db: Database, config: Config, status: str) -> list[dict]:
    sql = """SELECT l.id, l.source, l.source_id, l.url, l.title, l.price_czk, l.mileage_km,
                    l.year, l.fuel, l.gearbox, l.power_kw, l.seller_type, l.seller_name,
                    l.district, l.municipality, l.region, l.status, l.model_key,
                    l.first_seen_at, l.last_seen_at, l.image_urls, l.description,
                    a.data AS assessment,
                    (SELECT GROUP_CONCAT(lp.profile) FROM listing_profiles lp
                     WHERE lp.listing_id = l.id) AS profiles,
                    (SELECT s.price_czk FROM listing_snapshots s
                     WHERE s.listing_id = l.id AND s.price_czk IS NOT NULL
                     ORDER BY s.observed_at, s.id LIMIT 1) AS first_price
             FROM listings l LEFT JOIN listing_assessments a ON a.listing_id = l.id"""
    params: list = []
    if status != "all":
        sql += " WHERE l.status = ?"
        params.append(status)
    priority_of = {p.name: PRIORITIES.index(p.priority) for p in config.profiles}
    entries = []
    for r in db.conn.execute(sql, params):
        e = dict(r)
        a = json.loads(r["assessment"]) if r["assessment"] else _NO_ASSESSMENT
        e["a"] = a
        e["profiles"] = r["profiles"].split(",") if r["profiles"] else []
        e["gen_label"] = _gen_label(a, config)
        e["engine_label"] = _engine_label(a, config)
        e["images"] = json.loads(r["image_urls"])
        first, now = r["first_price"], r["price_czk"]
        e["drop_pct"] = (round(100 * (first - now) / first, 1)
                         if first and now is not None and now < first else None)
        e["rank"] = rank_key(a, e["profiles"], now, priority_of)
        entries.append(e)
    return entries


def _matches(e: dict, f: dict) -> bool:
    a = e["a"]
    checks = [
        ("source", lambda v: e["source"] == v),
        ("profile", lambda v: v in e["profiles"]),
        ("model", lambda v: e["model_key"] == v),
        ("gen", lambda v: e["gen_label"] == v),
        ("engine", lambda v: e["engine_label"] == v),
        ("tier", lambda v: (a["price_tier"] or "-") == v),
        ("conf", lambda v: a["confidence"] == v),
        ("flag", lambda v: any(fl["code"] == v for fl in a["flags"])),
        ("dropped", lambda v: e["drop_pct"] is not None),
        ("dup", lambda v: any(fl["code"] == "also_listed" for fl in a["flags"])),
    ]
    if not all(check(f[k]) for k, check in checks if f.get(k)):
        return False
    bounds = [("price_min", "price_czk", 1), ("price_max", "price_czk", -1),
              ("km_max", "mileage_km", -1), ("year_min", "year", 1), ("year_max", "year", -1)]
    for param, column, sign in bounds:
        limit = _int(f.get(param))
        if limit is not None and (e[column] is None or sign * (e[column] - limit) < 0):
            return False
    if f.get("q"):
        haystack = _fold(f"{e['title']} {e['description'] or ''} {e['source_id']} "
                         f"{e['seller_name'] or ''} {e['municipality'] or ''}")
        if not all(word in haystack for word in _fold(f["q"]).split()):
            return False
    return True


def _sorted(entries: list[dict], sort: str, direction: str) -> list[dict]:
    if sort not in SORTS:
        return sorted(entries, key=lambda e: e["rank"])
    value, default = SORTS[sort]
    direction = direction or default
    known = [e for e in entries if value(e) is not None]
    unknown = [e for e in entries if value(e) is None]
    known.sort(key=value, reverse=direction == "desc")
    return known + unknown  # missing values always last, whatever the direction


def _select(name: str, label: str, options, current: str | None, any_label: str = "any") -> str:
    """A filter dropdown; options are values or (value, text) pairs."""
    opts = [f'<option value="">{_e(any_label)}</option>'] if any_label else []
    for o in options:
        value, text = o if isinstance(o, tuple) else (o, o)
        sel = " selected" if str(value) == (current or "") else ""
        opts.append(f'<option value="{_e(value)}"{sel}>{_e(text)}</option>')
    return (f'<label>{_e(label)}<select name="{name}" onchange="this.form.submit()">'
            f'{"".join(opts)}</select></label>')


def _number(name: str, label: str, current: str | None, placeholder: str) -> str:
    return (f'<label>{_e(label)}<input type="text" inputmode="numeric" name="{name}" '
            f'value="{_e(current)}" placeholder="{_e(placeholder)}" size="8"></label>')


def _checkbox(name: str, label: str, current: str | None) -> str:
    checked = " checked" if current else ""
    return (f'<label class="check"><input type="checkbox" name="{name}" value="1"{checked} '
            f'onchange="this.form.submit()">{_e(label)}</label>')


def _filter_form(f: dict, pool: list[dict]) -> str:
    def distinct(values) -> list:
        return sorted({v for v in values if v})

    flag_codes = distinct(fl["code"] for e in pool for fl in e["a"]["flags"])
    hidden = "".join(f'<input type="hidden" name="{k}" value="{_e(f[k])}">'
                     for k in ("sort", "dir") if f.get(k))
    return f"""
<form class="filters" method="get" action="/">
  {hidden}
  <label class="grow">Search<input type="search" name="q" value="{_e(f.get('q'))}"
    placeholder="title, description, seller, place or ad id"></label>
  {_select("status", "Status", [(s, s) for s in STATUSES], f.get("status") or "active", any_label="")}
  {_select("source", "Source", distinct(e["source"] for e in pool), f.get("source"))}
  {_select("profile", "Profile", distinct(p for e in pool for p in e["profiles"]), f.get("profile"))}
  {_select("model", "Model", distinct(e["model_key"] for e in pool), f.get("model"))}
  {_select("gen", "Generation", distinct(e["gen_label"] for e in pool), f.get("gen"))}
  {_select("engine", "Engine", distinct(e["engine_label"] for e in pool), f.get("engine"))}
  {_select("tier", "Price tier", distinct((e["a"]["price_tier"] or "-") for e in pool), f.get("tier"))}
  {_select("conf", "Confidence", ["high", "medium", "low"], f.get("conf"))}
  {_select("flag", "Has flag", flag_codes, f.get("flag"))}
  {_number("price_min", "Price from", f.get("price_min"), "Kč")}
  {_number("price_max", "Price to", f.get("price_max"), "Kč")}
  {_number("km_max", "Km up to", f.get("km_max"), "km")}
  {_number("year_min", "Year from", f.get("year_min"), "")}
  {_number("year_max", "Year to", f.get("year_max"), "")}
  {_checkbox("dropped", "Price dropped", f.get("dropped"))}
  {_checkbox("dup", "Also listed elsewhere", f.get("dup"))}
  <div class="actions"><button type="submit">Apply</button><a href="/">Reset</a></div>
</form>"""


def _header(f: dict, sort: str, label: str) -> str:
    if f.get("sort") == sort:
        current = f.get("dir") or SORTS[sort][1]
        direction = "asc" if current == "desc" else "desc"
        arrow = " ▲" if current == "asc" else " ▼"
    else:
        direction, arrow = SORTS[sort][1], ""
    return f'<a href="{_e(_href(f, sort=sort, dir=direction, page=None))}">{_e(label)}{arrow}</a>'


def _row(e: dict) -> str:
    a = e["a"]
    thumb = (f'<img src="{_e(_img(e["images"][0], "thumb"))}" alt="" loading="lazy" '
             'referrerpolicy="no-referrer">'
             if e["images"] else '<div class="noimg">no photo</div>')
    price = _price(e["price_czk"])
    if e["drop_pct"] is not None:
        price += (f'<div class="drop" title="first seen at {_e(_price(e["first_price"]))}">'
                  f'▼ {e["drop_pct"]:g} % (was {_e(_price(e["first_price"]))})</div>')
    flags = []
    for fl in a["flags"]:
        if fl["severity"] == "caution":
            flags.append(_badge("⚠ " + fl["code"].replace("_", " "), "warn", fl["message"]))
        elif fl["code"] == "also_listed":
            flags.append(_badge("↔ also listed", "info", fl["message"]))
    infos = [fl["message"] for fl in a["flags"]
             if fl["severity"] != "caution" and fl["code"] != "also_listed"]
    if infos:
        flags.append(_badge(f"{len(infos)} info", "muted", "\n".join(infos)))
    place = e["municipality"] or e["district"] or e["region"] or "?"
    status = "" if e["status"] == "active" else " " + _status_badge(e["status"])
    return f"""<tr>
  <td class="thumb"><a href="/listing/{e['id']}">{thumb}</a></td>
  <td class="car"><a href="/listing/{e['id']}">{_e(e['title'])}</a>{status}
    <div class="sub">{_e(e['gen_label'])} · {_e(e['engine_label'])}</div>
    <div class="sub">{_e(', '.join(e['profiles']) or 'no profile')}</div></td>
  <td class="num">{price}</td>
  <td class="num">{_e(e['year'] or '?')}</td>
  <td class="num">{_km(e['mileage_km'])}</td>
  <td class="num">{_e(e['power_kw'] or '?')}</td>
  <td>{_e(e['gearbox'] or '?')}</td>
  <td>{_e(a['price_tier'] or '–')}</td>
  <td>{_badge(a['confidence'], {'high': 'good', 'medium': 'info'}.get(a['confidence'], 'warn'))}</td>
  <td class="flags">{''.join(flags)}</td>
  <td>{_e(place)}<div class="sub">{_e(e['seller_type'] or '')}</div></td>
  <td><a href="{_e(e['url'])}" target="_blank" rel="noreferrer">{_e(e['source'])} ↗</a></td>
  <td class="num">{_date(e['first_seen_at'])}</td>
</tr>"""


def listings_page(db: Database, config: Config, f: dict) -> str:
    status = f.get("status") if f.get("status") in STATUSES else "active"
    pool = _load_entries(db, config, status)
    shown = _sorted([e for e in pool if _matches(e, f)], f.get("sort", ""), f.get("dir", ""))
    pages = max(1, -(-len(shown) // PAGE_SIZE))
    page = min(max(_int(f.get("page")) or 1, 1), pages)
    rows = "".join(_row(e) for e in shown[(page - 1) * PAGE_SIZE:page * PAGE_SIZE])
    order = ("shortlist order (profile priority, confidence, cautions, price)"
             if f.get("sort") not in SORTS else "")
    nav = []
    if page > 1:
        nav.append(f'<a href="{_e(_href(f, page=page - 1))}">← previous</a>')
    if page < pages:
        nav.append(f'<a href="{_e(_href(f, page=page + 1))}">next →</a>')
    first = (page - 1) * PAGE_SIZE + 1 if shown else 0
    summary = (f"{len(shown)} of {len(pool)}{f' {status}' if status != 'all' else ''} listings"
               f"{f' · showing {first}–{min(page * PAGE_SIZE, len(shown))}' if pages > 1 else ''}"
               f"{f' · {order}' if order else ''}")
    table = f"""
<div class="tablewrap"><table class="listings">
<thead><tr><th></th><th>Car</th><th>{_header(f, 'price', 'Price')}</th>
<th>{_header(f, 'year', 'Year')}</th><th>{_header(f, 'km', 'Mileage')}</th>
<th>{_header(f, 'power', 'kW')}</th><th>Gearbox</th><th>Tier</th><th>Conf.</th><th>Flags</th>
<th>Location</th><th>Source</th><th>{_header(f, 'first_seen', 'First seen')}</th></tr></thead>
<tbody>{rows}</tbody></table></div>""" if shown else '<p class="empty">No listings match these filters.</p>'
    body = (f'{_filter_form(f, pool)}<p class="summary">{_e(summary)}'
            f'{" · " + _drop_sort_link(f) if shown else ""}</p>{table}'
            f'<p class="pager">{" · ".join(nav)}</p>')
    return _layout("Listings", body, "listings")


def _drop_sort_link(f: dict) -> str:
    return f'<a href="{_e(_href(f, sort="drop", dir="desc", page=None))}">biggest price drops first</a>'


# --- listing detail page ------------------------------------------------------------

def _price_chart(snaps: list, end: str) -> str:
    """Step chart of asking price over time, extended to `end` (last seen or removal)."""
    points = [(datetime.fromisoformat(s["observed_at"]), s["price_czk"], s["event"])
              for s in snaps if s["price_czk"] is not None]
    if not points:
        return '<p class="empty">No price recorded.</p>'
    end_t = max(datetime.fromisoformat(end), points[-1][0])
    w, h, left, right, top, bottom = 720, 200, 90, 16, 14, 28
    t0, t1 = points[0][0].timestamp(), end_t.timestamp()
    prices = [p for _, p, _ in points]
    lo, hi = min(prices), max(prices)
    pad = max((hi - lo) * 0.15, hi * 0.02, 1)
    lo, hi = lo - pad, hi + pad

    def x(t: datetime) -> float:
        return left + (w - left - right) * ((t.timestamp() - t0) / (t1 - t0) if t1 > t0 else 0)

    def y(p: float) -> float:
        return top + (h - top - bottom) * (1 - (p - lo) / (hi - lo))

    path = f"M{x(points[0][0]):.1f},{y(points[0][1]):.1f}"
    for t, p, _ in points[1:]:
        path += f" H{x(t):.1f} V{y(p):.1f}"
    path += f" H{x(end_t):.1f}"
    dots = "".join(
        f'<circle cx="{x(t):.1f}" cy="{y(p):.1f}" r="4"><title>{_e(_when(t.isoformat()))}: '
        f'{_e(_price(p))} ({_e(ev)})</title></circle>' for t, p, ev in points)
    labels = sorted({min(prices), max(prices)})
    y_axis = "".join(
        f'<text x="{left - 8}" y="{y(p) + 4:.1f}" text-anchor="end">{_e(_price(p))}</text>'
        f'<line x1="{left}" x2="{w - right}" y1="{y(p):.1f}" y2="{y(p):.1f}" class="grid"/>'
        for p in labels)
    x_axis = (f'<text x="{left}" y="{h - 8}">{_date(points[0][0].isoformat())}</text>'
              f'<text x="{w - right}" y="{h - 8}" text-anchor="end">{_date(end_t.isoformat())}</text>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Price history">'
            f'{y_axis}{x_axis}<path d="{path}"/>{dots}</svg>')


def _spec_table(rows: list[tuple[str, str]]) -> str:
    cells = "".join(f"<tr><th>{_e(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return f'<table class="specs">{cells}</table>'


def listing_page(db: Database, config: Config, listing_id: int) -> str | None:
    l = db.conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if l is None:
        return None
    row = db.conn.execute("SELECT data FROM listing_assessments WHERE listing_id = ?",
                          (listing_id,)).fetchone()
    a = json.loads(row["data"]) if row else _NO_ASSESSMENT
    profiles = db.conn.execute(
        "SELECT profile, first_seen_at, last_seen_at FROM listing_profiles WHERE listing_id = ? "
        "ORDER BY profile", (listing_id,)).fetchall()
    snaps = db.conn.execute(
        "SELECT * FROM listing_snapshots WHERE listing_id = ? ORDER BY observed_at, id",
        (listing_id,)).fetchall()
    images = json.loads(l["image_urls"])

    gallery = "".join(
        f'<a href="{_e(_img(u, "full"))}" target="_blank" rel="noreferrer">'
        f'<img src="{_e(_img(u, "full"))}" alt="" '
        f'loading="lazy" referrerpolicy="no-referrer"></a>' for u in images)
    hp = f" ({round(l['power_kw'] * 1.341)} hp)" if l["power_kw"] else ""
    place = ", ".join(v for v in (l["municipality"], l["district"], l["region"]) if v)
    specs = _spec_table([
        ("Price", _e(_price(l["price_czk"]))),
        ("Year", _e(l["year"] or "?")),
        ("Manufactured", _e(l["manufacturing_date"] or "–")),
        ("Mileage", _e(_km(l["mileage_km"]))),
        ("Fuel", _e(l["fuel"] or "–")),
        ("Gearbox", _e(l["gearbox"] or "–")),
        ("Power", _e(f"{l['power_kw']} kW{hp}" if l["power_kw"] else "–")),
        ("Engine", _e(f"{l['engine_cc']} cm³" if l["engine_cc"] else "–")),
        ("Body", _e(l["body"] or "–")),
        ("Variant", _e(l["variant"] or "–")),
        ("VIN", f'<code>{_e(l["vin"])}</code>' if l["vin"] else "–"),
        ("Service book", _yes_no(l["service_book"])),
        ("First owner", _yes_no(l["first_owner"])),
        ("Accident damage", _yes_no(l["damaged"])),
        ("Seller", _e(" · ".join(v for v in (l["seller_type"], l["seller_name"]) if v) or "–")),
        ("Location", _e(place or "–")),
    ])

    engines = config.catalog.engines
    engine_note = ""
    if a["engine"] in engines and engines[a["engine"]].notes:
        engine_note = f'<div class="sub">{_e(engines[a["engine"]].notes)}</div>'
    tier_note = next((t.note for t in config.evaluation.price_tiers
                      if t.label == a["price_tier"] and t.note), "")
    flags = "".join(
        f'<li class="{_e(fl["severity"])}">{"⚠ " if fl["severity"] == "caution" else ""}'
        f'{_e(fl["message"])} <span class="code">{_e(fl["code"])}</span></li>'
        for fl in a["flags"]) or "<li>none</li>"
    assessment = _spec_table([
        ("Generation", _e(_gen_label(a, config))),
        ("Engine", _e(_engine_label(a, config))
         + (" " + _badge(a["engine_status"], "warn" if a["engine_status"] in ("avoid", "caution")
                         else "muted") if a["engine_status"] else "") + engine_note),
        ("Price tier", _e(a["price_tier"] or "–") + (f'<div class="sub">{_e(tier_note)}</div>'
                                                      if tier_note else "")),
        ("Confidence", _e(a["confidence"])),
        ("Profiles", "<br>".join(f'{_e(p["profile"])} <span class="sub">since '
                                 f'{_date(p["first_seen_at"])}</span>' for p in profiles) or "–"),
    ])

    dups = db.same_vehicle(listing_id, l["vin"])
    dup_items = []
    for d in dups:
        other = db.conn.execute("SELECT id FROM listings WHERE url = ?", (d["url"],)).fetchone()
        how = "same VIN" if d["match"] == "vin" else "same model, year and km"
        dup_items.append(f'<li><a href="/listing/{other["id"]}">{_e(d["source"])}</a> for '
                         f'{_e(_price(d["price_czk"]))} ({how}) · '
                         f'<a href="{_e(d["url"])}" target="_blank" rel="noreferrer">original ↗</a></li>')
    dup_html = (f'<section><h2>Same car elsewhere</h2><ul>{"".join(dup_items)}</ul></section>'
                if dup_items else "")

    events = "".join(
        f"<tr><td>{_e(_when(s['observed_at']))}</td><td>{_e(s['event'])}</td>"
        f"<td class='num'>{_e(_price(s['price_czk']))}</td>"
        f"<td class='num'>{_e(_km(s['mileage_km']))}</td><td>{_e(s['status'])}</td></tr>"
        for s in reversed(snaps))
    tracking = _spec_table([
        ("Source", f'{_e(l["source"])} · <code>{_e(l["source_id"])}</code>'),
        ("Status", _status_badge(l["status"])),
        ("Listed on source", _e(_when(l["source_created_at"]))),
        ("First seen", _e(_when(l["first_seen_at"]))),
        ("Last seen", _e(_when(l["last_seen_at"]))),
        ("Removed", _e(_when(l["removed_at"]))),
        ("Details fetched", _e(_when(l["details_fetched_at"]))),
    ])
    try:
        raw = json.dumps(json.loads(l["raw_json"]), ensure_ascii=False, indent=2)
    except ValueError:
        raw = l["raw_json"]
    description = (f'<div class="desc">{_e(l["description"])}</div>' if l["description"]
                   else '<p class="empty">No description (details not fetched yet, or none given).</p>')

    body = f"""
<p class="back"><a href="javascript:history.back()">← back</a></p>
<div class="head">
  <div><h1>{_e(l['title'])}</h1>
    <div class="sub">{_e(_gen_label(a, config))} · {_e(_engine_label(a, config))} · {_status_badge(l['status'])}</div></div>
  <div class="headprice">{_e(_price(l['price_czk']))}
    <a class="button" href="{_e(l['url'])}" target="_blank" rel="noreferrer">Open original ad ↗</a></div>
</div>
{f'<div class="gallery">{gallery}</div>' if gallery else ''}
<div class="cols">
  <section><h2>Specs</h2>{specs}</section>
  <section><h2>Assessment</h2>{assessment}<h3>Flags</h3><ul class="flaglist">{flags}</ul></section>
</div>
{dup_html}
<section><h2>Price history</h2>{_price_chart(snaps, l['removed_at'] or l['last_seen_at'])}
  <table class="events"><thead><tr><th>When</th><th>Event</th><th>Price</th><th>Mileage</th>
  <th>Status</th></tr></thead><tbody>{events}</tbody></table></section>
<section><h2>Description <span class="sub">(original text)</span></h2>{description}</section>
<div class="cols">
  <section><h2>Tracking</h2>{tracking}</section>
  <section><h2>Source data</h2><details><summary>Raw payload as received</summary>
    <pre class="raw">{_e(raw)}</pre></details></section>
</div>"""
    return _layout(l["title"], body, "")


# --- overview page ------------------------------------------------------------------

def overview_page(db: Database, config: Config, f: dict) -> str:
    days = _int(f.get("days")) or 7
    groups = market_groups(db, config)
    market = "".join(
        f"<tr><td>{_e(g['generation'])}</td><td>{_e(g['engine'])}</td>"
        f"<td class='num'>{g['n']}</td><td class='num'>{_e(_price(g['p25']))}</td>"
        f"<td class='num'><b>{_e(_price(g['median']))}</b></td>"
        f"<td class='num'>{_e(_price(g['p75']))}</td><td class='num'>{_e(g['median_year'] or '?')}</td>"
        f"<td class='num'>{_e(_km(g['median_km']))}</td></tr>" for g in groups)
    market_html = (f"""<div class="tablewrap"><table><thead><tr><th>Generation</th><th>Engine</th>
<th>n</th><th>25 %</th><th>Median</th><th>75 %</th><th>Median year</th><th>Median km</th></tr></thead>
<tbody>{market}</tbody></table></div>
<p class="sub">Asking prices of active listings; actual selling prices are typically lower.</p>"""
                   if groups else '<p class="empty">No active listings yet. Run <code>auto-hunter collect</code> first.</p>')

    changes = recent_changes(db, days)
    by_event: dict[str, list] = {}
    for c in changes:
        by_event.setdefault(c["event"], []).append(c)
    change_html = ""
    for event in ("new", "changed", "reactivated", "removed"):
        items = by_event.get(event, [])
        if not items:
            continue
        lis = []
        for c in items:
            price = _price(c["price_czk"])
            if event == "changed" and c["prev_price"] not in (None, c["price_czk"]):
                price = f"{_price(c['prev_price'])} → {price}"
            lis.append(f"<li><span class='num'>{_e(price)}</span> · {_e(c['year'] or '?')} · "
                       f"{_e(_km(c['mileage_km']))} · <a href='/listing/{c['listing_id']}'>"
                       f"{_e(c['title'])}</a> <span class='sub'>{_e(_when(c['observed_at']))}</span></li>")
        change_html += f"<h3>{event.capitalize()} ({len(items)})</h3><ul class='changes'>{''.join(lis)}</ul>"
    day_links = " · ".join(
        f'<b>{d} days</b>' if d == days else f'<a href="/overview?days={d}">{d} days</a>'
        for d in (1, 7, 30))

    runs = db.conn.execute(
        "SELECT * FROM collection_runs ORDER BY id DESC LIMIT 30").fetchall()
    run_rows = []
    for r in runs:
        errors = json.loads(r["errors"])
        err = (f"<details><summary>{len(errors)} error(s)</summary><ul>"
               + "".join(f"<li>{_e(x)}</li>" for x in errors) + "</ul></details>") if errors else "–"
        kind = {"ok": "good", "partial": "warn", "failed": "bad"}.get(r["status"], "muted")
        run_rows.append(
            f"<tr><td>{_e(_when(r['started_at']))}</td><td>{_e(r['source'] or '?')}</td>"
            f"<td>{_badge(r['status'], kind)}</td><td class='num'>{r['seen']}</td>"
            f"<td class='num'>{r['new']}</td><td class='num'>{r['changed']}</td>"
            f"<td class='num'>{r['removed']}</td><td>{err}</td></tr>")
    runs_html = (f"""<div class="tablewrap"><table><thead><tr><th>Started</th><th>Source</th>
<th>Status</th><th>Seen</th><th>New</th><th>Changed</th><th>Removed</th><th>Errors</th></tr></thead>
<tbody>{''.join(run_rows)}</tbody></table></div>""" if run_rows
                 else '<p class="empty">No collection runs yet.</p>')

    body = f"""
<section><h2>Market</h2>{market_html}</section>
<section><h2>Recent changes <span class="sub">{day_links}</span></h2>
  {change_html or f'<p class="empty">No changes in the last {days} day(s).</p>'}</section>
<section><h2>Collection runs</h2>{runs_html}</section>"""
    return _layout("Overview", body, "overview")


# --- layout and server --------------------------------------------------------------

CSS = """
:root { --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#677084; --line:#e2e5ea;
  --accent:#2563eb; --good:#15803d; --good-bg:#dcfce7; --warn:#a16207; --warn-bg:#fef3c7;
  --bad:#b91c1c; --bad-bg:#fee2e2; --info:#1d4ed8; --info-bg:#dbeafe; --muted-bg:#eef0f3; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151b; --panel:#1b1f27; --text:#e6e8ec;
  --muted:#99a1b2; --line:#2c323d; --accent:#7aa7ff; --good:#6ee7a0; --good-bg:#14361f;
  --warn:#fbd36b; --warn-bg:#3b2f0d; --bad:#fca5a5; --bad-bg:#421818; --info:#a5c3ff;
  --info-bg:#1c2b4d; --muted-bg:#262b35; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
nav { display:flex; gap:20px; align-items:center; padding:10px 20px; background:var(--panel);
  border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
nav .brand { font-weight:700; color:var(--text); } nav a.on { font-weight:600; color:var(--text); }
main { padding:16px 20px 40px; max-width:1600px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:0 0 10px; }
h3 { font-size:14px; margin:14px 0 6px; }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin-bottom:16px; min-width:0; }
.sub { color:var(--muted); font-size:12px; font-weight:normal; }
.num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.empty { color:var(--muted); font-style:italic; }
.badge { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
  margin:1px 3px 1px 0; white-space:nowrap; background:var(--muted-bg); color:var(--muted); }
.badge.good { background:var(--good-bg); color:var(--good); }
.badge.warn { background:var(--warn-bg); color:var(--warn); }
.badge.bad { background:var(--bad-bg); color:var(--bad); }
.badge.info { background:var(--info-bg); color:var(--info); }
.badge[title] { cursor:help; }
.filters { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:flex-end;
  background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
.filters label { display:flex; flex-direction:column; font-size:11px; color:var(--muted); gap:2px; }
.filters label.grow { flex:1 1 260px; }
.filters label.check { flex-direction:row; align-items:center; gap:5px; font-size:13px;
  color:var(--text); padding-bottom:5px; }
input, select, button { font:inherit; color:var(--text); background:var(--bg);
  border:1px solid var(--line); border-radius:5px; padding:4px 6px; }
select { max-width:220px; }
button, .button { background:var(--accent); color:#fff; border:0; padding:5px 14px;
  border-radius:5px; cursor:pointer; display:inline-block; font-size:13px; }
.actions { display:flex; gap:12px; align-items:center; padding-bottom:2px; }
.summary { color:var(--muted); margin:10px 2px; }
.tablewrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { padding:6px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { font-size:12px; color:var(--muted); font-weight:600; white-space:nowrap; }
table.listings { background:var(--panel); border:1px solid var(--line); }
table.listings tbody tr:hover { background:var(--bg); }
td.thumb { width:112px; } td.thumb img, .noimg { width:104px; height:70px; object-fit:cover;
  border-radius:4px; display:block; background:var(--muted-bg); }
.noimg { display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:11px; }
td.car { min-width:240px; } td.car > a { font-weight:600; }
td.flags { max-width:260px; }
.drop { color:var(--good); font-size:11px; }
.pager { text-align:center; }
.back { margin:0 0 8px; }
.head { display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:12px; }
.headprice { font-size:22px; font-weight:700; display:flex; flex-direction:column;
  align-items:flex-end; gap:6px; }
.gallery { display:flex; gap:8px; overflow-x:auto; margin-bottom:16px; padding-bottom:6px; }
.gallery img { height:220px; border-radius:6px; display:block; }
.cols { display:grid; grid-template-columns:repeat(auto-fit, minmax(380px, 1fr)); gap:0 16px; }
table.specs th { width:150px; }
.flaglist { padding-left:18px; margin:0; } .flaglist li { margin:3px 0; }
.flaglist li.caution { color:var(--warn); }
.code { font-family:ui-monospace, Consolas, monospace; font-size:11px; color:var(--muted); }
.desc { white-space:pre-wrap; max-width:90ch; }
.chart { width:100%; max-width:760px; display:block; margin-bottom:10px; }
.chart path { fill:none; stroke:var(--accent); stroke-width:2.5; }
.chart circle { fill:var(--panel); stroke:var(--accent); stroke-width:2; }
.chart text { fill:var(--muted); font-size:11px; } .chart .grid { stroke:var(--line); }
table.events { max-width:760px; }
pre.raw { max-height:480px; overflow:auto; background:var(--bg); padding:10px; border-radius:6px;
  font-size:12px; }
ul.changes { padding-left:18px; margin:0; } ul.changes li { margin:2px 0; }
"""

_FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
            "<text y='13' font-size='13'>🚗</text></svg>")


def _layout(title: str, body: str, active: str) -> str:
    def link(href: str, key: str, text: str) -> str:
        return f'<a href="{href}"{" class=on" if key == active else ""}>{text}</a>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · auto-hunter</title><link rel="icon" href="{_FAVICON}">
<style>{CSS}</style></head><body>
<nav><span class="brand">auto-hunter</span>{link("/", "listings", "Listings")}
{link("/overview", "overview", "Overview")}</nav>
<main>{body}</main></body></html>"""


def render(db: Database, config: Config, target: str) -> tuple[int, str]:
    """Route a request target ("/path?query") to (HTTP status, HTML)."""
    parts = urlsplit(target)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    path = parts.path.rstrip("/") or "/"
    if path == "/":
        return 200, listings_page(db, config, {k: v for k, v in query.items()
                                               if k in FILTERS or k == "page"})
    if path == "/overview":
        return 200, overview_page(db, config, query)
    if path.startswith("/listing/") and path[len("/listing/"):].isdigit():
        page = listing_page(db, config, int(path[len("/listing/"):]))
        if page is not None:
            return 200, page
    return 404, _layout("Not found", '<p class="empty">No such page or listing. '
                                     '<a href="/">Back to listings</a></p>', "")


def serve(db_path: Path, config: Config, host: str, port: int, open_browser: bool) -> None:
    """Serve the viewer until Ctrl+C."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            db = Database(db_path, readonly=True)
            try:
                status, html = render(db, config, self.path)
            except Exception:
                log.exception("Failed to render %s", self.path)
                status, html = 500, _layout("Error", f"<pre>{_e(traceback.format_exc())}</pre>", "")
            finally:
                db.close()
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:
            log.debug("%s %s", self.address_string(), format % args)

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{server.server_port}/"
    print(f"Browsing {db_path} at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
