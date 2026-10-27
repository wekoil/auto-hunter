# Configuration

Everything about *what* to look for is in `config/`. Files are loaded and validated
together by `config.load_config`; a typo (unknown engine id, bad regex, profile over
budget) fails at startup with a message naming the problem.

| File | Layer | Changes when… |
|---|---|---|
| `requirements.yaml` | 1. Global requirements | your general needs change (budget, fuel, body) |
| `profiles.yaml` | 2. Vehicle profiles | you add/drop a model or engine family to watch |
| `evaluation.yaml` | 3. Evaluation rules | you change how cars are labelled |
| `catalog.yaml` | Knowledge base | you learn something about a generation or engine |
| `sources.yaml` | Marketplaces | you enable/disable a site or map a model onto it |
| `settings.yaml` | Technical | you change pacing, limits, database location |

Values such as fuel or body use **Sauto's SEO names**, the slugs in sauto.cz URLs:
fuel `benzin`, `nafta`, `hybridni`, `lpg-benzin`; gearbox `manualni`, `automaticka`;
body `kombi`, `hatchback`, `liftback`, `sedanlimuzina`, `suv`, `mpv`. Model keys are
Sauto's too (`skoda:octavia`). Other sources translate to these names via
`sources.yaml`, so profiles and the catalog never mention a specific site.

---

## requirements.yaml: global requirements

Inherited by every profile.

| Key | Meaning |
|---|---|
| `fuel` | Allowed fuels. Sent to the API as a filter. Currently petrol and petrol hybrids. |
| `body` | Allowed body styles. Note Sauto tags some MPVs (Touran, Roomster) as `kombi` too, which is why profiles name models explicitly. |
| `price_max` | Overall budget in CZK. A profile may set a lower one, never higher. |
| `mileage_max` | Hard mileage cap. Keep it above your *preferred* limit (in evaluation.yaml) so good higher-mileage cars are flagged, not hidden. |
| `exclude_keywords` | Words that drop a listing when found in its title. Matching ignores case and diacritics and only matches at a word start, so `havarovan` matches "Havarovaný" but not "nehavarovaný". Descriptions are not searched (they often say "nehavarovaný"). |
| `exclude_sellers` | Dealers you won't buy from (e.g. `aaa auto`). Matched as a case/diacritics-insensitive substring of the seller name; private sellers and Bazoš have none, so they're never affected. A profile may add its own. |

## profiles.yaml: vehicle profiles

Each profile is one kind of car you'd consider. Profiles may overlap; a car matching
several is stored once and listed with all its profiles.

| Key | Meaning |
|---|---|
| `name` | Unique id, shown in alerts and reports. Renaming a profile makes it "new" (its first run is a silent baseline). |
| `priority` | `high`, `medium` or `exploratory`. Orders the shortlist. |
| `generations` | Catalog generation ids. Determines which Sauto models are searched and the default year range. A car must *plausibly* be one of them. |
| `engines` | Catalog engine ids. If set, cars whose specs rule out all of these are dropped; cars with unknown/unclear engines stay, flagged. Omit to accept any engine. |
| `models` | Extra Sauto `make:model` keys searched without generation logic (used by exploratory profiles). |
| `year_min`, `year_max` | Override the generations' year span. |
| `body` | Override the global body styles, e.g. `[kombi]` for models with no liftback. |
| `price_max`, `price_min`, `mileage_max` | Override the global values (within budget). |
| `gearbox` | Restrict gearbox, e.g. `[manualni]`. Omit for either. |
| `exclude_keywords`, `exclude_sellers` | Added to the global lists. |

### Current profiles

| Profile | Searches | Engines |
|---|---|---|
| A-octavia3-1.6mpi | Octavia III, estate or liftback | 1.6 MPI (EA211) |
| A2-octavia-older-1.6mpi | Octavia II, Octavia I Tour (2004+) | 1.6 MPI (EA113) |
| B-golf-variant-1.6mpi | Golf V/VI/VII Variant | 1.6 MPI (EA113, EA211) |
| C-golf-variant-1.4tsi | Golf VII Variant | 1.4 TSI (EA211 only) |
| D-octavia3-other-petrol | Octavia III, estate or liftback | 1.0/1.2/1.4/1.5 TSI (EA211), 1.8 TSI |
| E-focus-estate | Focus II/III/IV kombi, 2008+ | 1.6 Ti-VCT, 1.0/1.5/1.6 EcoBoost |
| G-i30-ceed-1.6gdi | i30 GD kombi, cee'd JD SW | 1.6 GDI |
| H-superb | Superb II/III, estate or liftback | 1.8 TSI (EA888), 1.4/1.5 TSI (EA211) |
| I-passat-variant | Passat B6/B7/B8 Variant | 1.6 MPI (EA113), 1.8 TSI (EA888), 1.4/1.5 TSI (EA211) |
| J-mazda6 | Mazda 6 GH/GJ, estate or liftback | any (MZR, Skyactiv-G) |
| K-toyota-estate | Auris II TS, Avensis T27, Corolla TS | any, hybrids included |
| L-leon-st | Seat Leon III/IV ST | 1.6 MPI, 1.0/1.2/1.4/1.5 TSI (EA211), 1.8 TSI |
| M-rapid-toledo | Škoda Rapid, Seat Toledo IV (liftback, no Spaceback) | 1.0/1.2/1.4 TSI (EA211), 1.6 MPI |
| N-honda-tourer | Civic IX Tourer, Accord VIII Tourer | any (i-VTEC) |
| O-logan-mcv | Dacia Logan MCV I/II | any |
| P-i40-cw | Hyundai i40 CW | 1.6 GDI, 2.0 MPI/GDI |
| F-alternatives | i30, cee'd, Astra (2010+) | any (all flagged unvetted) |

