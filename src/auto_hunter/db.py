"""SQLite storage: current listing state plus an append-only change history.

Tables:
- listings:            one row per listing, its latest state and original source payload
- listing_snapshots:   append-only; one row per observed change (price/mileage/status)
- listing_profiles:    which profiles a listing has matched, and when
- listing_assessments: derived labels (generation, engine, flags), recomputable any time
- collection_runs:     one row per source per `collect` run, for monitoring reliability
- source_state:        collector bookkeeping between runs (e.g. Bazoš crawl position)

Timestamps are UTC ISO-8601 strings, so they sort and compare correctly as text.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from auto_hunter.evaluate import Assessment
from auto_hunter.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                 INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    url                TEXT NOT NULL,
    title              TEXT NOT NULL,
    price_czk          INTEGER,
    make               TEXT,
    model              TEXT,
    model_key          TEXT,
    variant            TEXT,
    year               INTEGER,
    manufacturing_date TEXT,
    mileage_km         INTEGER,
    fuel               TEXT,
    gearbox            TEXT,
    power_kw           INTEGER,
    engine_cc          INTEGER,
    body               TEXT,
    vin                TEXT,
    seller_type        TEXT,
    seller_name        TEXT,
    region             TEXT,
    district           TEXT,
    municipality       TEXT,
    description        TEXT,
    service_book       INTEGER,
    first_owner        INTEGER,
    damaged            INTEGER,
    image_urls         TEXT NOT NULL DEFAULT '[]',
    source_created_at  TEXT,
    -- active: in search results; unmatched: still online but outside all profiles;
    -- removed: source no longer has it (withdrawn, sold, or relisted - we can't tell).
    status             TEXT NOT NULL DEFAULT 'active',
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    removed_at         TEXT,
    details_fetched_at TEXT,
    raw_json           TEXT NOT NULL,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_vin ON listings(vin);

-- One row per observed change, never updated: this is the price history.
CREATE TABLE IF NOT EXISTS listing_snapshots (
    id          INTEGER PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    observed_at TEXT NOT NULL,
    event       TEXT NOT NULL,  -- new | changed | removed | reactivated | unmatched
    price_czk   INTEGER,
    mileage_km  INTEGER,
    status      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_listing ON listing_snapshots(listing_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON listing_snapshots(observed_at);

CREATE TABLE IF NOT EXISTS listing_profiles (
    listing_id    INTEGER NOT NULL REFERENCES listings(id),
    profile       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (listing_id, profile)
);

-- Derived labels (generation, engine, flags). Recomputable from listings + catalog.
CREATE TABLE IF NOT EXISTS listing_assessments (
    listing_id  INTEGER PRIMARY KEY REFERENCES listings(id),
    generation  TEXT,
    engine      TEXT,
    price_tier  TEXT,
    confidence  TEXT NOT NULL,
    data        TEXT NOT NULL,  -- full Assessment as JSON
    assessed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    source     TEXT PRIMARY KEY,
    state      TEXT NOT NULL,  -- JSON, owned by the collector
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
    seen        INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    errors      TEXT NOT NULL DEFAULT '[]'
);
"""

_LISTING_FIELDS = [
    "url", "title", "price_czk", "make", "model", "model_key", "variant", "year", "manufacturing_date",
    "mileage_km", "fuel", "gearbox", "power_kw", "engine_cc", "body", "vin", "seller_type",
    "seller_name", "region", "district", "municipality", "description", "service_book",
    "first_owner", "damaged", "source_created_at",
]
# Fields only present in detail responses; a later search result must not blank them.
DETAIL_ONLY_FIELDS = {
    "power_kw", "engine_cc", "body", "vin", "description", "service_book", "first_owner",
    "damaged",
}
# Columns added after the first release: (table, column, type) for _migrate.
_ADDED_COLUMNS = [
    ("listings", "model_key", "TEXT"),
    ("listings", "damaged", "INTEGER"),
    ("collection_runs", "source", "TEXT"),
]


def row_to_listing(row: sqlite3.Row) -> Listing:
    """Rebuild a Listing from stored state (for re-assessment without refetching)."""
    values = {f: row[f] for f in _LISTING_FIELDS}
    for f in ("service_book", "first_owner", "damaged"):
        if values[f] is not None:
            values[f] = bool(values[f])
    return Listing(
        source=row["source"], source_id=row["source_id"], **values,
        image_urls=json.loads(row["image_urls"]),
        is_active=row["status"] != "removed",
        has_details=row["details_fetched_at"] is not None,
        raw=json.loads(row["raw_json"]),
    )


