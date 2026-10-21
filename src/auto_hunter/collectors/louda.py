"""Collector for autolouda.cz (Louda Auto dealer group, all makes).

The catalogue page is filled by POST /api/cars/filter-cars, which takes the page's
filter object and returns cars as JSON (robots.txt only disallows some query-string
filters on HTML pages). Data: make, model, year, mileage, kW, displacement, fuel,
gearbox, price. No VIN and no body type per car: the body is known from the filter
we sent. Full scan.

Quirks handled here:
- unknown make/model names return no cars rather than an error, so a typo in
  sources.yaml shows up as a source that never finds anything (logged at debug level)
- results come 24 per page; paging stops once TotalCarsCount is reached
- a removed car's page still returns 200, with the title "Vozidlo již není v nabídce"
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from auto_hunter import textspecs
from auto_hunter.classify import fold
from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.collectors.http import PoliteClient
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile
from auto_hunter.models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://autolouda.cz"
PAGE_SIZE = 24
CUSTOM_PRICE_TYPE = 4  # the filter's "custom range" price option

# Our (Sauto) codebook names -> Louda filter values.
FUEL = {"benzin": "Benzín", "nafta": "Diesel", "elektro": "Elektřina", "hybridni": "Hybridní",
        "lpg-benzin": "LPG/CNG", "cng-benzin": "LPG/CNG"}
BODY = {"kombi": "Kombi", "hatchback": "Hatchback/liftback", "liftback": "Hatchback/liftback",
        "suv": "SUV", "mpv": "MPV", "sedanlimuzina": "Sedan/limuzína"}
GEARBOX = {"automaticka": "Automatická", "manualni": "Manuální"}
FUEL_BACK = {"benzin": "benzin", "diesel": "nafta", "elektrina": "elektro", "hybridni": "hybridni"}

_GONE_TITLE = "neni v nabidce"


class LoudaCollector(Collector):
    """Louda collector. Full scan; one instance per run (queries are cached)."""

    source = "louda"

    def __init__(self, http: HttpSettings, collection: CollectionSettings,
                 source_config: dict[str, Any]) -> None:
        super().__init__()
        self._collection = collection
        self._client = PoliteClient(BASE_URL, http)
        # model key -> {"make": "Škoda", "models": ["OCTAVIA"]}
        self._model_map: dict[str, dict[str, Any]] = source_config.get("models") or {}
        self._query_cache: dict[str, tuple[list[dict[str, Any]], bool]] = {}

    def close(self) -> None:
        self._client.close()

    def search(self, profile: SearchProfile) -> SearchResult:
        listings: dict[str, Listing] = {}
        complete = True
        # The body is only known from the filter, so pass it on when it's unambiguous.
        body = profile.body[0] if len(profile.body) == 1 else None
        for make_model in profile.models:
            mapping = self._model_map.get(make_model)
            if not mapping:
                continue
            cars, query_complete = self._query(build_filter(profile, mapping))
            complete &= query_complete
            if not cars:
                log.debug("Louda: nothing for %s %s", mapping["make"], mapping["models"])
            for car in cars:
                listing = parse_car(car, make_model, body)
                listings.setdefault(listing.source_id, listing)
        return SearchResult(list(listings.values()), complete)

    def _query(self, flt: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        key = repr(sorted(flt.items()))
        if key not in self._query_cache:
            cars: dict[int, dict[str, Any]] = {}
            complete = False
            for page in range(1, max(1, self._collection.max_results_per_query // PAGE_SIZE) + 1):
                resp = self._client.request("POST", "/api/cars/filter-cars", json={
                    "FilterResult": {**flt, "Page": page},
                    "ResetFilter": False, "ResetEVFilter": False, "ResetHybridFilter": False,
                })
                if resp.status_code != 200:
                    raise CollectorError(f"Louda filter-cars: HTTP {resp.status_code}")
                data = resp.json()
                total = data["FilterResult"]["TotalCarsCount"]
                before = len(cars)
                for car in data["Cars"]:
                    cars[car["id"]] = car
                if len(cars) >= total or len(cars) == before:
                    complete = len(cars) >= total
                    break
            self._query_cache[key] = (list(cars.values()), complete)
        return self._query_cache[key]

    def fetch(self, source_id: str) -> Listing | None:
        # Any slug works; only the trailing id matters.
        resp = self._client.request("GET", f"/koupit/x-{source_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CollectorError(f"Louda car {source_id}: HTTP {resp.status_code}")
        return parse_detail_page(resp.text, source_id)


def build_filter(profile: SearchProfile, mapping: dict[str, Any]) -> dict[str, Any]:
    """The catalogue filter object for one make's models, used cars only."""
    flt: dict[str, Any] = {
        "UsedCars": True,
        "Manufacturers": [{"Name": mapping["make"], "Models": list(mapping["models"])}],
        "FuelTypes": sorted({FUEL[f] for f in profile.fuel if f in FUEL}),
        "BodyTypes": sorted({BODY[b] for b in profile.body if b in BODY}),
        "Transmissions": sorted({GEARBOX[g] for g in profile.gearbox if g in GEARBOX}),
    }
    if profile.price_min or profile.price_max:
        flt["Prices"] = {"SelectedType": CUSTOM_PRICE_TYPE, "PriceFrom": profile.price_min,
                         "PriceTo": profile.price_max}
    if profile.mileage_max:
        flt["MileageCustomTo"] = profile.mileage_max
    return flt


def parse_car(car: dict[str, Any], model_key: str | None, body: str | None) -> Listing:
    """Convert a Louda catalogue car into a Listing (complete; no detail fetch needed)."""
    gearbox = fold(car.get("transmissionType") or "")
    liters = car.get("engineVolume")
    return Listing(
        source=LoudaCollector.source,
        source_id=str(car["id"]),
        url=BASE_URL + car["carWiseUrl"],
        title=car.get("name") or f"{car.get('manufacturer')} {car.get('model')}",
        price_czk=car.get("price") or None,
        make=car.get("manufacturer"),
        model=car.get("model"),
        model_key=model_key,
        year=car.get("yearOfProduction"),
        mileage_km=car.get("mileage"),
        fuel=FUEL_BACK.get(fold(car.get("fuelType") or "")),
        gearbox="automaticka" if "automat" in gearbox else "manualni" if "manual" in gearbox else None,
        power_kw=car.get("performace") or None,  # sic: the API's spelling
        engine_cc=int(round(liters * 1000, -2)) if liters else None,
        body=body,
        seller_type="dealer",
        seller_name="Louda Auto",
        image_urls=list(car.get("pictureList") or [])[:10],
        is_active=True,
        has_details=True,
        raw={k: v for k, v in car.items() if k != "pictureList"},
    )


def parse_detail_page(page: str, source_id: str) -> Listing | None:
    """Minimal listing from a car page, used to confirm it's still offered."""
    title_m = re.search(r"<title>(.*?)</title>", page, re.S)
    title = html.unescape(title_m.group(1)).split("|")[0].strip() if title_m else ""
    if not title or _GONE_TITLE in fold(title):
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    specs = textspecs.extract(title, re.sub(r"\s+", " ", text))
    return Listing(
        source=LoudaCollector.source, source_id=source_id,
        url=f"{BASE_URL}/koupit/x-{source_id}", title=title, price_czk=None,
        power_kw=specs.power_kw, engine_cc=specs.engine_cc,
        is_active=True, has_details=False,
    )
