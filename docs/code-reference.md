# Code reference

File by file, in the order data flows through them. Each module also has a docstring
at the top; this page explains how they connect and where to make common changes.

```
src/auto_hunter/
  cli.py              command-line entry point
  config.py           load + validate config/*.yaml
  models.py           Listing: the normalized advertisement
  collectors/
    base.py           Collector interface
    sauto.py          Sauto.cz implementation
  classify.py         generation/engine identification
  filters.py          does a listing fit a profile?
  evaluate.py         price tier, flags, confidence
  pipeline.py         one collection run, end to end
  db.py               SQLite schema and storage
  notify.py           alert formatting and delivery
  report.py           text reports
tests/
deploy/
```

Dependencies point one way: `cli → pipeline → (collectors, classify, filters, evaluate,
db) → config, models`. `report` and `notify` only read results.

---

## cli.py

Defines the `auto-hunter` command (registered in `pyproject.toml` under
`[project.scripts]`). Parses arguments, loads `.env` and the config, opens the
database, and dispatches:

| Command | Calls | Network |
|---|---|---|
| `collect [--profile X] [--no-alerts]` | `pipeline.collect`, `notify.send_alerts` | yes |
| `shortlist [--limit N] [--profile X]` | `report.shortlist_report` | no |
| `report [--days N]` | `report.changes_report` | no |
| `history <id>` | `report.history_report` | no |
| `market` | `report.market_report` | no |
| `reassess` | `pipeline.reassess` | no |

`collect` exits with status 1 only if errors occurred *and* nothing was seen, so a
systemd timer surfaces total failures without failing on a single flaky request.
httpx request logging is hidden unless `-v` is given.

## config.py

Loads the four YAML layers plus settings into dataclasses (`Settings`, `Requirements`,
`Catalog` of `Generation`/`Engine`, `EvaluationRules`, `SearchProfile`) bundled as `Config`.

The key function is `resolve_profile`: it merges global requirements into each profile,
derives the Sauto models and year range from the profile's generations, and validates
every reference. Everything downstream works with fully resolved `SearchProfile`s and
never reads requirements directly.

**To add a config option:** add a field to the relevant dataclass with a default (old
YAML keeps working), then use it where needed. Unknown YAML keys raise `TypeError` at
load, which catches typos.

## models.py

`Listing`, the one shape every source is converted into. Notable fields:

- `model_key`: the source's `make:model` slug (`skoda:octavia`); profiles and the
  catalog refer to models by this key.
- `has_details`: built from a detail response (has description, VIN, power, body...)
  rather than a search result.
- `is_active`: the source still shows it (only meaningful from a detail fetch).
- `raw`: the untouched source payload, stored so normalization can be redone later.

`None` always means "not stated", never "no"; filters and evaluation rely on that.

## collectors/base.py

The `Collector` interface: `search(profile) → SearchResult` and
`fetch(source_id) → Listing | None` (None = gone). `SearchResult.complete` is False when
the collector knows it didn't see everything; the pipeline then skips removal detection.
`CollectorError` means "this source call failed"; the pipeline records it and moves on.

## collectors/sauto.py

Sauto.cz via its undocumented JSON API (allowed by robots.txt).

- `search` runs one query per model in the profile and dedupes by id.
- `_query` pages through results and **caches per run** by exact parameters, so
  profiles sharing a model (A and D both search the Octavia) cost one query. It also
  raises if results come back for a different model, since Sauto silently ignores
  unknown model names.
- `_get_json` enforces the request delay and retries (network errors, 429, 5xx) with
  exponential backoff.
- `build_search_params` maps profile fields to API filters (`price_to`,
  `tachometer_to`, `fuel_seo`, `gearbox_seo`, `vehicle_body_seo`, used cars only).
- `parse_item` converts search and detail items into `Listing`. Year falls back to
  `in_operation_date` (dealer feeds often lack `manufacturing_date`); "price by
  agreement" becomes `price_czk=None`; dealer vs private is inferred from `premise`.

**To add another marketplace:** create `collectors/<name>.py` implementing `Collector`,
export it from `collectors/__init__.py`, and choose it in `cli.py`. Its `model_key`s
must match what profiles use, or map them.

## classify.py