## evaluation.yaml: evaluation rules

Rules here **never hide a car**; they only label it.

| Key | Meaning |
|---|---|
| `price_tiers` | Bands by `max` price. A car gets the first tier it fits. The `note` becomes a flag. |
| `preferred_mileage_max` | Above this, a `mileage_high` caution is added. |
| `medium_confidence_max_uncertainties` | Confidence is `high` with no uncertainties, `medium` up to this many, `low` beyond. |

Uncertainties are: unclear generation, unclear/unknown/uncataloged engine, missing VIN,
service history not stated. Known risks (caution engine, high mileage, no service book)
are flags but don't lower confidence. They are information, not uncertainty.

After editing this file, run `auto-hunter reassess` to re-label stored listings.

## catalog.yaml: the knowledge base

### Generations

```yaml
octavia-3:
  label: Škoda Octavia III (5E)
  models: [skoda:octavia]          # Sauto model keys it's listed under
  years: [2013, 2020]              # estate manufacturing years; null = still produced
  title_hints: ['octavia,?\s*(iii|3)\b(?![.,]\d)', '\b5e\b']
  engines: [ea211-1.6-mpi, ...]    # engines this generation was sold with
  vin_codes: [NE]                  # optional: VW-group type code, VIN characters 7-8
```

- **years** of adjacent generations deliberately overlap in changeover years. The
  classifier then uses title hints and engine specs to tell them apart.
- **title_hints** are regexes matched against the lowercased, diacritics-free title +
  variant. The `(?![.,]\d)` guard stops "Octavia 2.0 TSI" counting as "Octavia 2".
- **engines** matter for classification even if no profile wants them: listing the
  Octavia II's engines is what lets a 2013 1.6 MPI 75 kW be recognised as an Octavia II.

### Engines

```yaml
ea211-1.6-mpi:
  label: 1.6 MPI (EA211)
  liters: 1.6               # vs. displacement rounded to 0.1 l, or "1.6" in the title
  power_kw: [81]            # every output sold; matched ±3 kW
  turbo: false              # vs. title keywords (TSI/EcoBoost → turbo; MPI/Ti-VCT → not)
  status: preferred
  notes: ...                # shown in caution flags
  automatic_note: ...       # extra caution when the car is an automatic (e.g. DSG DQ200)
```

| Status | Effect |
|---|---|
| `preferred`, `ok` | No flag. |
| `caution` | Caution flag with the notes. |
| `unvetted` | Info flag "reliability not yet researched". |
| `avoid` | Car rejected when every engine it could be is `avoid`; if it only *might* be one, kept with "Could be X, which you've excluded. Confirm the engine code". |

The notes are research starting points, not verified facts.

### How a car is classified

1. Take the catalog generations for the car's Sauto model.
2. Keep those whose years include the car's year.
3. If the VIN's type code (characters 7-8) matches some generations' `vin_codes`, keep
   only those. This settles changeover years: a 2013 Octavia is `1Z` (II) or `NE` (III).
4. If any title hint matches, keep only the hinted generations.
5. Form every (generation, engine) pair; drop pairs whose engine contradicts the known
   displacement, power or turbo keyword.
6. One pair left → identified. Several → unclear (flagged). None but specs known →
   "engine not in catalog" (dropped by engine-restricted profiles).

---

## Recipes

**Exclude an engine you never want.** Set its `status: avoid` in catalog.yaml, then
`auto-hunter reassess`. The next `collect` moves already-stored matches to `unmatched`.

**Record research about an engine.** Edit its `status` and `notes`, then `reassess`.

**Add an engine to a profile.** If it's in the catalog, add its id to the profile's
`engines`. If not, add it under `engines:` in the catalog *and* to each generation's
`engines` list that had it.

**Add a new model with generation logic.** Find the Sauto model key in the website
URL (`sauto.cz/inzerce/osobni/<make>/<model>`); some models have two keys (e.g.
`golf` and `golf-variant`), so check both. Add generations (and engines) to the
catalog, then a profile referencing them. A wrong model key fails loudly: Sauto would
otherwise silently return the whole make, and the collector checks for that. To search
the model on Škoda Plus and Bazoš too, add it under their `models:` in sources.yaml.

