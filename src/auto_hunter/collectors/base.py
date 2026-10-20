"""Interface every marketplace collector implements.

To add a marketplace: subclass Collector, set a unique `source` name, convert its data
into Listing, and make `model_key` use the same "make:model" names as your profiles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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

    @abstractmethod
    def search(self, profile: SearchProfile) -> SearchResult:
        """Return summary-level listings matching the profile's server-side filters."""

    @abstractmethod
    def fetch(self, source_id: str) -> Listing | None:
        """Return the full listing, or None if the source no longer has it."""

    def close(self) -> None:
        pass
