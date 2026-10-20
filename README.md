# auto-hunter

Personal used-car monitor for the Czech market. It regularly searches
[sauto.cz](https://www.sauto.cz), keeps every listing's price history in SQLite,
and alerts you (Telegram or console) about new listings and price drops.

This is the MVP (phase 1): data collection and history, no AI yet. Later phases
add AI listing analysis, market valuation and ranking.

## Setup

```bash
uv sync
cp .env.example .env          # optional: Telegram credentials
```

Edit [config/search_profiles.yaml](config/search_profiles.yaml) to describe the cars
you're interested in. Tune request pacing and limits in [config/settings.yaml](config/settings.yaml).

## Usage

```bash
uv run auto-hunter collect              # fetch, store changes, send alerts
uv run auto-hunter collect --profile golf-vii
uv run auto-hunter report --days 7      # new / changed / removed listings
uv run auto-hunter market               # asking-price spread per profile and year
uv run auto-hunter history 211125480    # price history of one listing
```

The first `collect` for a profile builds the baseline and sends no "new listing"
alerts; later runs alert on genuinely new listings and on price drops of at least
`alerts.min_price_drop_pct`.

## How it works

1. **Search** – each profile's make/model, price, mileage, fuel, gearbox and body
   filters are sent to Sauto's JSON API (`/api/v1/items/search`, allowed by robots.txt).
2. **Local filters** – year range and title keywords (e.g. "havarovaný") are applied
   locally; the API has no year filter.
3. **Details** – newly seen listings get one detail request (description, VIN, power,
   body, service book). Listings whose details don't fit (e.g. wrong body type) are
   stored as `unmatched` so they aren't fetched again.
4. **History** – `listings` holds each listing's current state and the original source
   payload (`raw_json`); `listing_snapshots` gets a row whenever price, mileage or
   status changes.
5. **Removals** – after a complete, error-free run, tracked listings that no longer
   appear are checked individually: gone → `removed`; still online but outside the
   filters → `unmatched`. A removed listing is not necessarily sold: it may have been
   withdrawn or relisted.

The collector is deliberately slow (1.5 s between requests by default) and identifies
itself in the User-Agent. Keep it that way, and check Sauto's terms of use before
increasing the frequency.

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
