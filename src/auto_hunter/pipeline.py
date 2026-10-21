"""One collection run for one source: search profiles, store changes, detect removals.

Order of work, cheapest first:
1. search each profile (one query per model, shared between profiles)
2. drop listings with cheap local filters + classification from title/year
3. fetch details only for survivors never fetched before, then re-check them
4. store state, history snapshot, profile links and assessment; build alerts
5. check tracked listings the search didn't return:
   - full-scan sources (Sauto, Škoda Plus): only if every search was complete;
     missing means removed or no longer matching
   - incremental sources (Bazoš): those not seen for recheck_after_days are
     re-fetched; still online means still tracked, with price changes recorded
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from auto_hunter.classify import classify
from auto_hunter.collectors.base import Collector, CollectorError
from auto_hunter.config import Config, SearchProfile
from auto_hunter.db import DETAIL_ONLY_FIELDS, Database, row_to_listing
from auto_hunter.evaluate import Assessment, assess
from auto_hunter.filters import rejection_reason
from auto_hunter.models import Listing, is_real_vin, merge_listings

log = logging.getLogger(__name__)


def utcnow() -> str:
    """Current UTC time in the format stored in the database."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Alert:
    """Something worth telling you about; formatted by notify.py."""

    kind: str  # "new" | "price_drop"
    listing: Listing
    profiles: list[str]
    assessment: Assessment
    old_price: int | None = None


@dataclass
class RunSummary:
    """Counts for one run of one source; also written to the collection_runs table."""

    source: str = ""
    seen: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


class _Run:
    """State shared by the steps of one collection run."""

    def __init__(self, db: Database, collector: Collector, config: Config,
                 profiles: list[SearchProfile]) -> None:
        self.db, self.collector, self.config = db, collector, config
        self.profiles = profiles
        self.by_name = {p.name: p for p in profiles}
        # Profiles whose listings this run may remove or unmatch; None = all of them.
        self.scope = (None if set(self.by_name) == {p.name for p in config.profiles}
                      else list(self.by_name))
        self.now = utcnow()
        self.summary = RunSummary(source=collector.source)
        self.detail_budget = config.settings.collection.max_detail_fetches
        # A profile's first run on a source is the baseline: everything is "new" then,
        # so it doesn't alert.
        self.established = db.known_profiles(collector.source)

    def matching_profiles(self, listing: Listing, candidates: list[str]) -> list[str]:
        cls = classify(listing, self.config.catalog)
        return [n for n in candidates
                if rejection_reason(listing, self.by_name[n], cls, self.config.catalog) is None]

    def fetch(self, source_id: str, context: str) -> Listing | None:
        """Fetch one listing; errors are recorded, and raised as CollectorError."""
        try:
            return self.collector.fetch(source_id)
        except CollectorError as e:
            self.summary.errors.append(f"{context} {source_id}: {e}")
            raise

    def store(self, listing: Listing, profile_names: list[str]) -> None:
        """Save a matching listing, its profiles and assessment; queue alerts."""
        db, s = self.db, self.summary
        res = db.upsert_listing(listing, self.now)
        for name in profile_names:
            db.link_profile(res.listing_id, name, self.now)
        duplicates = db.same_vehicle(res.listing_id, listing.vin if is_real_vin(listing.vin) else None)
        assessment = assess(listing, classify(listing, self.config.catalog), self.config.catalog,
                            self.config.evaluation, duplicates)
        db.save_assessment(res.listing_id, assessment, self.now)
        if res.event == "new":
            s.new += 1
            if self.established.intersection(profile_names):
                s.alerts.append(Alert("new", listing, profile_names, assessment))
        elif res.event in ("changed", "reactivated"):
            s.changed += 1
            if _is_price_drop(res.old_price, res.new_price,
                              self.config.settings.alerts.min_price_drop_pct):
                s.alerts.append(Alert("price_drop", listing, profile_names, assessment,
                                      res.old_price))
        db.commit()

    def mark_unmatched(self, listing: Listing, existing) -> None:
        """Remember a non-matching listing so its details aren't paid for again."""
        if (existing is not None and self.scope is not None
                and self.db.listing_profiles(existing["id"]) - set(self.scope)):
            return  # still claimed by a profile this run didn't check
        if existing is None or existing["status"] == "active":
            res = self.db.upsert_listing(listing, self.now)
            self.db.set_status(res.listing_id, "unmatched", self.now)
            self.db.commit()


