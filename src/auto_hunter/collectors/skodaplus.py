"""Collector for skodaplus.cz (Škoda dealer network stock, all makes) via its GraphQL API.

The site's own frontend uses POST https://www.skodaplus.cz/graphql (robots.txt allows
everything). Data is structured and rich: VIN, displacement, power, service book,
first owner and an accident flag on every car, so no detail fetch is needed.

Quirks handled here:
- the API serves Czech and Slovak stock; Slovak prices are in EUR, so we always
  filter regionCountry: CZ
- makes, models, fuel and body types are opaque IDs ("model_404"); they are resolved
  from names once per run (see sources.yaml for the name mapping)
- `cars.totalCount` errors server-side, so paging follows pageInfo.hasNextPage
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

BASE_URL = "https://www.skodaplus.cz"
PAGE_SIZE = 100

# Our (Sauto) codebook names -> Škoda Plus codebook values.
FUEL_NAMES = {
    "benzin": ["Benzín"], "nafta": ["Diesel"], "lpg-benzin": ["Plyn (LPG)"],
    "cng-benzin": ["Plyn (CNG)"], "elektro": ["Elektro"],
    "hybridni": ["Hybridní M-HEV", "Hybridní - P-HEV"],
}
BODY_NAMES = {
    "kombi": ["Kombi"], "hatchback": ["Hatchback"], "liftback": ["Liftback"],
    "sedanlimuzina": ["Sedan", "Limuzína"], "suv": ["SUV"], "mpv": ["MPV"],
}
GEARBOX_NAMES = {"manualni": ["Manuální"], "automaticka": ["Automatická", "DSG", "tiptronic"]}

CAR_FIELDS = """
  id prettyUrl modelType engineCapacity enginePower mileage manufactureYear
  firstRegistration vin crashed hasServiceBook firstOwner reserved createdAt note
  model { id modelName carMake { name } }
  price { value }
  fuel { value } transmission { value } bodyType { value }
  dealer { name address { city zip } }
  images { normalUrl }
