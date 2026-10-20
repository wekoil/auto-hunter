"""Plain-text reports over the collected history."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from auto_hunter.db import Database


def _fmt_price(p: int | None) -> str:
    return f"{p:>9,}".replace(",", " ") if p is not None else "        ?"


def _fmt_km(km: int | None) -> str:
    return f"{km:>7,}".replace(",", " ") if km is not None else "      ?"


def changes_report(db: Database, days: float) -> str:
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
        "",
    ]
    for s in snaps:
        out.append(f"{s['observed_at']}  {s['event']:<11} {_fmt_price(s['price_czk'])} Kč  "
                   f"{_fmt_km(s['mileage_km'])} km")
    return "\n".join(out)


def market_report(db: Database) -> str:
    """Per profile and year: count and asking-price spread of active listings."""
    rows = db.conn.execute(
        """SELECT lp.profile, l.year, l.price_czk, l.mileage_km
           FROM listings l JOIN listing_profiles lp ON lp.listing_id = l.id
           WHERE l.status = 'active' AND l.price_czk IS NOT NULL
           ORDER BY lp.profile, l.year"""
    ).fetchall()
    if not rows:
        return "No active listings yet. Run `auto-hunter collect` first."
    groups: dict[tuple[str, int | None], list] = {}
    for r in rows:
        groups.setdefault((r["profile"], r["year"]), []).append(r)
    out = [f"{'profile':<22}{'year':>5}{'n':>5}{'p25':>11}{'median':>11}{'p75':>11}{'med km':>10}"]
    for (profile, year), items in groups.items():
        prices = sorted(i["price_czk"] for i in items)
        kms = [i["mileage_km"] for i in items if i["mileage_km"] is not None]
        q = statistics.quantiles(prices, n=4) if len(prices) >= 2 else [prices[0]] * 3
        out.append(
            f"{profile:<22}{year or '?':>5}{len(prices):>5}{_fmt_price(int(q[0])):>11}"
            f"{_fmt_price(int(q[1])):>11}{_fmt_price(int(q[2])):>11}"
            f"{_fmt_km(int(statistics.median(kms))) if kms else '?':>10}"
        )
    runs = db.conn.execute(
        "SELECT started_at, status, seen, new, changed, removed FROM collection_runs "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    out.append("\nRecent runs:")
    for r in runs:
        out.append(f"  {r['started_at']}  {r['status']:<8} seen={r['seen']} new={r['new']} "
                   f"changed={r['changed']} removed={r['removed']}")
    return "\n".join(out)