Works out which generation and engine a listing could be, by elimination (see
[configuration.md](configuration.md#how-a-car-is-classified) for the steps).

- `fold` lowercases and strips diacritics; used for all text matching.
- `engine_hints` extracts displacement, power and turbo/NA from structured fields
  first, falling back to title text ("1.4TSI 92kw").
- `engine_matches` treats unknown hints as compatible: missing data never eliminates.
- `classify` returns a `Classification`: candidate lists plus `engine_known` (did we
  have any specs?), `engine_uncataloged` (specs matched nothing) and `cataloged_model`.
  `.generation`/`.engine` are set only when exactly one candidate remains.

`POWER_TOLERANCE_KW = 3` absorbs sellers' rounding (92 vs 90 kW).

## filters.py

`rejection_reason(listing, profile, cls, catalog)` returns a short reason string or
`None` if the car fits. Checks, cheapest first: model, price, mileage, year, fuel,
gearbox, body, title keywords; then, with a classification, generation, profile
engines, and `avoid` engines. Every check passes on unknown values. Reasons are for
logs/debugging; nothing stores them.

## evaluate.py

`assess(listing, cls, catalog, rules) → Assessment`: the evaluation layer. Produces
`Flag`s (code, severity `info`/`caution`, message), a price tier and confidence.

| Flag code | Severity | Uncertainty? |
|---|---|---|
| `generation_unclear` | caution | yes |
| `engine_unclear`, `engine_unknown`, `engine_uncataloged` | caution | yes |
| `engine_avoid` (might be an avoided engine) | caution | no |
| `engine_caution`, `gearbox_caution` | caution | no |
| `engine_unvetted` | info | no |
| `mileage_high`, `service_none` | caution | no |
| `service_unknown`, `vin_missing` | info | yes |
| `price_missing`, `tier_<label>` | info | no |

Service and VIN flags are only raised after a detail fetch, because search results
never contain them. **To add a rule:** add a `flag(...)` call in `assess`, and add its
code to `UNCERTAINTY_CODES` if it represents missing information.

## pipeline.py

`collect(db, collector, config, profiles=None)`, one run:

1. **Search** every profile. Errors are recorded and the run continues;
   `complete` tracks whether removal detection is safe.
2. **Pre-filter** each result with classification from title/year. Survivors are
   gathered per listing with the profiles they matched.
3. **Details**: known listings reuse stored detail fields; new ones get one detail
   request (within `max_detail_fetches`). Then classify and filter again, because the
   detail data (power, cc, body) often settles the engine.
4. **Rejected after details** → stored once as `unmatched`, so its details are never
   paid for again.
5. **Matched** → `upsert_listing` (state + snapshot if changed), `link_profile`,
   `assess` + `save_assessment`, and an `Alert` for new listings (except a profile's
   first-ever run, which is a silent baseline) or price drops ≥ `min_price_drop_pct`.
6. **Removal check** (only if all searches were complete): active listings no search
   returned are fetched individually. Gone → `removed`; still online → `unmatched`.
7. Run counts and errors go to `collection_runs`.

`reassess(db, config)` recomputes assessments for every stored listing without the
network.

## db.py

Schema and all SQL writes. See [data-model.md](data-model.md) for tables and statuses.

- `upsert_listing` is the heart of history-keeping: insert → snapshot `new`; update →
  snapshot `changed` (price or mileage differ) or `reactivated` (was not active);
  otherwise no snapshot. A search result never blanks detail-only fields
  (`DETAIL_ONLY_FIELDS`) or overwrites the stored detail payload.
- `set_status` marks `removed`/`unmatched` with a snapshot.
- `row_to_listing` rebuilds a `Listing` from a row (used by `reassess`).
- `_migrate` adds columns introduced after a database was created. **When adding a
  column**, add it to `SCHEMA` *and* to `_migrate`, so existing databases keep working.

## notify.py

`format_alert` renders an alert as plain text: price (or drop), tier, confidence, title,
specs, identified generation/engine, every non-tier flag, profiles and link.
`send_alerts` posts to Telegram when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are
set (batched under Telegram's length limit), else prints. Errors are logged without
the URL, because it contains the bot token.

## report.py

Read-only text reports:

- `shortlist_report`: active listings ordered by best profile priority, then confidence,
  then number of cautions, then price. A transparent ordering, deliberately not a score.
- `changes_report`: snapshots in the last N days, grouped by event, with old → new price.
- `history_report`: one listing's specs, assessment and full snapshot history.
- `market_report`: asking-price quartiles, median year and mileage per generation and
  engine, plus the last five runs.

---

## tests/

Run with `uv run pytest`. No test touches the network.

| File | Covers |
|---|---|
| `test_pipeline.py` | Full runs against a `FakeCollector` and in-memory DB: baseline without alerts, price-drop history and alerts, removal vs unmatched, failed search skipping removals, remembering detail rejections, details not erased by later search results. |
| `test_filters_and_parsing.py` | Sauto item parsing, search parameters, plain filters, keyword word-boundary matching, and the collector's model guard and query cache (via `httpx.MockTransport`). |
| `test_classify_evaluate.py` | Classification against the **real** `config/catalog.yaml` (changeover years, power, title hints, turbo keywords), profile routing, `avoid` handling, profile resolution, and assessment flags/confidence. |

Because `test_classify_evaluate.py` uses the real config, catalog edits can break it.
That's intended: it shows what an edit changes. Update the expectations if the change
was deliberate.

## deploy/

`auto-hunter.service` (oneshot `uv run auto-hunter collect`) and `auto-hunter.timer`
(every 3 hours with a random delay of up to 10 minutes, catching up after downtime). Adjust `User`
and paths before installing; steps are in the main README.
