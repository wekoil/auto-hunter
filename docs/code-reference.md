# Code reference

File by file, in the order data flows through them. Each module also has a docstring
at the top; this page explains how they connect and where to make common changes.

```
src/auto_hunter/
  cli.py              command-line entry point
  config.py           load + validate config/*.yaml
  models.py           Listing: the normalized advertisement
  collectors/
    __init__.py       build_collectors(): sources.yaml → collector instances
    base.py           Collector interface (full-scan vs incremental)
    http.py           PoliteClient: pacing, retries, User-Agent
    sauto.py          Sauto.cz (JSON API)
    skodaplus.py      Škoda Plus (GraphQL API)
    bazos.py          Bazoš (HTML, incremental crawl)
  textspecs.py        specs from free text (used by Bazoš)
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
| `collect [--source S] [--profile X] [--no-alerts]` | `pipeline.collect` per source, then `notify.send_alerts` once | yes |
| `shortlist [--limit N] [--profile X]` | `report.shortlist_report` | no |
| `report [--days N]` | `report.changes_report` | no |
| `history <id>` | `report.history_report` | no |
| `market` | `report.market_report` | no |
| `reassess` | `pipeline.reassess` | no |

`collect` exits with status 1 only if every source had errors *and* saw nothing, so a
systemd timer surfaces total failures without failing on a single flaky request or
one site being down.
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

- `damaged`: the source reports previous accident damage (Škoda Plus `crashed`).

`None` always means "not stated", never "no"; filters and evaluation rely on that.
`is_real_vin` tells a full VIN from a masked or partial one; only real VINs are used
to match the same car across sources.

## collectors/base.py

The `Collector` interface: `search(profile) → SearchResult` and
`fetch(source_id) → Listing | None` (None = gone). `CollectorError` means "this source
call failed"; the pipeline records it and moves on. Class attributes describe how the
source behaves:

| Attribute | Meaning |
|---|---|
| `full_scan` | True: a search returns *all* matches, so a missing listing is suspicious (removal check after complete searches). False: a search returns only what's new (Bazoš); tracked listings are re-checked after `recheck_after_days`. |
| `summary_is_authoritative` | False: search results are less reliable than detail pages (Bazoš snippets), so once details are stored a re-seen listing only updates price and title. |
| `state` | A dict the collector may change during a run; the pipeline loads it from and saves it to the `source_state` table. |

`SearchResult.complete` is False when the collector knows it didn't see everything.

## collectors/__init__.py

`build_collectors(config, only)` instantiates the enabled sources from sources.yaml.
**To add a marketplace:** write `collectors/<name>.py`, add a factory entry here and a
section in sources.yaml.

## collectors/http.py

`PoliteClient`, shared by all collectors: waits `request_delay_s` between requests,
retries network errors / 429 / 5xx with exponential backoff, returns other responses
for the collector to interpret, and raises `CollectorError` when retries run out.

## collectors/sauto.py

Sauto.cz via its undocumented JSON API (allowed by robots.txt).

- `search` runs one query per model in the profile and dedupes by id.
- `_query` pages through results and **caches per run** by exact parameters, so
  profiles sharing a model (A and D both search the Octavia) cost one query. It also
  raises if results come back for a different model, since Sauto silently ignores
  unknown model names.
- `build_search_params` maps profile fields to API filters (`price_to`,
  `tachometer_to`, `fuel_seo`, `gearbox_seo`, `vehicle_body_seo`, used cars only).
- `parse_item` converts search and detail items into `Listing`. Year falls back to
  `in_operation_date` (dealer feeds often lack `manufacturing_date`); "price by
  agreement" becomes `price_czk=None`; dealer vs private is inferred from `premise`.

## collectors/skodaplus.py

Škoda Plus (Škoda dealer network, all makes) via the GraphQL endpoint its website uses
(`POST /graphql`; robots.txt allows everything). Full scan.

- `_resolve` runs once per run: looks up make, model, fuel, body and gearbox ids by
  name (`carMakes`, `carModels`, `codeBook`), using the names in sources.yaml.
- `_build_filter` always sets `regionCountry: CZ` and `isNew: false`. Without the
  country filter Slovak cars appear with EUR prices that would look absurdly cheap.
- `_query` pages with `pageInfo.endCursor` (the API's `totalCount` errors server-side)
  and is cached per run like Sauto's.
- Search results already contain everything (VIN, cc, kW, `crashed`, service book), so
  listings come back with `has_details=True` and no detail requests are made.
- `parse_car` maps their codebook values back to ours (`DSG`/`Automatická` →
  `automaticka`, `Kombi` → `kombi`) and `crashed` to `Listing.damaged`.

## collectors/bazos.py

Bazoš via HTML. robots.txt disallows its search and filter URLs, so only brand
category pages (`/skoda/`, `/skoda/20/`...) and listing pages are read.

**Incremental crawl.** Category pages are ordered by last bump, and paid "TOP"
listings are re-bumped constantly (about 2,000 a day for Škoda, against about 700 new
ads). A full read is impossible (31,000 Škodas), so `_crawl` reads newest-first and
stops after **two consecutive pages whose ids were all seen in previous crawls**. Those
ids live in `state["seen"][category]` (the last 5,000). Two pages, because a single page
of only re-bumped known listings is plausible, while two in a row means we've reached
the previous crawl. The first crawl stops at `max_pages_per_category`.

**What it can't see:** listings older than the first crawl's depth that are never
bumped. Removals and price changes of tracked listings are caught by the pipeline's
stale-listing recheck (`full_scan = False`).

- `parse_list_page` extracts id, title, snippet, price, city, postcode, date and TOP
  marker from each `inzeraty inzeratyflex` block.
- `search` keeps listings whose title matches a sources.yaml model regex, with a Czech
  postcode, an estate keyword when the profile requires a body, and no conflicting fuel.
- `fetch` reads `/inzerat/<id>/inzerat.php` without following redirects. A deleted
  listing redirects; a live one renders even with a wrong slug (HTTP 404 + content),
  so "gone" = redirect or no `popisdetail` element. The canonical link gives the real URL.
- All specs come from `textspecs.extract`, so they are less certain than structured
  fields. Classification and flags treat them the same way.

## textspecs.py

Extracts year, mileage, power, displacement, fuel, gearbox, body, VIN and service-book
mentions from free text. Each extractor prefers labelled values ("rok výroby: 2016",
"najeto 145 tis. km", "Palivo: Benzín") and returns None when ambiguous. Guards against
traps found in real ads:

| Trap | Handling |
|---|---|
| "STK 09/2027", "servis 03/2024" | dates after STK/servis/do/záruka aren't the car's year |
| "145000 km 2016" | a bare "km" label needs a colon |
| "automatická/manuální klimatizace" | air-con doesn't set the gearbox |
| "6 st. manuál" | no bare "st" body keyword (would make every car an estate) |
| "ev. č." | not electric |
| petrol and diesel both mentioned | fuel None |
| masked VIN "WVWZZZXXXXXXXX259" | ignored |

Displacement is read from the title only, because descriptions contain many other decimals.

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
| `damage_reported` | caution | no |
| `also_listed` (same VIN active on another source, with its price and link) | info | no |
| `price_missing`, `tier_<label>` | info | no |

Service and VIN flags are only raised after a detail fetch, because search results
never contain them. **To add a rule:** add a `flag(...)` call in `assess`, and add its
code to `UNCERTAINTY_CODES` if it represents missing information.

## pipeline.py

`collect(db, collector, config, profiles=None)` is one run of **one source**; the CLI
calls it per source. Per-run state lives in a small `_Run` helper (`store`,
`mark_unmatched`, `matching_profiles`, `fetch`), shared by the main loop and the
recheck paths. Steps:

0. Load the collector's `state` from the database (saved again at the end).
1. **Search** every profile. Errors are recorded and the run continues;
   `complete` tracks whether removal detection is safe.
2. **Pre-filter** each result with classification from title/year. Survivors are
   gathered per listing with the profiles they matched.
3. **Details**: known listings reuse stored detail fields (for non-authoritative
   sources, the stored listing is kept and only price and title are updated). New ones get one
   detail request (within `max_detail_fetches`) unless the search result already has
   details (Škoda Plus). Then classify and filter again, because the detail data
   (power, cc, body) often settles the engine.
4. **Rejected after details** → stored once as `unmatched`, so its details are never
   paid for again.
5. **Matched** → `upsert_listing` (state + snapshot if changed), `link_profile`,
   `assess` (with any same-VIN listings on other sources) + `save_assessment`, and an
   `Alert` for new listings (except a profile's first run *on that source*, which is a
   silent baseline) or price drops ≥ `min_price_drop_pct`.
6. **Tracked listings not returned by the search**:
   - full-scan source, all searches complete: `_check_vanished` fetches each one.
     Gone → `removed`; still online → `unmatched`.
   - incremental source: `_recheck_stale` fetches those not seen for
     `recheck_after_days`. Gone → `removed`; still online → stored again (price changes
     recorded and alerted) if it still matches a profile, else `unmatched`.
7. Run counts and errors go to `collection_runs`, the collector state to `source_state`.

`reassess(db, config)` recomputes assessments for every stored listing without the
network.

## db.py

Schema and all SQL writes. See [data-model.md](data-model.md) for tables and statuses.

- `upsert_listing` is the heart of history-keeping: insert → snapshot `new`; update →
  snapshot `changed` (price or mileage differ) or `reactivated` (was not active);
  otherwise no snapshot. A search result never blanks detail-only fields
  (`DETAIL_ONLY_FIELDS`) or overwrites the stored detail payload.
- `set_status` marks `removed`/`unmatched` with a snapshot.
- `active_listings(source, seen_before)` lists tracked listings, optionally only stale ones.
- `same_vehicle(listing_id, vin)` finds the same car on other sources.
- `load_state` / `save_state` persist each collector's `state` dict.
- `row_to_listing` rebuilds a `Listing` from a row (used by `reassess` and for
  non-authoritative sources).
- `_migrate` adds columns introduced after a database was created. **When adding a
  column**, add it to `SCHEMA` *and* to `_ADDED_COLUMNS`, so existing databases keep
  working.

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
| `test_pipeline.py` | Full runs against fake collectors and an in-memory DB: baseline without alerts, price-drop history and alerts, removal vs unmatched, failed search skipping removals, remembering detail rejections, details not erased by later search results; incremental sources (absence isn't removal, stale recheck finds removals and price drops, snippets don't overwrite detail specs), per-source baselines, same-VIN flag, persisted collector state. |
| `test_filters_and_parsing.py` | Sauto item parsing, search parameters, plain filters, keyword word-boundary matching, and the collector's model guard and query cache (via `httpx.MockTransport`). |
| `test_sources.py` | Bazoš list/detail parsing (markup copied from the site), model/country/body/fuel filtering, model order, the crawl stop rule and first-crawl cap, gone vs live detection; Škoda Plus parsing, id resolution, Czech-only filter and paging. |
| `test_textspecs.py` | Free-text extraction against real ad phrasing, including every trap listed above. |
| `test_classify_evaluate.py` | Classification against the **real** `config/catalog.yaml` (changeover years, power, title hints, turbo keywords), profile routing, `avoid` handling, profile resolution, and assessment flags/confidence. |

Because `test_classify_evaluate.py` uses the real config, catalog edits can break it.
That's intended: it shows what an edit changes. Update the expectations if the change
was deliberate.

## deploy/

`auto-hunter.service` (oneshot `uv run auto-hunter collect`) and `auto-hunter.timer`
(every 3 hours with a random delay of up to 10 minutes, catching up after downtime). Adjust `User`
and paths before installing; steps are in the main README.
