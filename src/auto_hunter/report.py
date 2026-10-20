"""Plain-text reports over the collected history."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from auto_hunter.config import PRIORITIES, Config
from auto_hunter.db import Database


def _fmt_price(p: int | None) -> str:
    return f"{p:>9,}".replace(",", " ") if p is not None else "        ?"


def _fmt_km(km: int | None) -> str:
    return f"{km:>7,}".replace(",", " ") if km is not None else "      ?"


def changes_report(db: Database, days: float) -> str:
    """New, changed, removed and reactivated listings over the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.conn.execute(
        """SELECT s.event, s.observed_at, s.price_czk, l.title, l.year, l.mileage_km, l.url,
                  (SELECT p.price_czk FROM listing_snapshots p
                   WHERE p.listing_id = s.listing_id AND p.observed_at < s.observed_at
                   ORDER BY p.observed_at DESC, p.id DESC LIMIT 1) AS prev_price
           FROM listing_snapshots s JOIN listings l ON l.id = s.listing_id
           WHERE s.observed_at >= ? AND s.event IN ('new', 'changed', 'removed', 'reactivated')
             AND l.status != 'unmatched'
           ORDER BY s.event, s.price_czk""",
        (since,),
    ).fetchall()
    if not rows:
        return f"No changes in the last {days:g} day(s)."
    out = [f"Changes in the last {days:g} day(s)"]
    current = None
    for r in rows:
        if r["event"] != current:
            current = r["event"]
            out.append(f"\n== {current.upper()} ==")
        price_info = _fmt_price(r["price_czk"])
        if r["event"] == "changed" and r["prev_price"] not in (None, r["price_czk"]):
            price_info = f"{_fmt_price(r['prev_price'])} → {_fmt_price(r['price_czk']).strip()}"
        out.append(f"{price_info} Kč  {r['year'] or '?'}  {_fmt_km(r['mileage_km'])} km  "
                   f"{r['title']}\n    {r['url']}")
    return "\n".join(out)


def history_report(db: Database, source_id: str) -> str:
    """One listing: specs, current assessment and every recorded change."""
    listing = db.conn.execute("SELECT * FROM listings WHERE source_id = ?", (source_id,)).fetchone()
    if listing is None:
        return f"No listing with id {source_id}."
    snaps = db.conn.execute(
        "SELECT * FROM listing_snapshots WHERE listing_id = ? ORDER BY observed_at, id",
        (listing["id"],),
    ).fetchall()
    out = [
        f"{listing['title']}  ({listing['status']})",
        listing["url"],
        f"{listing['year']}  {listing['mileage_km']} km  {listing['power_kw']} kW  "
        f"{listing['fuel']}  {listing['gearbox']}  {listing['body']}  VIN: {listing['vin'] or '-'}",
        f"first seen {listing['first_seen_at']}, last seen {listing['last_seen_at']}",
    ]
    a = db.conn.execute(
        "SELECT data FROM listing_assessments WHERE listing_id = ?", (listing["id"],)
    ).fetchone()
    if a:
        a = json.loads(a["data"])
        out.append(
            f"generation: {a['generation'] or ' / '.join(a['generation_candidates']) or '?'}  "
            f"engine: {a['engine'] or ' / '.join(a['engine_candidates']) or '?'}  "
            f"tier: {a['price_tier']}  confidence: {a['confidence']}"
        )
        out.extend(f"  [{f['severity']}] {f['message']}" for f in a["flags"])
    out.append("")
    for s in snaps:
        out.append(f"{s['observed_at']}  {s['event']:<11} {_fmt_price(s['price_czk'])} Kč  "
                   f"{_fmt_km(s['mileage_km'])} km")
    return "\n".join(out)


def _quartiles(values: list[int]) -> tuple[int, int, int]:
    if len(values) < 2:
        return values[0], values[0], values[0]
    q = statistics.quantiles(values, n=4)
    return int(q[0]), int(q[1]), int(q[2])


