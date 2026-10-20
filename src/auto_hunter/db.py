"""SQLite storage: current listing state plus an append-only change history."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

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

CREATE TABLE IF NOT EXISTS collection_runs (
    id          INTEGER PRIMARY KEY,
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
    "url", "title", "price_czk", "make", "model", "variant", "year", "manufacturing_date",
    "mileage_km", "fuel", "gearbox", "power_kw", "engine_cc", "body", "vin", "seller_type",
    "seller_name", "region", "district", "municipality", "description", "service_book",
    "first_owner", "source_created_at",
]
# Fields only present in detail responses; a later search result must not blank them.
DETAIL_ONLY_FIELDS = {
    "power_kw", "engine_cc", "body", "vin", "description", "service_book", "first_owner",
}


@dataclass
class UpsertResult:
    listing_id: int
    event: str | None  # None when nothing tracked changed
    old_price: int | None = None
    new_price: int | None = None


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- listings -----------------------------------------------------------------

    def get_listing(self, source: str, source_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()

    def upsert_listing(self, listing: Listing, now: str) -> UpsertResult:
        """Store the listing's current state and record a snapshot if price/mileage/status changed."""
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

    def touch(self, listing_id: int, now: str) -> None:
        self.conn.execute("UPDATE listings SET last_seen_at = ? WHERE id = ?", (now, listing_id))

    def link_profile(self, listing_id: int, profile: str, now: str) -> None:
        self.conn.execute(
            """INSERT INTO listing_profiles (listing_id, profile, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (listing_id, profile) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (listing_id, profile, now, now),
        )

    def known_profiles(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT profile FROM listing_profiles")}

    def active_listings(self, source: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, source_id FROM listings WHERE source = ? AND status = 'active'", (source,)
        ).fetchall()

    def _snapshot(self, listing_id: int, now: str, event: str, price: int | None,
                  mileage: int | None, status: str) -> None:
        self.conn.execute(
            """INSERT INTO listing_snapshots (listing_id, observed_at, event, price_czk, mileage_km, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (listing_id, now, event, price, mileage, status),
        )

    # --- runs ---------------------------------------------------------------------

    def start_run(self, now: str) -> int:
        cur = self.conn.execute("INSERT INTO collection_runs (started_at) VALUES (?)", (now,))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, now: str, status: str, seen: int, new: int,
                   changed: int, removed: int, errors: list[str]) -> None:
        self.conn.execute(
            """UPDATE collection_runs SET finished_at = ?, status = ?, seen = ?, new = ?,
               changed = ?, removed = ?, errors = ? WHERE id = ?""",
            (now, status, seen, new, changed, removed, json.dumps(errors), run_id),
        )
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
