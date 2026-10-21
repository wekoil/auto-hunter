from __future__ import annotations

import dataclasses

import pytest

from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.config import Catalog, Config, EvaluationRules, Requirements, SearchProfile, Settings
from auto_hunter.db import Database
from auto_hunter.models import Listing
from auto_hunter.pipeline import collect


def make_listing(source_id: str, price: int = 200_000, **kw) -> Listing:
    return Listing(source="fake", source_id=source_id, url=f"https://x/{source_id}",
                   title=kw.pop("title", f"Car {source_id}"), price_czk=price,
                   year=kw.pop("year", 2017), mileage_km=kw.pop("mileage_km", 120_000), **kw)


class FakeCollector(Collector):
    source = "fake"

    def __init__(self) -> None:
        self.search_results: list[Listing] = []
        self.details: dict[str, Listing | None] = {}
        self.fetched: list[str] = []
        self.fail_search = False

    def search(self, profile: SearchProfile) -> SearchResult:
        if self.fail_search:
            raise CollectorError("boom")
        return SearchResult([dataclasses.replace(l) for l in self.search_results], True)

    def fetch(self, source_id: str) -> Listing | None:
        self.fetched.append(source_id)
        if source_id in self.details:
            d = self.details[source_id]
            return dataclasses.replace(d, has_details=True) if d else None
        base = next((l for l in self.search_results if l.source_id == source_id), None)
        return dataclasses.replace(base, has_details=True, body="kombi") if base else None


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


PROFILE = SearchProfile(name="p", models=["x:y"], price_max=300_000, year_min=2015,
                        exclude_keywords=["havarovan"])


def cfg(profile: SearchProfile = PROFILE) -> Config:
    return Config(Settings(), Requirements(), Catalog(), EvaluationRules(), [profile])


def snapshots(db, source_id):
    return [tuple(r) for r in db.conn.execute(
        """SELECT s.event, s.price_czk FROM listing_snapshots s JOIN listings l ON l.id = s.listing_id
           WHERE l.source_id = ? ORDER BY s.id""", (source_id,))]


def test_first_run_is_baseline_without_new_alerts(db):
    c = FakeCollector()
    c.search_results = [make_listing("1"), make_listing("2")]
    s = collect(db, c, cfg())
    assert (s.seen, s.new) == (2, 2)
    assert s.alerts == []
    assert sorted(c.fetched) == ["1", "2"]

    c.search_results.append(make_listing("3"))
    s = collect(db, c, cfg())
    assert [a.listing.source_id for a in s.alerts] == ["3"]
    assert c.fetched.count("1") == 1  # details are fetched once


def test_price_drop_recorded_and_alerted(db):
    c = FakeCollector()
    c.search_results = [make_listing("1", 200_000)]
    collect(db, c, cfg())
    c.search_results = [make_listing("1", 180_000)]
    s = collect(db, c, cfg())
    assert [(a.kind, a.old_price) for a in s.alerts] == [("price_drop", 200_000)]
    assert snapshots(db, "1") == [("new", 200_000), ("changed", 180_000)]
    # Unchanged run adds no snapshot.
    collect(db, c, cfg())
    assert len(snapshots(db, "1")) == 2


def test_small_price_drop_does_not_alert(db):
    c = FakeCollector()
    c.search_results = [make_listing("1", 200_000)]
    collect(db, c, cfg())
    c.search_results = [make_listing("1", 199_000)]
    assert collect(db, c, cfg()).alerts == []


def test_vanished_listing_removed_or_unmatched(db):
    c = FakeCollector()
    c.search_results = [make_listing("1"), make_listing("2")]
    collect(db, c, cfg())

    c.search_results = []
    c.details = {"1": None, "2": make_listing("2", 350_000)}
    s = collect(db, c, cfg())
    assert s.removed == 1
    assert db.get_listing("fake", "1")["status"] == "removed"
    row = db.get_listing("fake", "2")
    assert (row["status"], row["price_czk"]) == ("unmatched", 350_000)

    # Unmatched listings aren't re-checked every run.
    c.fetched.clear()
    collect(db, c, cfg())
    assert c.fetched == []

    # Listing comes back into search results.
    c.search_results = [make_listing("1", 190_000)]
    collect(db, c, cfg())
    assert db.get_listing("fake", "1")["status"] == "active"
    assert snapshots(db, "1")[-1] == ("reactivated", 190_000)


