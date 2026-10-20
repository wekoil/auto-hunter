"""One collection run: search every profile, store changes, detect removals."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from auto_hunter.collectors.base import Collector, CollectorError
from auto_hunter.config import SearchProfile, Settings
from auto_hunter.db import DETAIL_ONLY_FIELDS, Database
from auto_hunter.filters import rejection_reason
from auto_hunter.models import Listing

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Alert:
    kind: str  # "new" | "price_drop"
    listing: Listing
    profiles: list[str]
    old_price: int | None = None


@dataclass
class RunSummary:
    seen: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


def collect(db: Database, collector: Collector, profiles: list[SearchProfile],
            settings: Settings) -> RunSummary:
    now = utcnow()
    run_id = db.start_run(now)
    summary = RunSummary()
    complete = True
    # source_id -> (listing, matching profile names)
    matched: dict[str, tuple[Listing, list[str]]] = {}

    for profile in profiles:
        try:
            result = collector.search(profile)
        except CollectorError as e:
            log.error("Profile %s failed: %s", profile.name, e)
            summary.errors.append(f"{profile.name}: {e}")
            complete = False
            continue
        complete &= result.complete
        kept = 0
        for listing in result.listings:
            if rejection_reason(listing, profile) is None:
                matched.setdefault(listing.source_id, (listing, []))[1].append(profile.name)
                kept += 1
        log.info("%s: %d found, %d kept after local filters", profile.name,
                 len(result.listings), kept)

    detail_budget = settings.collection.max_detail_fetches
    by_name = {p.name: p for p in profiles}
    # A profile's first run is the baseline: everything is "new", so don't alert on it.
    established = db.known_profiles()
    for source_id, (listing, profile_names) in matched.items():
        existing = db.get_listing(collector.source, source_id)
        if existing is not None and existing["details_fetched_at"] is not None:
            for f in DETAIL_ONLY_FIELDS:
                if getattr(listing, f) is None:
                    setattr(listing, f, existing[f])
        elif detail_budget > 0:
            detail_budget -= 1
            try:
                detailed = collector.fetch(source_id)
            except CollectorError as e:
                summary.errors.append(f"detail {source_id}: {e}")
                detailed = None
            if detailed is not None:
                listing = detailed

        # Details reveal body, description etc.; re-check what the search couldn't.
        profile_names = [n for n in profile_names if rejection_reason(listing, by_name[n]) is None]
        if not profile_names:
            # Remember it as unmatched so we don't pay for its details again next run.
            if existing is None or existing["status"] == "active":
                res = db.upsert_listing(listing, now)
                db.set_status(res.listing_id, "unmatched", now)
                db.commit()
            continue

        summary.seen += 1
        res = db.upsert_listing(listing, now)
        for name in profile_names:
            db.link_profile(res.listing_id, name, now)
        if res.event == "new":
            summary.new += 1
            if established.intersection(profile_names):
                summary.alerts.append(Alert("new", listing, profile_names))
        elif res.event in ("changed", "reactivated"):
            summary.changed += 1
            if _is_price_drop(res.old_price, res.new_price, settings.alerts.min_price_drop_pct):
                summary.alerts.append(Alert("price_drop", listing, profile_names, res.old_price))
        db.commit()

    if complete:
        summary.removed = _check_vanished(db, collector, set(matched), now, settings, summary)
    else:
        log.warning("Search incomplete; skipping removal detection this run")

    status = "ok" if not summary.errors else ("partial" if matched else "failed")
    db.finish_run(run_id, utcnow(), status, summary.seen, summary.new, summary.changed,
                  summary.removed, summary.errors)
    return summary


def _check_vanished(db: Database, collector: Collector, seen_ids: set[str], now: str,
                    settings: Settings, summary: RunSummary) -> int:
    """Listings we tracked that no search returned: gone, or just no longer matching?"""
    vanished = [r for r in db.active_listings(collector.source) if r["source_id"] not in seen_ids]
    removed = 0
    for row in vanished[: settings.collection.max_removal_checks]:
        try:
            listing = collector.fetch(row["source_id"])
        except CollectorError as e:
            summary.errors.append(f"removal check {row['source_id']}: {e}")
            continue
        if listing is None or not listing.is_active:
            db.set_status(row["id"], "removed", now)
            removed += 1
        else:
            # Still online but outside our filters (e.g. price raised). Keep its latest
            # state, then stop re-checking it every run.
            db.upsert_listing(listing, now)
            db.set_status(row["id"], "unmatched", now)
        db.commit()
    if len(vanished) > settings.collection.max_removal_checks:
        log.info("%d vanished listings left for the next run",
                 len(vanished) - settings.collection.max_removal_checks)
    return removed


def _is_price_drop(old: int | None, new: int | None, min_pct: float) -> bool:
    return old is not None and new is not None and old > 0 and (old - new) / old * 100 >= min_pct
