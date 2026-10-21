"""Loading of the config directory.

Three layers plus a knowledge base:
- requirements.yaml: global requirements every profile inherits
- profiles.yaml:     vehicle profiles (generations, engines, model-specific limits)
- evaluation.yaml:   rules that label listings (price tiers, mileage preference, confidence)
- catalog.yaml:      generations and engines with reliability status/notes
plus settings.yaml for technical settings (HTTP pacing, limits, database path).

`load_config` is the entry point. It validates cross-references (profile → generation →
engine) up front, so a typo in YAML fails at startup instead of silently matching nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class HttpSettings:
    """How the collector talks to the marketplace. Keep it slow and identifiable."""

    user_agent: str = "auto-hunter/0.1 (personal used-car monitor)"
    request_delay_s: float = 1.5
    timeout_s: float = 20
    max_retries: int = 3


@dataclass
class CollectionSettings:
    """Per-run request budgets; whatever exceeds a budget is picked up by the next run."""

    page_size: int = 100
    max_results_per_query: int = 2000  # safety cap for one model query
    max_detail_fetches: int = 300      # detail requests for newly seen listings
    max_removal_checks: int = 200      # requests confirming that vanished listings are gone


@dataclass
class AlertSettings:
    """When to notify about changes to already-known listings."""

    min_price_drop_pct: float = 3


@dataclass
class Settings:
    """settings.yaml: technical settings, independent of what car you want."""

    database_path: Path = Path("data/auto_hunter.db")
    http: HttpSettings = field(default_factory=HttpSettings)
    collection: CollectionSettings = field(default_factory=CollectionSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)


@dataclass
class Requirements:
    """requirements.yaml: global requirements, merged into every profile.

    Values use Sauto's SEO names (fuel "benzin", body "kombi", ...).
    """

    fuel: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    mileage_max: int | None = None
    exclude_keywords: list[str] = field(default_factory=list)


# preferred/ok: no flag; caution: flagged with its notes; unvetted: flagged as not yet
# researched; avoid: rejected when certain, flagged when only possible.
ENGINE_STATUSES = ("preferred", "ok", "caution", "unvetted", "avoid")


@dataclass
class Engine:
    """catalog.yaml engine: the specs used to recognise it, plus your verdict on it."""

    id: str
    label: str
    liters: float          # compared with displacement rounded to 0.1 l
    power_kw: list[int]    # every output it was sold with; matched with ±3 kW tolerance
    turbo: bool            # compared with title keywords (TSI/EcoBoost vs MPI/Ti-VCT)
    status: str = "unvetted"
    notes: str = ""
    automatic_note: str = ""  # extra caution shown when the car is an automatic


@dataclass
class Generation:
    """catalog.yaml generation of a model, e.g. Octavia III.

    Sauto has no generation field, so classify.py infers it from the listing's year,
    title (title_hints regexes) and which engines this generation was sold with.
    """

    id: str
    label: str
    models: list[str]      # Sauto "make:model" keys this generation is listed under
    year_from: int
    year_to: int | None    # None = still in production
    title_hints: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)

    def covers_year(self, year: int) -> bool:
        return self.year_from <= year and (self.year_to is None or year <= self.year_to)


@dataclass
class Catalog:
    """catalog.yaml: the knowledge base of generations and engines, keyed by id."""

    generations: dict[str, Generation] = field(default_factory=dict)
    engines: dict[str, Engine] = field(default_factory=dict)

    def generations_for(self, model_key: str | None) -> list[Generation]:
        """Generations listed under a Sauto model; empty for models not in the catalog."""
        return [g for g in self.generations.values() if model_key in g.models]


@dataclass
class PriceTier:
    """A price band; a listing gets the first tier whose max it doesn't exceed."""

    max: int
    label: str
    note: str = ""


@dataclass
class EvaluationRules:
    """evaluation.yaml: rules that label listings. They never reject anything."""

    price_tiers: list[PriceTier] = field(default_factory=list)
    preferred_mileage_max: int | None = None
    medium_confidence_max_uncertainties: int = 2


@dataclass
class SearchProfile:
    """A profile with global requirements already merged in."""

    name: str
    models: list[str]
    priority: str = "medium"
    price_min: int | None = None
    price_max: int | None = None
    mileage_max: int | None = None
    year_min: int | None = None
    year_max: int | None = None
    fuel: list[str] = field(default_factory=list)
    gearbox: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    generations: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)


PRIORITIES = ("high", "medium", "exploratory")


@dataclass
class Config:
    """Everything loaded from the config directory."""

    settings: Settings
    requirements: Requirements
    catalog: Catalog
    evaluation: EvaluationRules
    profiles: list[SearchProfile]
    # sources.yaml: per-marketplace sections, interpreted by each collector.
    sources: dict[str, dict[str, Any]] = field(default_factory=lambda: {"sauto": {}})

    def enabled_sources(self) -> list[str]:
        return [name for name, cfg in self.sources.items() if (cfg or {}).get("enabled", True)]


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_settings(path: Path) -> Settings:
    """Read settings.yaml; missing keys keep their defaults."""
    data = _read(path)
    return Settings(
        database_path=Path(data.get("database_path", Settings.database_path)),
        http=HttpSettings(**data.get("http", {})),
        collection=CollectionSettings(**data.get("collection", {})),
        alerts=AlertSettings(**data.get("alerts", {})),
    )


