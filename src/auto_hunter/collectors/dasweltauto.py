"""Collector for dasweltauto.cz (VW-group dealer network's used cars, all makes).

The site's frontend reads GET /api/locales/cs_CZ/vehicles/search (robots.txt allows it;
only PDF exports are disallowed). Results are structured: VIN, kW, mileage, first
registration, dealer. Full scan.

Quirks handled here:
- `models` is silently ignored unless `brands` is also given, and several models in
  one request return inconsistent results, so each model is its own request
- a vehicle's id is the dealer and vehicle numbers, 5 digits each, concatenated; the
  same search accepts `vehicleIds=<id>`, which is how a single car is re-checked
- there is no displacement field; classify.py reads it from the title ("1.0 TSI")
"""

from __future__ import annotations

import logging
from typing import Any

from auto_hunter.classify import fold
from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.collectors.http import PoliteClient
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile
from auto_hunter.models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://www.dasweltauto.cz"
SEARCH_PATH = "/api/locales/cs_CZ/vehicles/search"
PAGE_SIZE = 100

# Our (Sauto) codebook names -> Das WeltAuto filter values.
FUEL = {"benzin": "benzin", "nafta": "diesel", "elektro": "elektro",
        "cng-benzin": "zemni-plyn", "lpg-benzin": "kapalny-plyn",
        "hybridni": "plug-in-hybrid-benzin"}
BODY = {"kombi": "kombi", "sedanlimuzina": "limousine", "suv": "suv", "mpv": "van"}
GEARBOX = {"automaticka": "A", "manualni": "S"}
# Their display texts -> our names.
FUEL_TEXT = {"benzin": "benzin", "diesel": "nafta", "elektro": "elektro",
             "zemni plyn (cng)": "cng-benzin", "kapalny plyn": "lpg-benzin"}
BODY_TEXT = {"kombi": "kombi", "liftback": "liftback", "hatchback": "hatchback",
             "limuzina/sedan": "sedanlimuzina", "suv/terenni vuz": "suv", "van/minibus": "mpv"}


class DasWeltAutoCollector(Collector):
    """Das WeltAuto collector. Full scan; one instance per run (queries are cached)."""

    source = "dasweltauto"

    def __init__(self, http: HttpSettings, collection: CollectionSettings,
                 source_config: dict[str, Any]) -> None:
        super().__init__()
        self._collection = collection
        self._client = PoliteClient(BASE_URL, http, headers={"Accept": "application/json"})
        # model key -> {"brand": "skoda", "models": ["skoda-octavia"]}
        self._model_map: dict[str, dict[str, Any]] = source_config.get("models") or {}
        self._query_cache: dict[tuple, tuple[list[dict[str, Any]], bool]] = {}

    def close(self) -> None:
        self._client.close()

    def _search(self, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.request("GET", SEARCH_PATH, params=params)
        if resp.status_code != 200:
            raise CollectorError(f"Das WeltAuto search: HTTP {resp.status_code}")
        data = resp.json()
        if "resultListPage" not in data:
            raise CollectorError(f"Das WeltAuto search: {data.get('message', 'bad response')}")
        return data

    def search(self, profile: SearchProfile) -> SearchResult:
        listings: dict[str, Listing] = {}
        complete = True
        for make_model in profile.models:
            mapping = self._model_map.get(make_model)
            if not mapping:
                continue
            for model in mapping["models"]:
                items, query_complete = self._query(build_params(profile, mapping["brand"], model))
                complete &= query_complete
                for item in items:
                    listing = parse_vehicle(item, make_model)
                    listings.setdefault(listing.source_id, listing)
        return SearchResult(list(listings.values()), complete)

    def _query(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """All vehicles for one filter, page by page; cached per run."""
        key = tuple(sorted(params.items()))
        if key not in self._query_cache:
            items: dict[str, dict[str, Any]] = {}
            complete = False
            for page in range(1, max(1, self._collection.max_results_per_query // PAGE_SIZE) + 1):
                data = self._search({**params, "page": page, "pageSize": PAGE_SIZE})
                # With no matches the site fills the page with other cars instead.
                content = data["resultListPage"]["content"] if data.get("count") else []
                for item in content:
                    items[vehicle_id(item)] = item
                if not content or len(items) >= data.get("count", 0):
                    complete = True
                    break
            if not complete:
                log.warning("Das WeltAuto %s: results exceed cap of %d", params.get("models"),
                            self._collection.max_results_per_query)
            self._query_cache[key] = (list(items.values()), complete)
        return self._query_cache[key]

    def fetch(self, source_id: str) -> Listing | None:
        data = self._search({"vehicleIds": source_id, "pageSize": 1})
        content = data["resultListPage"]["content"]
        if not data.get("count") or not content:
            return None
        # model_key unknown here; the pipeline keeps the stored one (merge_listings).
        return parse_vehicle(content[0], None)


def build_params(profile: SearchProfile, brand: str, model: str) -> dict[str, Any]:
    """Search filters for one model. Only the first value of each list filter is sent
    when several are requested (the API's multi-value handling is unreliable); the
    local filters still check every listing afterwards."""
    params: dict[str, Any] = {"brands": brand, "models": model}
    for ours, theirs, name in ((profile.fuel, FUEL, "fuelType"), (profile.body, BODY, "bodyTypes"),
                               (profile.gearbox, GEARBOX, "transmission")):
        mapped = [theirs[v] for v in ours if v in theirs]
        if len(mapped) == 1 and len(ours) == 1:
            params[name] = mapped[0]
    if profile.price_min:
        params["minPrice"] = profile.price_min
    if profile.price_max:
        params["maxPrice"] = profile.price_max
    if profile.mileage_max:
        params["maxMileage"] = profile.mileage_max
    return params


def vehicle_id(item: dict[str, Any]) -> str:
    return item["id"]["fiveDigitsDealerId"] + item["id"]["fiveDigitsVehicleId"]


def _text_to_ours(text: str | None, table: dict[str, str]) -> str | None:
    return table.get(fold(text)) if text else None


def parse_vehicle(item: dict[str, Any], model_key: str | None) -> Listing:
    """Convert a Das WeltAuto search item into a Listing (complete; no detail fetch needed)."""
    vid = vehicle_id(item)
    dealer = item.get("dealer") or {}
    registration = item.get("registration")
    gear = fold(item.get("gearTypeText") or item.get("transmission") or "")
    return Listing(
        source=DasWeltAutoCollector.source,
        source_id=vid,
        url=f"{BASE_URL}/vehicle/{vid}",
        title=item.get("modelVariant") or f"{item.get('brand')} {item.get('nameLabel')}",
        price_czk=item.get("price") or None,
        make=item.get("brand"),
        model=item.get("nameLabel"),
        model_key=model_key,
        variant=item.get("variantLabel"),
        year=int(registration[:4]) if registration else None,
        manufacturing_date=registration,
        mileage_km=item.get("mileage"),
        fuel=_text_to_ours(item.get("fuelText"), FUEL_TEXT),
        gearbox="automaticka" if "automat" in gear else "manualni" if "manual" in gear else None,
        power_kw=item.get("powerKw") or None,
        body=_text_to_ours(item.get("bodyType"), BODY_TEXT),
        vin=item.get("vin") or None,
        seller_type="dealer",
        seller_name=dealer.get("name"),
        municipality=dealer.get("city"),
        image_urls=list(item.get("imageUrls") or [])[:10],
        is_active=True,
        has_details=True,
        raw={k: v for k, v in item.items() if k != "imageUrls"},
    )