def collect(db: Database, collector: Collector, config: Config,
            profiles: list[SearchProfile] | None = None) -> RunSummary:
    """Run one collection of one source over `profiles` (default: all profiles).

    Errors from the source are recorded rather than raised, so one failing model
    doesn't lose the rest of the run. For full-scan sources, removal detection is
    skipped unless every search was complete; otherwise a network hiccup would mark
    live cars as removed.
    """
    run = _Run(db, collector, config, config.profiles if profiles is None else profiles)
    s = run.summary
    run_id = db.start_run(collector.source, run.now)
    collector.state = db.load_state(collector.source)
    complete = True
    # source_id -> (listing, matching profile names)
    matched: dict[str, tuple[Listing, list[str]]] = {}

    for profile in run.profiles:
        try:
            result = collector.search(profile)
        except CollectorError as e:
            log.error("%s: profile %s failed: %s", collector.source, profile.name, e)
            s.errors.append(f"{profile.name}: {e}")
            complete = False
            continue
        complete &= result.complete
        kept = 0
        for listing in result.listings:
            if run.matching_profiles(listing, [profile.name]):
                matched.setdefault(listing.source_id, (listing, []))[1].append(profile.name)
                kept += 1
        log.info("%s %s: %d found, %d kept after local filters", collector.source,
                 profile.name, len(result.listings), kept)

    for source_id, (listing, profile_names) in matched.items():
        existing = db.get_listing(collector.source, source_id)
        if existing is not None and existing["details_fetched_at"] is not None:
            if collector.summary_is_authoritative:
                for f in DETAIL_ONLY_FIELDS:
                    if getattr(listing, f) is None:
                        setattr(listing, f, existing[f])
            else:
                # Keep the specs parsed from the full listing; take only what a
                # summary reliably carries.
                stored = row_to_listing(existing)
                stored.price_czk, stored.title = listing.price_czk, listing.title
                stored.has_details = False
                listing = stored
        elif not listing.has_details and run.detail_budget > 0:
            run.detail_budget -= 1
            try:
                detailed = run.fetch(source_id, "detail")
            except CollectorError:
                detailed = None
            if detailed is not None:
                listing = merge_listings(listing, detailed)

        # Details reveal body, engine specs etc.; re-check what the search couldn't.
        profile_names = run.matching_profiles(listing, profile_names)
        if not profile_names:
            run.mark_unmatched(listing, existing)
            continue
        s.seen += 1
        run.store(listing, profile_names)

    if collector.full_scan:
        if complete:
            s.removed = _check_vanished(run, set(matched))
        else:
            log.warning("%s: search incomplete; skipping removal detection", collector.source)
    else:
        s.removed = _recheck_stale(run, set(matched))

    db.save_state(collector.source, collector.state, utcnow())
    status = "ok" if not s.errors else ("partial" if matched else "failed")
    db.finish_run(run_id, utcnow(), status, s.seen, s.new, s.changed, s.removed, s.errors)
    return s


def _check_vanished(run: _Run, seen_ids: set[str]) -> int:
    """Full-scan source: tracked listings no search returned are gone or out of scope."""
    db, cap = run.db, run.config.settings.collection.max_removal_checks
    vanished = [r for r in db.active_listings(run.collector.source, profiles=run.scope)
                if r["source_id"] not in seen_ids]
    removed = 0
    for row in vanished[:cap]:
        try:
            listing = run.fetch(row["source_id"], "removal check")
        except CollectorError:
            continue
        if listing is None or not listing.is_active:
            db.set_status(row["id"], "removed", run.now)
            removed += 1
        else:
            # Still online but outside our search filters (e.g. price raised). Keep its
            # latest state, then stop re-checking it every run.
            stored = db.get_listing(run.collector.source, row["source_id"])
            db.upsert_listing(merge_listings(row_to_listing(stored), listing), run.now)
            db.set_status(row["id"], "unmatched", run.now)
        db.commit()
    if len(vanished) > cap:
        log.info("%d vanished listings left for the next run", len(vanished) - cap)
    return removed


def _recheck_stale(run: _Run, seen_ids: set[str]) -> int:
    """Incremental source: re-fetch tracked listings not seen for a while.

    Not appearing in a crawl means nothing here, so each one is checked directly:
    gone → removed; still online → price/details updated, and kept if it still
    matches a profile.
    """
    db, cap = run.db, run.config.settings.collection.max_removal_checks
    cutoff = (datetime.now(timezone.utc) - timedelta(days=run.collector.recheck_after_days)
              ).isoformat(timespec="seconds")
    stale = [r for r in db.active_listings(run.collector.source, seen_before=cutoff,
                                           profiles=run.scope)
             if r["source_id"] not in seen_ids]
    removed = 0
    all_profiles = list(run.by_name)
    for row in stale[:cap]:
        try:
            listing = run.fetch(row["source_id"], "recheck")
        except CollectorError:
            continue
        if listing is None or not listing.is_active:
            db.set_status(row["id"], "removed", run.now)
            db.commit()
            removed += 1
            continue
        stored = db.get_listing(run.collector.source, row["source_id"])
        listing = merge_listings(row_to_listing(stored), listing)
        profile_names = run.matching_profiles(listing, all_profiles)
        if profile_names:
            run.store(listing, profile_names)
        else:
            run.mark_unmatched(listing, stored)
    if len(stale) > cap:
        log.info("%d stale listings left for the next run", len(stale) - cap)
    return removed


def _is_price_drop(old: int | None, new: int | None, min_pct: float) -> bool:
    """True if the price fell by at least min_pct percent."""
    return old is not None and new is not None and old > 0 and (old - new) / old * 100 >= min_pct


def reassess(db: Database, config: Config) -> int:
    """Re-label every stored listing, e.g. after editing catalog.yaml or evaluation.yaml.

    Uses stored data only (no network). It updates assessments, not profile membership:
    a listing that a new rule would reject stays until the next `collect` re-checks it.
    """
    now = utcnow()
    rows = db.all_listings()
    for row in rows:
        listing = row_to_listing(row)
        cls = classify(listing, config.catalog)
        duplicates = db.same_vehicle(row["id"], listing.vin if is_real_vin(listing.vin) else None)
        db.save_assessment(
            row["id"], assess(listing, cls, config.catalog, config.evaluation, duplicates), now)
    db.commit()
    return len(rows)