def test_profile_limited_run_leaves_other_profiles_alone(db):
    other = dataclasses.replace(PROFILE, name="q")
    config = Config(Settings(), Requirements(), Catalog(), EvaluationRules(), [PROFILE, other])
    c = FakeCollector()
    c.search_results = [make_listing("1")]
    collect(db, c, config, [other])
    c.search_results = [make_listing("2")]
    collect(db, c, config, [PROFILE])

    # "1" belongs to profile q only; a run of p must not check or unmatch it.
    c.fetched.clear()
    c.details = {"1": make_listing("1", 350_000)}
    collect(db, c, config, [PROFILE])
    assert "1" not in c.fetched
    assert db.get_listing("fake", "1")["status"] == "active"

    # A full run still does.
    c.search_results = []
    collect(db, c, config)
    assert db.get_listing("fake", "1")["status"] == "unmatched"


def test_profile_limited_run_keeps_listing_rejected_after_details(db):
    sedans = dataclasses.replace(PROFILE, name="s", body=["sedan"])
    config = Config(Settings(), Requirements(), Catalog(), EvaluationRules(), [PROFILE, sedans])
    c = FakeCollector()
    c.search_results = [make_listing("1")]
    collect(db, c, config, [PROFILE])  # details say kombi
    # The sedan search returns it too (no body in summaries), but the stored details
    # rule it out; it must stay active for profile p.
    collect(db, c, config, [sedans])
    assert db.get_listing("fake", "1")["status"] == "active"


def test_failed_search_skips_removal_detection(db):
    c = FakeCollector()
    c.search_results = [make_listing("1")]
    collect(db, c, cfg())
    c.fail_search = True
    s = collect(db, c, cfg())
    assert s.errors and s.removed == 0
    assert db.get_listing("fake", "1")["status"] == "active"
    run = db.conn.execute("SELECT status FROM collection_runs ORDER BY id DESC").fetchone()
    assert run["status"] == "failed"


def test_detail_rejection_is_remembered(db):
    profile = dataclasses.replace(PROFILE, body=["kombi"])
    c = FakeCollector()
    c.search_results = [make_listing("1")]
    c.details = {"1": make_listing("1", body="hatchback")}
    s = collect(db, c, cfg(profile))
    assert s.seen == 0
    assert db.get_listing("fake", "1")["status"] == "unmatched"
    collect(db, c, cfg(profile))
    assert c.fetched == ["1"]


def test_search_result_does_not_erase_details(db):
    c = FakeCollector()
    c.search_results = [make_listing("1")]
    c.details = {"1": make_listing("1", vin="TMBXXX", description="Servisní knížka")}
    collect(db, c, cfg())
    c.search_results = [make_listing("1", 190_000)]
    collect(db, c, cfg())
    row = db.get_listing("fake", "1")
    assert (row["vin"], row["description"], row["price_czk"]) == ("TMBXXX", "Servisní knížka", 190_000)


# --- incremental sources (Bazoš-style) ---------------------------------------------------

class IncrementalCollector(FakeCollector):
    source = "inc"
    full_scan = False
    summary_is_authoritative = False
    recheck_after_days = 2


def inc_listing(source_id, price=200_000, **kw):
    return dataclasses.replace(make_listing(source_id, price, **kw), source="inc")


def age_listing(db, source, source_id, days=3):
    db.conn.execute(
        "UPDATE listings SET last_seen_at = datetime('now', ?) WHERE source = ? AND source_id = ?",
        (f"-{days} days", source, source_id))
    db.commit()


def test_incremental_absence_is_not_removal(db):
    c = IncrementalCollector()
    c.search_results = [inc_listing("1")]
    collect(db, c, cfg())
    c.search_results = []
    c.fetched.clear()
    s = collect(db, c, cfg())
    assert s.removed == 0 and c.fetched == []        # recently seen: no recheck
    assert db.get_listing("inc", "1")["status"] == "active"


