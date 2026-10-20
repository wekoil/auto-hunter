"""Loading of settings.yaml and search_profiles.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class HttpSettings:
    user_agent: str = "auto-hunter/0.1 (personal used-car monitor)"
    request_delay_s: float = 1.5
    timeout_s: float = 20
    max_retries: int = 3


@dataclass
class CollectionSettings:
    page_size: int = 100
    max_results_per_query: int = 2000
    max_detail_fetches: int = 300
    max_removal_checks: int = 200


@dataclass
class AlertSettings:
    min_price_drop_pct: float = 3


@dataclass
class Settings:
    database_path: Path = Path("data/auto_hunter.db")
    http: HttpSettings = field(default_factory=HttpSettings)
    collection: CollectionSettings = field(default_factory=CollectionSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)


@dataclass
class SearchProfile:
    name: str
    models: list[str]
    price_min: int | None = None
    price_max: int | None = None
    mileage_max: int | None = None
    year_min: int | None = None
    year_max: int | None = None
    fuel: list[str] = field(default_factory=list)
    gearbox: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)


def load_settings(path: Path) -> Settings:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings(
        database_path=Path(data.get("database_path", Settings.database_path)),
        http=HttpSettings(**data.get("http", {})),
        collection=CollectionSettings(**data.get("collection", {})),
        alerts=AlertSettings(**data.get("alerts", {})),
    )


def load_profiles(path: Path) -> list[SearchProfile]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = [SearchProfile(**p) for p in data.get("profiles", [])]
    names = [p.name for p in profiles]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate profile names in {path}")
    for p in profiles:
        for m in p.models:
            if ":" not in m:
                raise ValueError(f"Profile {p.name!r}: model {m!r} must be 'make:model'")
    return profiles