def load_catalog(path: Path) -> Catalog:
    """Read catalog.yaml, validating statuses, title-hint regexes and engine references."""
    data = _read(path)
    engines = {eid: Engine(id=eid, **e) for eid, e in (data.get("engines") or {}).items()}
    for e in engines.values():
        if e.status not in ENGINE_STATUSES:
            raise ValueError(f"Engine {e.id}: status must be one of {ENGINE_STATUSES}")
    generations = {}
    for gid, g in (data.get("generations") or {}).items():
        year_from, year_to = g.pop("years")
        gen = Generation(id=gid, year_from=year_from, year_to=year_to, **g)
        for h in gen.title_hints:
            re.compile(h)
        unknown = set(gen.engines) - set(engines)
        if unknown:
            raise ValueError(f"Generation {gid}: unknown engines {sorted(unknown)}")
        generations[gid] = gen
    return Catalog(generations, engines)


def load_evaluation(path: Path) -> EvaluationRules:
    """Read evaluation.yaml; price tiers are sorted by their max."""
    data = _read(path)
    tiers = sorted((PriceTier(**t) for t in data.pop("price_tiers", [])), key=lambda t: t.max)
    return EvaluationRules(price_tiers=tiers, **data)


def resolve_profile(raw: dict[str, Any], req: Requirements, catalog: Catalog) -> SearchProfile:
    """Turn one profiles.yaml entry into a SearchProfile.

    - models: the generations' Sauto models plus any listed explicitly
    - year range: spans the generations unless the profile sets year_min/year_max
    - price/mileage caps: the profile's own, else the global ones (never above the budget)
    - fuel/body: global; exclude_keywords: global plus the profile's own
    """
    raw = dict(raw)
    name = raw.pop("name")
    gen_ids = raw.pop("generations", [])
    engine_ids = raw.pop("engines", [])
    for gid in gen_ids:
        if gid not in catalog.generations:
            raise ValueError(f"Profile {name!r}: unknown generation {gid!r}")
    for eid in engine_ids:
        if eid not in catalog.engines:
            raise ValueError(f"Profile {name!r}: unknown engine {eid!r}")
    gens = [catalog.generations[g] for g in gen_ids]

    models = list(dict.fromkeys([*(m for g in gens for m in g.models), *raw.pop("models", [])]))
    if not models:
        raise ValueError(f"Profile {name!r}: needs generations or models")
    for m in models:
        if ":" not in m:
            raise ValueError(f"Profile {name!r}: model {m!r} must be 'make:model'")

    priority = raw.pop("priority", "medium")
    if priority not in PRIORITIES:
        raise ValueError(f"Profile {name!r}: priority must be one of {PRIORITIES}")

    year_min = raw.pop("year_min", min((g.year_from for g in gens), default=None))
    default_year_max = (
        None if not gens or any(g.year_to is None for g in gens) else max(g.year_to for g in gens)
    )
    year_max = raw.pop("year_max", default_year_max)
    profile = SearchProfile(
        name=name,
        models=models,
        priority=priority,
        price_min=raw.pop("price_min", req.price_min),
        price_max=raw.pop("price_max", req.price_max),
        mileage_max=raw.pop("mileage_max", req.mileage_max),
        year_min=year_min,
        year_max=year_max,
        fuel=raw.pop("fuel", req.fuel),
        body=raw.pop("body", req.body),
        exclude_keywords=[*req.exclude_keywords, *raw.pop("exclude_keywords", [])],
        generations=gen_ids,
        engines=engine_ids,
        **raw,  # gearbox
    )
    if req.price_max is not None and (profile.price_max or 0) > req.price_max:
        raise ValueError(f"Profile {name!r}: price_max exceeds the global budget")
    return profile


def load_config(config_dir: Path) -> Config:
    """Load and validate the whole config directory. Raises ValueError on mistakes."""
    settings = load_settings(config_dir / "settings.yaml")
    requirements = Requirements(**_read(config_dir / "requirements.yaml"))
    catalog = load_catalog(config_dir / "catalog.yaml")
    evaluation = load_evaluation(config_dir / "evaluation.yaml")
    raw_profiles = _read(config_dir / "profiles.yaml").get("profiles", [])
    profiles = [resolve_profile(p, requirements, catalog) for p in raw_profiles]
    names = [p.name for p in profiles]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate profile names in profiles.yaml")
    sources_path = config_dir / "sources.yaml"
    sources = _read(sources_path) if sources_path.exists() else {"sauto": {}}
    for name, cfg in sources.items():
        for key, mapping in ((cfg or {}).get("models") or {}).items():
            if ":" not in key:
                raise ValueError(f"sources.yaml {name}: model {key!r} must be 'make:model'")
            if "title" in mapping:
                re.compile(mapping["title"])
    return Config(settings, requirements, catalog, evaluation, profiles, sources)
