"""Collector for sauto.cz via its public JSON API (robots.txt allows /api/)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile
from auto_hunter.models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://www.sauto.cz"
PASSENGER_CARS_CATEGORY = 838


class SautoCollector(Collector):
    source = "sauto"

    def __init__(self, http: HttpSettings, collection: CollectionSettings) -> None:
        self._http = http
        self._collection = collection
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": http.user_agent, "Accept": "application/json"},
            timeout=http.timeout_s,
        )
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def search(self, profile: SearchProfile) -> SearchResult:
        listings: dict[str, Listing] = {}
        complete = True
        for make_model in profile.models:
            params = build_search_params(profile, make_model)
            offset = 0
            while True:
                data = self._get_json(
                    "/api/v1/items/search",
                    {**params, "limit": self._collection.page_size, "offset": offset},
                )
                results = data.get("results") or []
                total = data.get("pagination", {}).get("total", 0)
                for item in results:
                    listing = parse_item(item)
                    # Sorting by date can shift items between pages mid-scan; dedupe by id.
                    listings[listing.source_id] = listing
                offset += len(results)
                if not results or offset >= total:
                    break
                if offset >= self._collection.max_results_per_query:
                    log.warning(
                        "%s/%s: %d results exceed cap of %d, narrow the profile",
                        profile.name, make_model, total, self._collection.max_results_per_query,
                    )
                    complete = False
                    break
        return SearchResult(list(listings.values()), complete)

    def fetch(self, source_id: str) -> Listing | None:
        data = self._get_json(f"/api/v1/items/{source_id}", allow_404=True)
        if data is None or "result" not in data:
            return None
        return parse_item(data["result"], has_details=True)

    def _get_json(
        self, path: str, params: dict[str, Any] | None = None, allow_404: bool = False
    ) -> Any:
        for attempt in range(1, self._http.max_retries + 1):
            wait = self._http.request_delay_s - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as e:
                err: str = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 404 and allow_404:
                    return None
                if resp.status_code == 200:
                    return resp.json()
                err = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            log.warning("GET %s failed (%s), attempt %d", path, err, attempt)
            time.sleep(self._http.request_delay_s * 2**attempt)
        raise CollectorError(f"GET {path} failed: {err}")


def build_search_params(profile: SearchProfile, make_model: str) -> dict[str, Any]:
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


def parse_item(item: dict[str, Any], has_details: bool = False) -> Listing:
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
