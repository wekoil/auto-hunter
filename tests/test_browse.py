import sqlite3

import pytest

from auto_hunter.browse import render
from auto_hunter.db import Database
from auto_hunter.pipeline import collect
from test_pipeline import FakeCollector, cfg, make_listing


@pytest.fixture
def db_path(tmp_path):
    """A database file with two runs: listing 2 got cheaper, listing 3 was removed."""
    path = tmp_path / "auto_hunter.db"
    db = Database(path)
    c = FakeCollector()
    c.search_results = [
        make_listing("1", 250_000, title="Škoda Octavia combi", description="Servisní kniha, tažné"),
        make_listing("2", 200_000, title="Ford Focus kombi"),
        make_listing("3", 180_000, title="Kia Ceed SW"),
    ]
    collect(db, c, cfg())
    c.search_results = [make_listing("1", 250_000, title="Škoda Octavia combi"),
                        make_listing("2", 180_000, title="Ford Focus kombi")]
    collect(db, c, cfg())
    db.close()
    return path


@pytest.fixture
def ro(db_path):
    d = Database(db_path, readonly=True)
    yield d
    d.close()


def page(ro, target):
    status, html = render(ro, cfg(), target)
    assert status == 200, html
    return html


def test_readonly_database_rejects_writes(ro):
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("DELETE FROM listings")


def test_listings_default_to_active(ro):
    html = page(ro, "/")
    assert "Škoda Octavia combi" in html and "Ford Focus kombi" in html
    assert "Kia Ceed SW" not in html
    assert "2 of 2 active listings" in html


def test_filters_and_search(ro):
    assert "Kia Ceed SW" in page(ro, "/?status=removed")
    html = page(ro, "/?q=skoda")  # accent- and case-insensitive
    assert "Octavia" in html and "Focus" not in html
    assert "Octavia" in page(ro, "/?q=tazne")  # matches the original description too
    html = page(ro, "/?price_max=190000")
    assert "Focus" in html and "Octavia" not in html
    html = page(ro, "/?dropped=1")
    assert "Focus" in html and "Octavia" not in html and "was 200" in html


def test_sorting(ro):
    html = page(ro, "/?sort=price&dir=desc")
    assert html.index("Octavia") < html.index("Focus")
    html = page(ro, "/?sort=price&dir=asc")
    assert html.index("Focus") < html.index("Octavia")


def test_listing_detail_shows_history_and_original_text(ro):
    listing_id = ro.conn.execute("SELECT id FROM listings WHERE source_id = '2'").fetchone()[0]
    html = page(ro, f"/listing/{listing_id}")
    assert "Ford Focus kombi" in html
    assert "<svg" in html and "changed" in html
    assert "200 000 Kč" in html and "180 000 Kč" in html
    octavia = ro.conn.execute("SELECT id FROM listings WHERE source_id = '1'").fetchone()[0]
    assert "Servisní kniha, tažné" in page(ro, f"/listing/{octavia}")


def test_overview(ro):
    html = page(ro, "/overview?days=30")
    assert "Collection runs" in html and "Ford Focus kombi" in html


def test_unknown_pages_are_404(ro):
    assert render(ro, cfg(), "/listing/9999")[0] == 404
    assert render(ro, cfg(), "/nope")[0] == 404


def test_html_is_escaped(tmp_path):
    db = Database(tmp_path / "x.db")
    c = FakeCollector()
    c.search_results = [make_listing("1", title="<script>alert(1)</script>")]
    collect(db, c, cfg())
    db.close()
    ro = Database(tmp_path / "x.db", readonly=True)
    try:
        html = page(ro, "/")
        assert "<script>alert" not in html and "&lt;script&gt;" in html
    finally:
        ro.close()
