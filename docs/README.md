# auto-hunter documentation

auto-hunter watches used-car listings on Sauto.cz, Škoda Plus and Bazoš for the cars
described in `config/`,
keeps every listing's price history, labels each car (generation, engine, risks,
confidence) and alerts you about new listings and price drops.

| Document | Read it when you want to… |
|---|---|
| [configuration.md](configuration.md) | change what cars are searched, add a model/engine, tune the rules |
| [code-reference.md](code-reference.md) | understand or change the code, file by file |
| [data-model.md](data-model.md) | query the database or understand statuses and history |

Setup, commands and scheduling are in the [main README](../README.md).

## How the pieces fit

```
config/*.yaml ──► config.py ──► Config (settings, requirements, catalog, evaluation, profiles)
                                   │
cli.py collect ──► for each enabled source in sources.yaml: pipeline.collect()
                     │
                     ├─ collectors/<source>   search each profile's models
                     │    sauto.py      JSON API          skodaplus.py  GraphQL API
                     │    bazos.py      HTML + textspecs.py (specs parsed from free text)
                     ├─ classify.py           which generation/engine could this be?
                     ├─ filters.py            does it fit the profile? (cheap, local)
                     ├─ collectors/<source>   fetch details for new survivors, re-check
                     ├─ db.py                 store state + history snapshot + profile links
                     ├─ evaluate.py           price tier, flags, confidence → assessment
                     └─ removal / recheck     full scan: vanished → check; Bazoš: stale → check
                   then notify.py             Telegram / console alerts from all sources
cli.py shortlist|report|market|history ──► report.py ──► db.py (read only)
```

## Design principles

These come from the project brief and explain most decisions in the code.

- **Cheap filters first.** Search filters go to the API; year, generation, engine and
  keywords are checked locally; detail requests are spent only on survivors, once.
  The same rule will govern future AI analysis.
- **Unknown is not bad.** A missing field never rejects a car. It becomes a flag and
  lowers confidence. A car is dropped only when its data *rules it out*.
- **Never guess.** If a 2013 "1.4 TSI" could be EA111 or EA211, it's reported as
  unclear, with a question to ask the seller, not silently assigned one.
- **Keep the raw data.** The original source payload is stored, and derived labels
  (assessments) can be recomputed with `auto-hunter reassess` after rules change.
- **Explanations, not scores.** Evaluation produces readable flags. A weighted score
  needs market comparables and researched reliability data (later phases).
- **Knowledge lives in YAML.** Which engines exist, which you avoid and why is
  in `config/catalog.yaml`, so research results don't require code changes.
- **Advertised ≠ sold.** Prices are asking prices; a removed listing may have been
  sold, withdrawn or relisted.
- **Respect the sources.** Only official/public endpoints or pages robots.txt allows,
  paced requests, an honest User-Agent. Where a site disallows search (Bazoš), we accept
  less coverage rather than work around it.

## Roadmap position

This is phase 1 (data collection) plus part of the phase 2 groundwork (classification
and rule-based evaluation). Next: AI extraction from descriptions, photo analysis,
statistical market valuation, then validation against your own inspections.
