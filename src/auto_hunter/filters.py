"""Cheap local filters for criteria the source API can't express."""

from __future__ import annotations

import re

from auto_hunter.classify import Classification, fold
from auto_hunter.config import Catalog, SearchProfile
from auto_hunter.models import Listing


def rejection_reason(listing: Listing, profile: SearchProfile,
                     cls: Classification | None = None,
                     catalog: Catalog | None = None) -> str | None:
    """Return why the listing doesn't fit the profile, or None if it fits.

    Unknown values pass: a missing field is a question for the seller, not a reason to drop.
    """
    if listing.model_key and listing.model_key not in profile.models:
        return f"model {listing.model_key}"
    if listing.price_czk is not None:
        if profile.price_max is not None and listing.price_czk > profile.price_max:
            return "price above max"
        if profile.price_min is not None and listing.price_czk < profile.price_min:
            return "price below min"
    if (
        profile.mileage_max is not None
        and listing.mileage_km is not None
        and listing.mileage_km > profile.mileage_max
    ):
        return "mileage above max"
    if listing.year is not None:
        if profile.year_min is not None and listing.year < profile.year_min:
            return "too old"
        if profile.year_max is not None and listing.year > profile.year_max:
            return "too new"
    if profile.fuel and listing.fuel and listing.fuel not in profile.fuel:
        return f"fuel {listing.fuel}"
    if profile.gearbox and listing.gearbox and listing.gearbox not in profile.gearbox:
        return f"gearbox {listing.gearbox}"
    if profile.body and listing.body and listing.body not in profile.body:
        return f"body {listing.body}"
    # Only the title/variant, and only at a word start: descriptions and titles often
    # say "nehavarovaný" (not crashed), which must not match "havarovan".
    headline = fold(f"{listing.title} {listing.variant or ''}")
    for kw in profile.exclude_keywords:
        if re.search(r"\b" + re.escape(fold(kw)), headline):
            return f"keyword {kw!r}"

    if cls is not None:
        if profile.generations and not set(cls.generations) & set(profile.generations):
            return "generation " + ("/".join(cls.generations) or "outside catalog years")
        # Only reject on engine when the specs actually rule the profile's engines out.
        if profile.engines and cls.engine_known:
            if cls.engine_uncataloged or not set(cls.engines) & set(profile.engines):
                return "engine " + ("/".join(cls.engines) or "not in profile")
        # Engines you never want: reject only when every remaining candidate is one.
        if catalog is not None and cls.engines and all(
            catalog.engines[e].status == "avoid" for e in cls.engines
        ):
            return "avoided engine " + "/".join(cls.engines)
    return None
