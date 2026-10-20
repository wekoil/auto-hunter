"""Source-independent representation of a listing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Listing:
    """One advertisement, normalized. Collectors produce it; everything else consumes it.

    Units: price in CZK, mileage in km, power in kW, displacement in cc.
    Enumerations (fuel, gearbox, body) use Sauto SEO names, e.g. "benzin", "manualni".
    None always means "the listing doesn't say", never "no".
    """

    source: str
    source_id: str
    url: str
    title: str
    price_czk: int | None
    make: str | None = None
    model: str | None = None
    # Source-specific "make:model" key used by profiles and the catalog, e.g. "skoda:octavia".
    model_key: str | None = None
    variant: str | None = None
    year: int | None = None
    manufacturing_date: str | None = None
    mileage_km: int | None = None
    fuel: str | None = None
    gearbox: str | None = None
    power_kw: int | None = None
    engine_cc: int | None = None
    body: str | None = None
    vin: str | None = None
    seller_type: str | None = None  # "dealer" | "private"
    seller_name: str | None = None
    region: str | None = None
    district: str | None = None
    municipality: str | None = None
    description: str | None = None
    service_book: bool | None = None
    first_owner: bool | None = None
    image_urls: list[str] = field(default_factory=list)
    source_created_at: str | None = None
    # Whether the source still shows the listing (only known from a detail fetch).
    is_active: bool = True
    # True when built from a detail response rather than a search result.
    has_details: bool = False
    # Original source payload, kept so we can re-normalize later without recollecting.
    raw: dict[str, Any] = field(default_factory=dict)