def test_incremental_recheck_detects_removal_and_price_drop(db):
    c = IncrementalCollector()
    c.search_results = [inc_listing("1"), inc_listing("2", 200_000)]
    collect(db, c, cfg())
    c.search_results = []
    age_listing(db, "inc", "1")
    age_listing(db, "inc", "2")
    c.details = {"1": None, "2": inc_listing("2", 180_000)}
    s = collect(db, c, cfg())
    assert s.removed == 1
    assert db.get_listing("inc", "1")["status"] == "removed"
    assert db.get_listing("inc", "2")["status"] == "active"
    assert [(a.kind, a.listing.source_id) for a in s.alerts] == [("price_drop", "2")]


def test_non_authoritative_summary_keeps_detail_specs(db):
    c = IncrementalCollector()
    c.search_results = [inc_listing("1", 200_000)]
    c.details = {"1": inc_listing("1", 200_000, mileage_km=150_000, vin="TMBJC7NE0H0062499")}
    collect(db, c, cfg())
    # Bumped again; the snippet lacks mileage and VIN and has a new price.
    c.search_results = [inc_listing("1", 190_000, mileage_km=None)]
    collect(db, c, cfg())
    row = db.get_listing("inc", "1")
    assert (row["price_czk"], row["mileage_km"], row["vin"]) == (190_000, 150_000, "TMBJC7NE0H0062499")
    assert [e for e, _ in snapshots(db, "1")] == ["new", "changed"]


def test_baseline_is_per_source(db):
    c = FakeCollector()
    c.search_results = [make_listing("1")]
    collect(db, c, cfg())
    inc = IncrementalCollector()
    inc.search_results = [inc_listing("9")]
    assert collect(db, inc, cfg()).alerts == []      # first run on this source


def test_same_vin_on_two_sources_is_flagged(db):
    vin = "TMBJC7NE0H0062499"
    c = FakeCollector()
    c.search_results = [make_listing("1", 200_000, vin=vin)]
    c.details = {"1": make_listing("1", 200_000, vin=vin)}
    collect(db, c, cfg())
    inc = IncrementalCollector()
    inc.search_results = [inc_listing("9", 190_000, vin=vin)]
    inc.details = {"9": inc_listing("9", 190_000, vin=vin)}
    collect(db, inc, cfg())
    data = db.conn.execute(
        """SELECT a.data FROM listing_assessments a JOIN listings l ON l.id = a.listing_id
           WHERE l.source = 'inc'""").fetchone()["data"]
    assert "Same VIN also listed on fake for 200 000 Kč" in data


def test_collector_state_persists_between_runs(db):
    class Counting(IncrementalCollector):
        def search(self, profile):
            self.state["runs"] = self.state.get("runs", 0) + 1
            return super().search(profile)

    for _ in range(3):
        c = Counting()                             # fresh instance each run, like the CLI
        c.search_results = []
        collect(db, c, cfg())
    assert db.load_state("inc") == {"runs": 3}


def test_same_car_without_vin_matched_by_fingerprint(db):
    c = FakeCollector()
    c.search_results = [make_listing("1", 200_000, model_key="x:y", vin="TMBJC7NE0H0062499")]
    c.details = {"1": c.search_results[0]}
    collect(db, c, cfg())
    inc = IncrementalCollector()          # e.g. a dealer site that publishes no VIN
    inc.search_results = [inc_listing("9", 195_000, model_key="x:y")]
    inc.details = {"9": inc.search_results[0]}
    collect(db, inc, cfg())
    data = db.conn.execute(
        """SELECT a.data FROM listing_assessments a JOIN listings l ON l.id = a.listing_id
           WHERE l.source = 'inc'""").fetchone()["data"]
    assert "Probably the same car (model, year, km) also listed on fake for 200 000 Kč" in data


def test_detail_fetch_does_not_blank_summary_fields(db):
    c = FakeCollector()
    c.search_results = [make_listing("1", mileage_km=98_000)]
    c.details = {"1": make_listing("1", mileage_km=None, power_kw=81)}   # detail lacks km
    collect(db, c, cfg())
    row = db.get_listing("fake", "1")
    assert (row["mileage_km"], row["power_kw"]) == (98_000, 81)
