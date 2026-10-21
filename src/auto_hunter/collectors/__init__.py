from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.collectors.bazos import BazosCollector
from auto_hunter.collectors.sauto import SautoCollector
from auto_hunter.collectors.skodaplus import SkodaPlusCollector
from auto_hunter.config import Config

__all__ = [
    "BazosCollector", "Collector", "CollectorError", "SautoCollector", "SearchResult",
    "SkodaPlusCollector", "build_collectors",
]


def build_collectors(config: Config, only: list[str] | None = None) -> list[Collector]:
    """Instantiate the sources enabled in sources.yaml (optionally just `only`)."""
    s = config.settings
    factories = {
        "sauto": lambda cfg: SautoCollector(s.http, s.collection),
        "skodaplus": lambda cfg: SkodaPlusCollector(s.http, s.collection, cfg),
        "bazos": lambda cfg: BazosCollector(s.http, s.collection, cfg),
    }
    unknown = set(config.sources) - set(factories)
    if unknown:
        raise ValueError(f"sources.yaml: unknown sources {sorted(unknown)}")
    names = [n for n in config.enabled_sources() if not only or n in only]
    return [factories[n](config.sources[n] or {}) for n in names]
