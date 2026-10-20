"""Evaluation rules: label a listing with price tier, flags and a confidence level.

This deliberately produces explanations, not a single score. Weighted scoring needs
market comparables and researched reliability data, which come in later phases.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from auto_hunter.classify import Classification
from auto_hunter.config import Catalog, EvaluationRules
from auto_hunter.models import Listing

# Flags that mean "we don't know", as opposed to "we know something bad".
UNCERTAINTY_CODES = {
    "generation_unclear", "engine_unclear", "engine_unknown", "engine_uncataloged",
    "vin_missing", "service_unknown",
}


@dataclass
class Flag:
    """One finding. code is stable (for code/tests), message is for you to read."""

    code: str
    severity: str  # info | caution
    message: str


@dataclass
class Assessment:
    """Labels for one listing. generation/engine are set only when unambiguous."""

    generation: str | None
    generation_candidates: list[str]
    engine: str | None
    engine_candidates: list[str]
    engine_status: str | None
    price_tier: str | None
    confidence: str  # high | medium | low
    flags: list[Flag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def assess(listing: Listing, cls: Classification, catalog: Catalog,
           rules: EvaluationRules) -> Assessment:
    """Apply the evaluation rules to a classified listing.

    Flags fall into two kinds: things we know are risky (caution engines, high mileage,
    no service book) and things we don't know (unclear engine, missing VIN). Only the
    second kind lowers confidence, since a known risk is information, not uncertainty.
    """
    flags: list[Flag] = []

    def flag(code: str, severity: str, message: str) -> None:
        flags.append(Flag(code, severity, message))

    gen_labels = [catalog.generations[g].label for g in cls.generations]
    if cls.cataloged_model and len(cls.generations) > 1:
        flag("generation_unclear", "caution", "Generation unclear: " + " or ".join(gen_labels))

    engines = [catalog.engines[e] for e in cls.engines]
    engine = engines[0] if len(engines) == 1 else None
    if not cls.cataloged_model:
        flag("engine_unvetted", "info", "Model not in catalog yet; engine reliability not researched")
    elif cls.engine_uncataloged:
        flag("engine_uncataloged", "caution", "Engine specs match no catalog engine for this generation")
    elif not cls.engine_known:
        flag("engine_unknown", "caution", "Engine not stated (no displacement or power)")
    elif engine is None:
        flag("engine_unclear", "caution",
             "Engine unclear: " + " or ".join(e.label for e in engines) + ". Ask for the engine code")
    for e in engines if engine is None and cls.engine_known else [engine] if engine else []:
        if e.status == "avoid":
            flag("engine_avoid", "caution",
                 f"Could be {e.label}, which you've excluded. Confirm the engine code first")
        elif e.status == "caution":
            flag("engine_caution", "caution", f"{e.label}: {e.notes}")
        elif e.status == "unvetted":
            flag("engine_unvetted", "info", f"{e.label}: reliability not yet researched")
        if listing.gearbox == "automaticka" and e.automatic_note:
            flag("gearbox_caution", "caution", e.automatic_note)

    if (rules.preferred_mileage_max is not None and listing.mileage_km is not None
            and listing.mileage_km > rules.preferred_mileage_max):
        flag("mileage_high", "caution",
             f"Mileage above preferred {rules.preferred_mileage_max:,} km; condition and "
             "maintenance matter more than usual".replace(",", " "))

    tier = None
    if listing.price_czk is None:
        flag("price_missing", "info", "Price on request")
    else:
        for t in rules.price_tiers:
            if listing.price_czk <= t.max:
                tier = t.label
                if t.note:
                    flag(f"tier_{t.label}", "info", t.note)
                break

    # Service history and VIN are only known after a detail fetch.
    if listing.has_details:
        if listing.service_book is False:
            flag("service_none", "caution", "Seller does not state a service book")
        elif listing.service_book is None:
            flag("service_unknown", "info", "Service history not stated")
        if not listing.vin:
            flag("vin_missing", "info", "VIN not published; ask the seller before travelling")

    uncertainties = sum(f.code in UNCERTAINTY_CODES for f in flags)
    confidence = (
        "high" if uncertainties == 0
        else "medium" if uncertainties <= rules.medium_confidence_max_uncertainties
        else "low"
    )
    return Assessment(
        generation=cls.generation,
        generation_candidates=cls.generations,
        engine=cls.engine,
        engine_candidates=cls.engines,
        engine_status=engine.status if engine else None,
        price_tier=tier,
        confidence=confidence,
        flags=flags,
    )