**Turn a marketplace off.** `enabled: false` under it in sources.yaml, or run
`collect --source sauto` to collect from one source once.

**Watch a model without researching it.** Add its key to a profile's `models` (like
F-alternatives). Every car is flagged "not in catalog yet".

**Change the budget.** `price_max` in requirements.yaml, and the tiers in evaluation.yaml.

**Allow only manual gearboxes for one profile.** Add `gearbox: [manualni]` to it.

## sources.yaml: marketplaces

Each top-level key is a source; `enabled: false` turns it off. If the file is missing,
only Sauto runs. A model key a source doesn't map is simply not searched there.

### sauto

No options besides `enabled`. Sauto's model keys are the canonical ones.

### skodaplus

```yaml
models:
  skoda:octavia: {make: Škoda, models: [Octavia, Octavia Combi]}
```

Names as skodaplus.cz shows them (matched ignoring case and diacritics). They are
resolved to the API's internal ids at the start of each run; a name that doesn't
exist logs a warning. Only Czech stock is requested, because Slovak stock is priced in EUR.
Škoda Plus is mostly certified dealer stock, often above this budget, so expect
few matches but very complete data (VIN, accident flag, service book).

### dasweltauto

```yaml
models:
  volkswagen:golf-variant: {brand: vw, models: [vw-golf-variant, vw-golf-variant-8-fl]}
```

Brand and model values from dasweltauto.cz's filter (visible in the API's facet lists).
Each model is one request: the API ignores `models` without `brands`, and mixes up
several models in one request. Includes non-VW brands traded in at VW-group dealers,
and the VW-group stock of groups such as Louda and Klokočka.

### louda

```yaml
models:
  hyundai:i30: {make: Hyundai, models: [I30, I30 COMBI]}
```

Make and model names exactly as autolouda.cz's filter lists them (upper case models).
An unknown name returns no cars, not an error, so check the filter if a model never
matches. No VINs; duplicates on other sites are found by fingerprint.

### klokocka

```yaml
models:
  skoda:octavia: {brand: skoda, model: octavia}
```

The `znacka:` and `model:` parts of autoklokocka.cz URLs. Two model keys may share
a path (Golf and Golf Variant both use `volkswagen/golf`; the body filter tells
them apart). Cards lack power, so each new car costs one detail request. Klokočka's
stock is mostly newer and above this budget.

### bazos

| Key | Default | Meaning |
|---|---|---|
| `max_pages_per_category` | 40 | Pages of 20 read per brand category per run. Also how deep the very first crawl goes. |
| `recheck_after_days` | 2 | Tracked listings not seen for this long are re-fetched to catch price changes and removals. |
| `models` | | `{category: <bazos brand slug>, title: <regex>}` per model key. |

```yaml
models:
  volkswagen:golf-variant: {category: volkswagen, title: '\bgolf\b.*\bvariant\b'}
  volkswagen:golf:         {category: volkswagen, title: '\bgolf\b(?!\s*(plus|sportsvan|\+))'}
```

- The category is the slug in the URL (`auto.bazos.cz/volkswagen/`).
- The title regex runs on the lowercased, diacritics-free title. Within a category
  the **first** matching entry wins, so put specific entries (Golf Variant) before
  general ones (Golf).
- Bazoš has no body field. A listing is accepted as an estate only if its text says
  so (combi, kombi, variant, SW, touring, tourer, caravan...). One that doesn't say
  is skipped when a profile requires a body type. Most estate ads do say it.
  Exception: an entry's optional `bodies` lists every body the model comes in
  (Octavia, Superb: `[kombi, liftback]`); if the profile accepts all of them, a
  listing that doesn't say is kept. `body_hints` maps a body to a title regex for
  model-specific names that `textspecs` can't know (`Leon ST`, `Logan MCV`).
- Listings with a Slovak postcode are skipped.

How the incremental crawl works and what it can't see is described in
[code-reference.md](code-reference.md#collectorsbazospy).

## settings.yaml

| Key | Default | Meaning |
|---|---|---|
| `database_path` | `data/auto_hunter.db` | SQLite file (created on first run; git-ignored). |
| `http.user_agent` | `auto-hunter/0.1 (...)` | Identifies the tool honestly. |
| `http.request_delay_s` | 1.5 | Minimum gap between requests. Don't lower it casually. |
| `http.timeout_s`, `http.max_retries` | 20, 3 | Retries cover network errors, 429 and 5xx, with exponential backoff. |
| `collection.page_size` | 100 | Results per search page. |
| `collection.max_results_per_query` | 2000 | Safety cap per model query; hitting it disables removal detection that run. |
| `collection.max_detail_fetches` | 300 | Detail requests per source per run; the rest wait for the next run. |
| `collection.max_removal_checks` | 200 | Requests per source per run confirming vanished (or, for Bazoš, stale) listings. |
| `alerts.min_price_drop_pct` | 3 | Smallest price drop that triggers an alert. |

Telegram credentials go in `.env` (see `.env.example`), never in config files.
