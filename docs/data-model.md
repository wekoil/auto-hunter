# Data model

One SQLite file (`data/auto_hunter.db` by default). Timestamps are UTC ISO-8601
strings (`2026-10-20T17:48:50+00:00`), so text comparison sorts correctly.

```
listings ─┬─< listing_snapshots     (history, append-only)
          ├─< listing_profiles      (which profiles matched)
          └── listing_assessments   (derived labels, 1:1, recomputable)
collection_runs                      (one row per collect)
```

## listings

Current state of each advertisement, unique on `(source, source_id)`.

| Columns | Notes |
|---|---|
| `source`, `source_id`, `url` | e.g. `sauto`, `211125480` |
| `title`, `variant`, `make`, `model`, `model_key` | `model_key` = `skoda:octavia` |
| `price_czk`, `mileage_km`, `year`, `manufacturing_date` | price NULL = "by agreement" |
| `fuel`, `gearbox`, `body`, `power_kw`, `engine_cc` | Sauto SEO names; detail-only fields may be NULL until fetched |
| `vin`, `service_book`, `first_owner`, `description` | detail only |
| `seller_type`, `seller_name`, `region`, `district`, `municipality` | `dealer` / `private` |
| `image_urls` | JSON array |
| `status` | see below |
| `first_seen_at`, `last_seen_at`, `removed_at`, `details_fetched_at`, `source_created_at` | |
| `raw_json` | original payload (detail payload once fetched) |

### Status

| Status | Meaning | Becomes |
|---|---|---|
| `active` | In search results and matching at least one profile | `removed`/`unmatched` |
| `unmatched` | Still online, but outside every profile (price raised, wrong engine after details, rule changed) | `active` if it matches again |
| `removed` | The source no longer has it | `active` if it reappears |

`removed` does **not** mean sold: it can also be withdrawn or relisted under a new id.

## listing_snapshots

Append-only history. A row is written only when something tracked changes.

| Column | Notes |
|---|---|
| `event` | `new`, `changed` (price or mileage), `removed`, `unmatched`, `reactivated` |
| `price_czk`, `mileage_km`, `status` | values *after* the event |
| `observed_at` | run time |

Unchanged runs write nothing; `listings.last_seen_at` shows the car was still there.

## listing_profiles

`(listing_id, profile)` with `first_seen_at` / `last_seen_at`. A car matching several
profiles has several rows but is one listing, so statistics don't double-count it.

## listing_assessments

The latest evaluation per listing: `generation`, `engine` (set only when unambiguous),
`price_tier`, `confidence`, and `data` (full JSON including candidates and flags).
Overwritten on each run and by `auto-hunter reassess`. Never the only copy of anything.

## collection_runs

`started_at`, `finished_at`, `status` (`running` = crashed or still going, `ok`,
`partial` = some errors, `failed` = errors and nothing matched), counts, and `errors`
(JSON array). Check this table to see whether scheduled runs are healthy.

---

## Useful queries

Open with `sqlite3 data/auto_hunter.db` (or any SQLite browser).

Price drops in the last 7 days:
```sql
SELECT l.title, s.observed_at, s.price_czk, l.url
FROM listing_snapshots s JOIN listings l ON l.id = s.listing_id
WHERE s.event = 'changed'
  -- strftime, not datetime(): stored values use 'T' as the date/time separator
  AND s.observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')
ORDER BY s.observed_at DESC;
```

How long removed listings stayed online:
```sql
SELECT model_key, COUNT(*) AS n,
       ROUND(AVG(julianday(removed_at) - julianday(first_seen_at)), 1) AS avg_days
FROM listings WHERE status = 'removed' GROUP BY model_key;
```

Active listings by identified engine:
```sql
SELECT a.engine, COUNT(*), MIN(l.price_czk), AVG(l.price_czk)
FROM listings l JOIN listing_assessments a ON a.listing_id = l.id
WHERE l.status = 'active' GROUP BY a.engine;
```

Cars with a specific flag:
```sql
SELECT l.title, l.url FROM listings l JOIN listing_assessments a ON a.listing_id = l.id
WHERE l.status = 'active'
  AND EXISTS (SELECT 1 FROM json_each(a.data, '$.flags') f
              WHERE json_extract(f.value, '$.code') = 'engine_avoid');
```

Run health:
```sql
SELECT started_at, status, seen, new, changed, removed, errors
FROM collection_runs ORDER BY id DESC LIMIT 10;
```
