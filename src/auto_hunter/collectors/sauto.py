"""Collector for sauto.cz via its public JSON API (robots.txt allows /api/).

Endpoints (undocumented, observed from the website):
- GET /api/v1/items/search  paginated search; filters below in build_search_params
- GET /api/v1/items/{id}    full listing (description, VIN, power, body, status);
                            404 once the listing is gone

Quirks handled here:
- unknown model names are silently ignored (you get the whole make), so results
  are checked against the requested model
- no year/generation filter, so those are applied locally
- dealer-feed listings may only have in_operation_date (first registration)
"""

from __future__ import annotations

import logging
from typing import Any

from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.collectors.http import PoliteClient
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile
from auto_hunter.models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://www.sauto.cz"
PASSENGER_CARS_CATEGORY = 838


class SautoCollector(Collector):
    """Sauto.cz collector. One instance per run: its query cache lives as long as it does."""

    source = "sauto"

    def __init__(self, http: HttpSettings, collection: CollectionSettings) -> None:
        super().__init__()
        self._collection = collection
        self._client = PoliteClient(BASE_URL, http, headers={"Accept": "application/json"})
        # Profiles often share a query (same model, same global filters); fetch it once.
        self._query_cache: dict[tuple, tuple[list[dict[str, Any]], bool]] = {}

    def close(self) -> None:
        self._client.close()

    def search(self, profile: SearchProfile) -> SearchResult:
        listings: dict[str, Listing] = {}
        complete = True
        for make_model in profile.models:
            items, query_complete = self._query(build_search_params(profile, make_model))
            complete &= query_complete
            for item in items:
                listing = parse_item(item)
                listings[listing.source_id] = listing
        return SearchResult(list(listings.values()), complete)

    def _query(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """All raw items for one model query, paging through results; cached per run."""
        key = tuple(sorted(params.items()))
        if key in self._query_cache:
            return self._query_cache[key]
        make_model = params["manufacturer_model_seo"]
        # Sorting by date can shift items between pages mid-scan; dedupe by id.
        items: dict[Any, dict[str, Any]] = {}
        complete = True
        offset = 0
        while True:
            data = self._get_json(
                "/api/v1/items/search",
                {**params, "limit": self._collection.page_size, "offset": offset},
            )
            results = data.get("results") or []
            total = data.get("pagination", {}).get("total", 0)
            for item in results:
                if _model_key(item) != make_model:
                    # Sauto silently ignores unknown model names and returns the whole make.
                    raise CollectorError(
                        f"Sauto doesn't recognise model {make_model!r} (got "
                        f"{_model_key(item)!r}); check the name in the sauto.cz URL"
                    )
                items[item["id"]] = item
            offset += len(results)
            if not results or offset >= total:
                break
            if offset >= self._collection.max_results_per_query:
                log.warning("%s: %d results exceed cap of %d, narrow the profile",
                            make_model, total, self._collection.max_results_per_query)
                complete = False
                break
        self._query_cache[key] = (list(items.values()), complete)
        return self._query_cache[key]

    def fetch(self, source_id: str) -> Listing | None:
        data = self._get_json(f"/api/v1/items/{source_id}", allow_404=True)
        if data is None or "result" not in data:
            return None
        return parse_item(data["result"], has_details=True)

    def _get_json(
        self, path: str, params: dict[str, Any] | None = None, allow_404: bool = False
    ) -> Any:
        """GET JSON; None on 404 if allowed. Other non-200 statuses raise CollectorError."""
        resp = self._client.request("GET", path, params=params)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code != 200:
            raise CollectorError(f"GET {path} failed: HTTP {resp.status_code}")
        return resp.json()


def build_search_params(profile: SearchProfile, make_model: str) -> dict[str, Any]:
    """Translate a profile into Sauto search filters for one model (used cars only).

    Multi-value filters are comma-separated, e.g. fuel_seo=benzin,nafta.
    """
    params: dict[str, Any] = {
        "category_id": PASSENGER_CARS_CATEGORY,
        "manufacturer_model_seo": make_model,
        "condition_seo": "ojete",
    }
    if profile.price_min is not None:
        params["price_from"] = profile.price_min
    if profile.price_max is not None:
        params["price_to"] = profile.price_max
    if profile.mileage_max is not None:
        params["tachometer_to"] = profile.mileage_max
    if profile.fuel:
        params["fuel_seo"] = ",".join(profile.fuel)
    if profile.gearbox:
        params["gearbox_seo"] = ",".join(profile.gearbox)
    if profile.body:
        params["vehicle_body_seo"] = ",".join(profile.body)
    # The API exposes no year filter; that is applied locally in filters.py.
    return params


def _seo(cb: dict[str, Any] | None) -> str | None:
    return cb.get("seo_name") if cb else None


def _model_key(item: dict[str, Any]) -> str:
    return f"{_seo(item.get('manufacturer_cb'))}:{_seo(item.get('model_cb'))}"


def parse_item(item: dict[str, Any], has_details: bool = False) -> Listing:
    """Convert a Sauto item (search result or detail) into a Listing.

    Fields absent from search results (description, VIN, power...) come out as None.
    """
    manufacturer = item.get("manufacturer_cb") or {}
    model = item.get("model_cb") or {}
    locality = item.get("locality") or {}
    premise = item.get("premise")
    # Imported (dealer-feed) listings often only carry the first-registration date.
    manufacturing_date = item.get("manufacturing_date") or item.get("in_operation_date")
    source_id = str(item["id"])
    return Listing(
        source=SautoCollector.source,
        source_id=source_id,
        url=f"{BASE_URL}/osobni/detail/{manufacturer.get('seo_name', 'x')}/{model.get('seo_name', 'x')}/{source_id}",
        title=item.get("name") or "",
        price_czk=None if item.get("price_by_agreement") else item.get("price"),
        make=manufacturer.get("name"),
        model=model.get("name"),
        model_key=_model_key(item),
        variant=item.get("additional_model_name"),
        year=int(manufacturing_date[:4]) if manufacturing_date else None,
        manufacturing_date=manufacturing_date,
        mileage_km=item.get("tachometer"),
        fuel=_seo(item.get("fuel_cb")),
        gearbox=_seo(item.get("gearbox_cb")),
        power_kw=item.get("engine_power"),
        engine_cc=item.get("engine_volume"),
        body=_seo(item.get("vehicle_body_cb")),
        vin=item.get("vin") or None,
        seller_type="dealer" if premise else "private",
        seller_name=premise.get("name") if premise else None,
        region=locality.get("region"),
        district=locality.get("district"),
        municipality=locality.get("municipality"),
        description=item.get("description"),
        service_book=item.get("service_book"),
        first_owner=item.get("first_owner"),
        image_urls=[
            "https:" + img["url"] if img["url"].startswith("//") else img["url"]
            for img in item.get("images") or []
            if img.get("url")
        ],
        source_created_at=item.get("create_date"),
        is_active=item.get("status", "active") == "active",
        has_details=has_details,
        raw=item,
    )
