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