@dataclass
class UpsertResult:
    """What upsert_listing did: event is "new", "changed", "reactivated" or None."""

    listing_id: int
    event: str | None  # None when nothing tracked changed
    old_price: int | None = None
    new_price: int | None = None


class Database:
    """Thin wrapper over one SQLite connection. Callers decide when to commit()."""

    def __init__(self, path: Path | str, readonly: bool = False) -> None:
        """Open (creating if needed) the database at path, or ":memory:" for tests.

        readonly opens an existing file as-is: no schema creation, no migrations, and
        any write raises. `browse` uses it so it can never touch collected data.
        """
        if readonly:
            self.conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created."""
        for table, column, col_type in _ADDED_COLUMNS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column in cols:
                continue
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            if (table, column) == ("listings", "model_key"):
                self.conn.execute(
                    "UPDATE listings SET model_key = json_extract(raw_json, '$.manufacturer_cb.seo_name')"
                    " || ':' || json_extract(raw_json, '$.model_cb.seo_name') WHERE source = 'sauto'"
                )
            if (table, column) == ("collection_runs", "source"):
                self.conn.execute("UPDATE collection_runs SET source = 'sauto'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- listings -----------------------------------------------------------------

    def get_listing(self, source: str, source_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()

    def upsert_listing(self, listing: Listing, now: str) -> UpsertResult:
        """Store the listing's current state and record a snapshot if price/mileage/status changed.

        Always sets status to 'active'. A search result (no details) never blanks
        fields that only a detail fetch provides, nor replaces the richer detail payload.
        """
        row = self.get_listing(listing.source, listing.source_id)
        values = {f: getattr(listing, f) for f in _LISTING_FIELDS}
        values["image_urls"] = json.dumps(listing.image_urls)
        values["raw_json"] = json.dumps(listing.raw, ensure_ascii=False)
        status = "active"

        if row is None:
            cols = ["source", "source_id", *values, "status", "first_seen_at", "last_seen_at",
                    "details_fetched_at"]
            params = [listing.source, listing.source_id, *values.values(), status, now, now,
                      now if listing.has_details else None]
            cur = self.conn.execute(
                f"INSERT INTO listings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                params,
            )
            self._snapshot(cur.lastrowid, now, "new", listing.price_czk, listing.mileage_km, status)
            return UpsertResult(cur.lastrowid, "new", None, listing.price_czk)

        if not listing.has_details:
            for f in DETAIL_ONLY_FIELDS:
                if values[f] is None:
                    values[f] = row[f]
            # Keep the richer detail payload rather than overwriting it with a search stub.
            if row["details_fetched_at"]:
                values["raw_json"] = row["raw_json"]
            if not listing.image_urls:
                values["image_urls"] = row["image_urls"]

        sets = ", ".join(f"{k} = ?" for k in values)
        extra = ", details_fetched_at = ?" if listing.has_details else ""
        params = [*values.values(), status, now, None]
        if listing.has_details:
            params.append(now)
        self.conn.execute(
            f"UPDATE listings SET {sets}, status = ?, last_seen_at = ?, removed_at = ?{extra} "
            f"WHERE id = ?",
            [*params, row["id"]],
        )

        if row["status"] != "active":
            event = "reactivated"
        elif row["price_czk"] != listing.price_czk or row["mileage_km"] != listing.mileage_km:
            event = "changed"
        else:
            return UpsertResult(row["id"], None)
        self._snapshot(row["id"], now, event, listing.price_czk, listing.mileage_km, status)
        return UpsertResult(row["id"], event, row["price_czk"], listing.price_czk)

    def set_status(self, listing_id: int, status: str, now: str) -> None:
        """Mark a listing 'removed' or 'unmatched' and record it in the history."""
        row = self.conn.execute(
            "SELECT price_czk, mileage_km FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        self.conn.execute(
            "UPDATE listings SET status = ?, removed_at = ? WHERE id = ?",
            (status, now if status == "removed" else None, listing_id),
        )
        self._snapshot(listing_id, now, status, row["price_czk"], row["mileage_km"], status)

    def link_profile(self, listing_id: int, profile: str, now: str) -> None:
        """Record that the listing matched this profile in this run."""
        self.conn.execute(
            """INSERT INTO listing_profiles (listing_id, profile, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (listing_id, profile) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (listing_id, profile, now, now),
        )

    def listing_profiles(self, listing_id: int) -> set[str]:
        """Profiles the listing has matched."""
        return {r[0] for r in self.conn.execute(
            "SELECT profile FROM listing_profiles WHERE listing_id = ?", (listing_id,))}

    def save_assessment(self, listing_id: int, assessment: Assessment, now: str) -> None:
        """Replace the listing's assessment. Only the latest one is kept."""
        self.conn.execute(
            """INSERT OR REPLACE INTO listing_assessments
               (listing_id, generation, engine, price_tier, confidence, data, assessed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, assessment.generation, assessment.engine, assessment.price_tier,
             assessment.confidence, json.dumps(assessment.to_dict(), ensure_ascii=False), now),
        )

    def all_listings(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM listings").fetchall()

    def known_profiles(self, source: str) -> set[str]:
        """Profiles that have matched at least one listing from this source before."""
        return {r[0] for r in self.conn.execute(
            """SELECT DISTINCT lp.profile FROM listing_profiles lp
               JOIN listings l ON l.id = lp.listing_id WHERE l.source = ?""", (source,))}

    def active_listings(self, source: str, seen_before: str | None = None,
                        profiles: list[str] | None = None) -> list[sqlite3.Row]:
        """(id, source_id) of listings considered live and matching.

        With seen_before, only those whose last_seen_at is older than that timestamp.
        With profiles, only those matched by no profile outside that list, so a run
        limited to some profiles leaves the other profiles' listings alone.
        """
        sql = "SELECT id, source_id FROM listings WHERE source = ? AND status = 'active'"
        params: list = [source]
        if profiles is not None:
            sql += (" AND NOT EXISTS (SELECT 1 FROM listing_profiles p WHERE p.listing_id ="
                    f" listings.id AND p.profile NOT IN ({','.join('?' * len(profiles))}))")
            params.extend(profiles)
        if seen_before is not None:
            sql += " AND last_seen_at < ? ORDER BY last_seen_at"
            params.append(seen_before)
        return self.conn.execute(sql, params).fetchall()

    def same_vehicle(self, listing_id: int, vin: str | None) -> list[sqlite3.Row]:
        """Active listings on other sources that are (probably) the same car.

        Matched by full VIN when known; otherwise by an exact fingerprint of model,
        year and mileage, since several dealer sites (Louda, Klokočka) publish no VIN
        but mirror their stock elsewhere. `match` says which rule applied.
        """
        row = self.conn.execute(
            "SELECT source, model_key, year, mileage_km FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        matches = []
        if vin:
            matches += self.conn.execute(
                """SELECT source, price_czk, url, 'vin' AS match FROM listings
                   WHERE vin = ? AND id != ? AND source != ? AND status = 'active'""",
                (vin, listing_id, row["source"]),
            ).fetchall()
        # An exact odometer reading is specific enough only for real mileages. Two
        # listings that both have full VINs are compared by VIN alone (above).
        if row["model_key"] and row["year"] and (row["mileage_km"] or 0) >= 1000:
            matches += self.conn.execute(
                """SELECT source, price_czk, url, 'fingerprint' AS match FROM listings
                   WHERE model_key = ? AND year = ? AND mileage_km = ? AND id != ?
                     AND source != ? AND status = 'active'
                     AND (? IS NULL OR vin IS NULL OR vin LIKE '%XXX%' OR length(vin) != 17)""",
                (row["model_key"], row["year"], row["mileage_km"], listing_id, row["source"], vin),
            ).fetchall()
        return list({m["url"]: m for m in matches}.values())

    # --- collector state ------------------------------------------------------------

    def load_state(self, source: str) -> dict:
        row = self.conn.execute(
            "SELECT state FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return json.loads(row["state"]) if row else {}

    def save_state(self, source: str, state: dict, now: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_state (source, state, updated_at) VALUES (?, ?, ?)",
            (source, json.dumps(state), now),
        )

    def _snapshot(self, listing_id: int, now: str, event: str, price: int | None,
                  mileage: int | None, status: str) -> None:
        self.conn.execute(
            """INSERT INTO listing_snapshots (listing_id, observed_at, event, price_czk, mileage_km, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (listing_id, now, event, price, mileage, status),
        )

    # --- runs ---------------------------------------------------------------------

    def start_run(self, source: str, now: str) -> int:
        """Open a collection_runs row (committed at once, so crashed runs stay visible)."""
        cur = self.conn.execute(
            "INSERT INTO collection_runs (source, started_at) VALUES (?, ?)", (source, now)
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, now: str, status: str, seen: int, new: int,
                   changed: int, removed: int, errors: list[str]) -> None:
        """Close the run with its counts; status is ok, partial or failed."""
        self.conn.execute(
            """UPDATE collection_runs SET finished_at = ?, status = ?, seen = ?, new = ?,
               changed = ?, removed = ?, errors = ? WHERE id = ?""",
            (now, status, seen, new, changed, removed, json.dumps(errors), run_id),
        )
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
