"""Interface every marketplace collector implements.

To add a marketplace: subclass Collector, set a unique `source` name, convert its data
into Listing, and make `model_key` use the same "make:model" names as your profiles
(map them in config/sources.yaml).

Two kinds of source:
- full scan (full_scan = True): every search returns *all* matching listings, so a
  tracked listing missing from the results has probably gone (Sauto, Škoda Plus).
- incremental (full_scan = False): searches only return what is new since last time
  (Bazoš). Absence means nothing; tracked listings are re-checked individually once
  they haven't been seen for `recheck_after_days`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from auto_hunter.config import SearchProfile
from auto_hunter.models import Listing


class CollectorError(Exception):
    """The source could not be queried; results for this run are unreliable."""


@dataclass
class SearchResult:
    listings: list[Listing]
    # False if we know we did not see every matching listing (e.g. hit a cap).
    # Removal detection only runs after complete searches.
    complete: bool


class Collector(ABC):
    """A marketplace. Implementations must be polite: pace requests, retry sparingly."""

    source: str  # stored with every listing, e.g. "sauto"
    full_scan: bool = True
    recheck_after_days: float = 2
    # False if search results carry less reliable data than a detail fetch (e.g. specs
    # parsed from a truncated snippet). Then, once details are stored, a search result
    # only updates the price and title.
    summary_is_authoritative: bool = True

    # Persistent per-source state (e.g. where the last crawl stopped). The pipeline
    # loads it from the database before a run and saves it afterwards.
    state: dict[str, Any]

    def __init__(self) -> None:
        self.state = {}

    @abstractmethod
    def search(self, profile: SearchProfile) -> SearchResult:
        """Return listings matching the profile's source-side filters."""

    @abstractmethod
    def fetch(self, source_id: str) -> Listing | None:
        """Return the full listing, or None if the source no longer has it."""

    def close(self) -> None:
        pass