def market_report(db: Database, config: Config) -> str:
    """Asking-price spread of active listings per generation and engine."""
    rows = db.conn.execute(
        """SELECT l.model, l.year, l.price_czk, l.mileage_km, a.generation, a.engine
           FROM listings l JOIN listing_assessments a ON a.listing_id = l.id
           WHERE l.status = 'active' AND l.price_czk IS NOT NULL"""
    ).fetchall()
    if not rows:
        return "No active listings yet. Run `auto-hunter collect` first."
    cat = config.catalog
    groups: dict[tuple[str, str], list] = {}
    for r in rows:
        gen = cat.generations[r["generation"]].label if r["generation"] else f"{r['model']} (gen ?)"
        eng = cat.engines[r["engine"]].label if r["engine"] else "engine ?"
        groups.setdefault((gen, eng), []).append(r)
    out = [f"{'generation':<28}{'engine':<22}{'n':>4}{'p25':>10}{'median':>10}{'p75':>10}"
           f"{'med yr':>7}{'med km':>9}"]
    for (gen, eng), items in sorted(groups.items()):
        p25, med, p75 = _quartiles(sorted(i["price_czk"] for i in items))
        years = [i["year"] for i in items if i["year"]]
        kms = [i["mileage_km"] for i in items if i["mileage_km"] is not None]
        out.append(
            f"{gen[:27]:<28}{eng[:21]:<22}{len(items):>4}{_fmt_price(p25):>10}{_fmt_price(med):>10}"
            f"{_fmt_price(p75):>10}{int(statistics.median(years)) if years else '?':>7}"
            f"{_fmt_km(int(statistics.median(kms))) if kms else '?':>9}"
        )
    out.append("\nAdvertised prices only; actual selling prices are typically lower.")
    runs = db.conn.execute(
        "SELECT started_at, status, seen, new, changed, removed FROM collection_runs "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    out.append("\nRecent runs:")
    for r in runs:
        out.append(f"  {r['started_at']}  {r['status']:<8} seen={r['seen']} new={r['new']} "
                   f"changed={r['changed']} removed={r['removed']}")
    return "\n".join(out)


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def shortlist_report(db: Database, config: Config, limit: int = 25,
                     profile: str | None = None) -> str:
    """Active listings ordered by profile priority, confidence, cautions, then price.

    A transparent ordering, not a score: weighted scoring needs market comparables
    and researched reliability data first.
    """
    rows = db.conn.execute(
        """SELECT l.*, a.data AS assessment, GROUP_CONCAT(lp.profile) AS profiles
           FROM listings l
           JOIN listing_assessments a ON a.listing_id = l.id
           JOIN listing_profiles lp ON lp.listing_id = l.id
           WHERE l.status = 'active'
           GROUP BY l.id"""
    ).fetchall()
    priority_of = {p.name: PRIORITIES.index(p.priority) for p in config.profiles}
    entries = []
    for r in rows:
        profiles = r["profiles"].split(",")
        if profile and profile not in profiles:
            continue
        a = json.loads(r["assessment"])
        cautions = [f for f in a["flags"] if f["severity"] == "caution"]
        best_priority = min((priority_of.get(p, len(PRIORITIES)) for p in profiles))
        key = (best_priority, _CONFIDENCE_ORDER[a["confidence"]], len(cautions),
               r["price_czk"] if r["price_czk"] is not None else 10**9)
        entries.append((key, r, a, cautions, profiles))
    if not entries:
        return "No active listings match."
    entries.sort(key=lambda e: e[0])
    cat = config.catalog
    out = []
    for _, r, a, cautions, profiles in entries[:limit]:
        gen = cat.generations[a["generation"]].label if a["generation"] else "gen ?"
        eng = (f"{cat.engines[a['engine']].label} [{a['engine_status']}]" if a["engine"]
               else "engine ?")
        out.append(
            f"{_fmt_price(r['price_czk'])} Kč  {a['price_tier'] or '-':<8} {r['year'] or '?'}  "
            f"{_fmt_km(r['mileage_km'])} km  conf={a['confidence']:<6} {r['title']}\n"
            f"    {gen} · {eng} · {r['gearbox'] or '?'} · {r['district'] or '?'} · "
            f"{','.join(profiles)}"
        )
        for f in cautions:
            out.append(f"    ⚠ {f['message']}")
        out.append(f"    {r['url']}")
    out.append(f"\n{len(entries)} active listings; showing {min(limit, len(entries))}.")
    return "\n".join(out)
