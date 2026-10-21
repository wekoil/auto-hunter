# auto-hunter

Personal used-car monitor for the Czech market. It regularly searches six marketplaces
and dealer networks, keeps every listing's price history in SQLite, and alerts you
(Telegram or console) about new listings and price drops.

| Source | Access | Data | Coverage |
|---|---|---|---|
| [Sauto](https://www.sauto.cz) | public JSON API | structured, VIN often | full scan each run |
| [Škoda Plus](https://www.skodaplus.cz) | public GraphQL API (Czech stock only) | structured, VIN, accident flag | full scan each run |
| [Das WeltAuto](https://www.dasweltauto.cz) | public JSON API | structured, VIN | full scan each run |
| [Louda Auto](https://autolouda.cz) | catalogue JSON API | structured, no VIN | full scan each run |
| [Autosalon Klokočka](https://autoklokocka.cz) | server-rendered pages | cards + detail pages, no VIN | full scan each run |
| [Bazoš](https://auto.bazos.cz) | category pages (search URLs are disallowed by robots.txt) | free text, parsed | incremental: new and bumped listings |

Dealer stock is widely mirrored onto Sauto, so the same car often appears twice. Matches
are flagged by VIN, or, for dealers that publish none, by an exact model + year +
mileage fingerprint. TipCars is not collected: it refuses automated clients (HTTP 403)
and its robots.txt disallows the Czech section.

This is the MVP (phase 1): data collection and history, no AI yet. Later phases
add AI listing analysis, market valuation and ranking.

Full documentation is in [docs/](docs/README.md): [configuration](docs/configuration.md),
[code reference](docs/code-reference.md) and [data model](docs/data-model.md).

## Setup

```bash
uv sync
cp .env.example .env          # optional: Telegram credentials
```

## Configuration

The config is split into three layers plus a knowledge base:

| File | Layer | What it holds |
|---|---|---|
| [requirements.yaml](config/requirements.yaml) | Global requirements | Petrol, estate, budget, hard mileage cap, excluded keywords |
| [profiles.yaml](config/profiles.yaml) | Vehicle profiles | Which generations/engines to look for, priority |
| [evaluation.yaml](config/evaluation.yaml) | Evaluation rules | Price tiers, preferred mileage, confidence thresholds |
| [catalog.yaml](config/catalog.yaml) | Knowledge base | Generations (years, title hints, engines) and engines (specs, reliability status, notes) |

[sources.yaml](config/sources.yaml) enables marketplaces and maps model names onto
each one; [settings.yaml](config/settings.yaml) holds request pacing, limits and the
database path.

No marketplace can filter by generation or engine family, so each listing is classified
locally: the catalog lists which (generation, engine) pairs exist for a model, and the
listing's year, title hints, displacement, power and turbo keywords eliminate the ones
that don't fit. If one pair remains, the car is identified; if several do, it is
flagged as unclear (e.g. "1.4 TSI EA111 or EA211") rather than guessed.

Profiles only drop a car when its specs rule it out. Unknown engines are kept
and flagged. Evaluation never hides anything: it adds a price tier, flags (cautions such
as an EA111 engine or high mileage, uncertainties such as a missing VIN) and a confidence
level. After editing the catalog or evaluation rules, run `auto-hunter reassess`.

## Usage

```bash
uv run auto-hunter collect              # fetch from all sources, store changes, alert
uv run auto-hunter collect --source bazos --profile A-octavia3-1.6mpi
uv run auto-hunter shortlist            # active listings, most promising first
uv run auto-hunter report --days 7      # new / changed / removed listings
uv run auto-hunter market               # asking-price spread per generation and engine
uv run auto-hunter history 211125480    # price history and assessment of one listing
uv run auto-hunter reassess             # re-label stored listings after config edits
uv run auto-hunter browse               # look through the database at http://localhost:8080
```

`browse` opens a local, read-only web viewer: a filterable and sortable listings table,
a page per listing with photos, price history chart and the original description, and
an overview of market prices, recent changes and collection runs. It only listens on
this computer unless you pass `--host`.

The first `collect` for a profile on each source builds the baseline and sends no
"new listing" alerts; later runs alert on genuinely new listings and on price drops of
at least `alerts.min_price_drop_pct`. The same car on two sites is flagged with the other
listing's price.

## How it works

1. **Search** – each source is collected in turn. Structured sources get the profile's
   make/model, price, mileage, fuel, gearbox and body filters server-side where they
   support them. Bazoš brand categories are read newest-first until the previous
   crawl's position, and specs are parsed from the text.
2. **Local filters and classification** – year range, title keywords (e.g. "havarovaný"),
   generation and engine are checked locally. Identical queries from several profiles
   are fetched once.
3. **Details** – newly seen listings get one detail request (description, VIN, power,
   body, service book), then are re-classified with the better data. Listings whose
   details don't fit (e.g. wrong engine) are stored as `unmatched` so they aren't
   fetched again.
4. **History** – `listings` holds each listing's current state and the original source
   payload (`raw_json`); `listing_snapshots` gets a row whenever price, mileage or
   status changes; `listing_assessments` holds the derived labels.
5. **Removals** – full-scan sources: after a complete, error-free run, tracked listings
   that no longer appear are checked individually: gone → `removed`; still online but
   outside the filters → `unmatched`. Bazoš: listings not seen for
   `recheck_after_days` are re-fetched. A removed listing is not necessarily sold: it
   may have been withdrawn or relisted.

Collectors are deliberately slow (1.5 s between requests by default), identify
themselves in the User-Agent and respect each site's robots.txt. Keep it that way,
and check each site's terms of use before increasing the frequency.

## Scheduling (Ubuntu)

Copy the units from [deploy/](deploy/), adjust the paths and user, then:

```bash
sudo cp deploy/auto-hunter.service deploy/auto-hunter.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now auto-hunter.timer
journalctl -u auto-hunter.service -f
```

The timer runs every 3 hours, which is plenty to catch price changes.

## Development

```bash
uv run pytest
```

Collectors implement `Collector` in [src/auto_hunter/collectors/base.py](src/auto_hunter/collectors/base.py)
and convert source data into the shared `Listing` model.