"""
SEARCH_QUERY = (
    "query($filter: CarFilterInput, $after: String) {"
    " cars(first: %d, after: $after, filter: $filter) {"
    " pageInfo { hasNextPage endCursor } edges { node { %s } } } }" % (PAGE_SIZE, CAR_FIELDS)
)
CAR_QUERY = "query($id: ID!) { car(id: $id) { %s } }" % CAR_FIELDS
LOOKUP_QUERY = """{
  makes: carMakes(includeUnused: true) { id name }
  fuel: codeBook(codeBookName: FUEL_TYPE, includeUnused: true) { id value }
  body: codeBook(codeBookName: BODY_TYPE, includeUnused: true) { id value }
  gearbox: codeBook(codeBookName: TRANSMISSION, includeUnused: true) { id value }
}"""
MODELS_QUERY = (
    "query($makes: [ID]) { carModels(includeUnused: true, filter: {make: $makes})"
    " { id modelName carMake { name } } }"
)


class SkodaPlusCollector(Collector):
    """Škoda Plus collector. Full scan; one instance per run (lookups are cached)."""

    source = "skodaplus"

    def __init__(self, http: HttpSettings, collection: CollectionSettings,
                 source_config: dict[str, Any]) -> None:
        super().__init__()
        self._collection = collection
        self._client = PoliteClient(BASE_URL, http)
        # model key -> {"make": "Škoda", "models": ["Octavia", ...]}
        self._model_map: dict[str, dict[str, Any]] = source_config.get("models") or {}
        self._lookups: dict[str, dict[str, str]] | None = None  # kind -> folded name -> id
        self._model_ids: dict[str, list[str]] = {}               # model key -> ids
        self._key_by_model_id: dict[str, str] = {}
        self._query_cache: dict[str, tuple[list[dict[str, Any]], bool]] = {}

    def close(self) -> None:
        self._client.close()

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.request("POST", "/graphql",
                                    json={"query": query, "variables": variables or {}})
        if resp.status_code != 200:
            raise CollectorError(f"Škoda Plus GraphQL: HTTP {resp.status_code}")
        body = resp.json()
        if body.get("errors") and not body.get("data"):
            raise CollectorError(f"Škoda Plus GraphQL: {body['errors'][0].get('message')}")
        return body["data"]

    def _resolve(self) -> None:
        """Turn the names in sources.yaml and our codebook names into Škoda Plus IDs."""
        if self._lookups is not None:
            return
        data = self._graphql(LOOKUP_QUERY)
        self._lookups = {
            kind: {fold(item.get("name") or item.get("value")): item["id"] for item in data[kind]}
            for kind in ("makes", "fuel", "body", "gearbox")
        }
        make_ids = sorted({
            self._lookups["makes"][fold(m["make"])]
            for m in self._model_map.values() if fold(m["make"]) in self._lookups["makes"]
        })
        models = self._graphql(MODELS_QUERY, {"makes": make_ids})["carModels"]
        by_name = {(fold(m["carMake"]["name"]), fold(m["modelName"])): m["id"] for m in models}
        for key, mapping in self._model_map.items():
            ids = []
            for name in mapping["models"]:
                model_id = by_name.get((fold(mapping["make"]), fold(name)))
                if model_id is None:
                    log.warning("Škoda Plus has no model %s %s (sources.yaml %s)",
                                mapping["make"], name, key)
                else:
                    ids.append(model_id)
                    self._key_by_model_id[model_id] = key
            self._model_ids[key] = ids

    def _codebook_ids(self, kind: str, names: dict[str, list[str]], ours: list[str]) -> list[str]:
        assert self._lookups is not None
        return [self._lookups[kind][fold(v)] for o in ours for v in names.get(o, [])
                if fold(v) in self._lookups[kind]]

    def search(self, profile: SearchProfile) -> SearchResult:
        self._resolve()
        listings: dict[str, Listing] = {}
        complete = True
        for make_model in profile.models:
            model_ids = self._model_ids.get(make_model)
            if not model_ids:
                log.debug("Škoda Plus: no mapping for %s, skipped", make_model)
                continue
            nodes, query_complete = self._query(self._build_filter(profile, model_ids))
            complete &= query_complete
            for node in nodes:
                listing = parse_car(node, self._key_by_model_id.get(node["model"]["id"]))
                listings[listing.source_id] = listing
        return SearchResult(list(listings.values()), complete)

    def _build_filter(self, profile: SearchProfile, model_ids: list[str]) -> dict[str, Any]:
        flt: dict[str, Any] = {"model": model_ids, "regionCountry": "CZ", "isNew": False}
        if profile.fuel:
            flt["fuelTypes"] = self._codebook_ids("fuel", FUEL_NAMES, profile.fuel)
        if profile.body:
            flt["bodyTypes"] = self._codebook_ids("body", BODY_NAMES, profile.body)
        if profile.gearbox:
            flt["transmissionTypes"] = self._codebook_ids("gearbox", GEARBOX_NAMES, profile.gearbox)
        price = {k: v for k, v in (("min", profile.price_min), ("max", profile.price_max)) if v}
        if price:
            flt["priceRange"] = price
        if profile.mileage_max:
            flt["mileageRange"] = {"max": profile.mileage_max}
        return flt

    def _query(self, flt: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """All cars for one filter, following the cursor; cached per run."""
        key = repr(sorted(flt.items()))
        if key not in self._query_cache:
            nodes: dict[str, dict[str, Any]] = {}
            after = None
            complete = False
            for _ in range(max(1, self._collection.max_results_per_query // PAGE_SIZE)):
                cars = self._graphql(SEARCH_QUERY, {"filter": flt, "after": after})["cars"]
                for edge in cars["edges"]:
                    nodes[edge["node"]["id"]] = edge["node"]
                if not cars["pageInfo"]["hasNextPage"]:
                    complete = True
                    break
                after = cars["pageInfo"]["endCursor"]
            if not complete:
                log.warning("Škoda Plus: results exceed cap of %d, narrow the profile",
                            self._collection.max_results_per_query)
            self._query_cache[key] = (list(nodes.values()), complete)
        return self._query_cache[key]

    def fetch(self, source_id: str) -> Listing | None:
        self._resolve()
        car = self._graphql(CAR_QUERY, {"id": source_id}).get("car")
        if car is None:
            return None
        return parse_car(car, self._key_by_model_id.get(car["model"]["id"]))


def _codebook_to_ours(value: str | None, names: dict[str, list[str]]) -> str | None:
    if not value:
        return None
    for ours, theirs in names.items():
        if fold(value) in (fold(t) for t in theirs):
            return ours
    return None


def parse_car(node: dict[str, Any], model_key: str | None) -> Listing:
    """Convert a Škoda Plus Car node into a Listing (always has full details)."""
    number = node["id"].removeprefix("Car-")
    dealer = node.get("dealer") or {}
    address = dealer.get("address") or {}
    year = node.get("manufactureYear") or (
        int(node["firstRegistration"][:4]) if node.get("firstRegistration") else None
    )
    make = node["model"]["carMake"]["name"]
    title = f"{make} {node['model']['modelName']} {node.get('modelType') or ''}".strip()
    return Listing(
        source=SkodaPlusCollector.source,
        source_id=node["id"],
        url=f"{BASE_URL}/Car/{number}/{node.get('prettyUrl') or ''}/",
        title=title,
        price_czk=(node.get("price") or {}).get("value") or None,
        make=make,
        model=node["model"]["modelName"],
        model_key=model_key,
        variant=node.get("modelType"),
        year=year,
        manufacturing_date=node.get("firstRegistration"),
        mileage_km=node.get("mileage"),
        fuel=_codebook_to_ours((node.get("fuel") or {}).get("value"), FUEL_NAMES),
        gearbox=_codebook_to_ours((node.get("transmission") or {}).get("value"), GEARBOX_NAMES),
        power_kw=node.get("enginePower") or None,
        engine_cc=node.get("engineCapacity") or None,
        body=_codebook_to_ours((node.get("bodyType") or {}).get("value"), BODY_NAMES),
        vin=node.get("vin") or None,
        seller_type="dealer",
        seller_name=dealer.get("name"),
        municipality=address.get("city"),
        description=node.get("note") or None,
        service_book=node.get("hasServiceBook"),
        first_owner=node.get("firstOwner"),
        damaged=node.get("crashed"),
        image_urls=[BASE_URL + i["normalUrl"] for i in node.get("images") or []
                    if i.get("normalUrl")],
        source_created_at=node.get("createdAt"),
        is_active=True,
        has_details=True,
        raw=node,
    )
