"""Identify a listing's generation and engine from year, title and specs.

Works by elimination: start from every (generation, engine) pair the catalog allows for
the listing's model, drop pairs contradicted by the data, and report what's left. One
pair left means identified; several mean unclear; we never guess between them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from auto_hunter.config import Catalog, Engine, Generation
from auto_hunter.models import Listing

POWER_TOLERANCE_KW = 3

_LITERS_RE = re.compile(r"(?<![\d.,])([0-3])[.,](\d)(?!\d)")
_KW_RE = re.compile(r"\b(\d{2,3})\s*kw\b")
_TURBO_RE = re.compile(r"tsi|tfsi|turbo|ecoboost|ecob\b|t-?gdi|\btjet\b")
_NA_RE = re.compile(r"\bmpi\b|ti-?vct|cvvt|vvt-?i|valvematic|\bfsi\b")


def fold(text: str) -> str:
    """Lowercase and strip diacritics so 'Havarovaný' matches 'havarovan'."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass
class EngineHints:
    """What the listing reveals about its engine; None means not stated."""

    liters: float | None = None
    power_kw: int | None = None
    turbo: bool | None = None

    @property
    def known(self) -> bool:
        return self.liters is not None or self.power_kw is not None


def engine_hints(listing: Listing) -> EngineHints:
    """Structured specs first (detail fetch), title text as fallback."""
    text = fold(f"{listing.title} {listing.variant or ''}")
    hints = EngineHints()
    if listing.engine_cc:
        hints.liters = round(listing.engine_cc / 1000, 1)
    elif m := _LITERS_RE.search(text):
        hints.liters = float(f"{m.group(1)}.{m.group(2)}")
    if listing.power_kw:
        hints.power_kw = listing.power_kw
    elif m := _KW_RE.search(text):
        hints.power_kw = int(m.group(1))
    if _TURBO_RE.search(text):
        hints.turbo = True
    elif _NA_RE.search(text):
        hints.turbo = False
    return hints


def engine_matches(engine: Engine, hints: EngineHints) -> bool:
    """True unless a known hint contradicts the engine; unknown hints match anything."""
    if hints.liters is not None and abs(engine.liters - hints.liters) > 0.05:
        return False
    if hints.power_kw is not None and not any(
        abs(p - hints.power_kw) <= POWER_TOLERANCE_KW for p in engine.power_kw
    ):
        return False
    if hints.turbo is not None and engine.turbo != hints.turbo:
        return False
    return True


@dataclass
class Classification:
    """Remaining (generation, engine) candidates after elimination."""

    generations: list[str] = field(default_factory=list)  # candidates, best first
    engines: list[str] = field(default_factory=list)      # candidates
    # Specs were available but match no catalog engine of the candidate generations.
    engine_uncataloged: bool = False
    engine_known: bool = False
    # Model has generations in the catalog (False for exploratory models).
    cataloged_model: bool = False

    @property
    def generation(self) -> str | None:
        return self.generations[0] if len(self.generations) == 1 else None

    @property
    def engine(self) -> str | None:
        return self.engines[0] if len(self.engines) == 1 else None


def classify(listing: Listing, catalog: Catalog) -> Classification:
    """Narrow down generation and engine: year, then title hints, then engine specs."""
    gens: list[Generation] = catalog.generations_for(listing.model_key)
    if not gens:
        return Classification(engine_known=engine_hints(listing).known)

    if listing.year is not None:
        gens = [g for g in gens if g.covers_year(listing.year)]
    text = fold(f"{listing.title} {listing.variant or ''}")
    hinted = [g for g in gens if any(re.search(h, text) for h in g.title_hints)]
    if hinted:
        gens = hinted

    hints = engine_hints(listing)
    pairs = [
        (g.id, e) for g in gens for e in g.engines if engine_matches(catalog.engines[e], hints)
    ]
    if pairs:
        return Classification(
            generations=list(dict.fromkeys(g for g, _ in pairs)),
            engines=list(dict.fromkeys(e for _, e in pairs)),
            engine_known=hints.known,
            cataloged_model=True,
        )
    return Classification(
        generations=[g.id for g in gens],
        engine_uncataloged=hints.known,
        engine_known=hints.known,
        cataloged_model=True,
    )
