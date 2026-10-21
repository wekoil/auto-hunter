"""Collector for autoklokocka.cz (Autosalon Klokočka dealer group).

Server-rendered HTML; robots.txt only disallows /ajax/, /eshop/, /cart/ and
/onlinepayment/. The list URL carries make and model in the path and the other
filters as query parameters:

    /nabidka-vozu/znacka:skoda/model:octavia?stav[]=ojete&palivo[]=benzin&karoserie[]=kombi&page=2

Cards (10 per page) give title, price, fuel, mileage and year; power, body and gearbox
need the detail page (/nabidka-vozu/vuz/<slug>), which returns 404 once a car is gone.
The slug is the listing's id here. No VINs are published. Full scan.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from auto_hunter import textspecs
from auto_hunter.classify import fold
from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.collectors.http import PoliteClient
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile
from auto_hunter.models import Listing

BASE_URL = "https://autoklokocka.cz"
PAGE_SIZE = 10

# Our (Sauto) codebook names -> Klokočka filter values (mostly identical).
FUEL = {"benzin": "benzin", "nafta": "nafta", "elektro": "elektro",
        "cng-benzin": "cng-benzin", "hybridni": "hybridni-benzin"}
BODY = {"kombi": "kombi", "hatchback": "hatchback", "liftback": "liftback",
        "sedanlimuzina": "sedan", "suv": "suv", "mpv": "mpv"}
GEARBOX = {"automaticka": "automaticka", "manualni": "manualni"}
FUEL_BACK = {"benzin": "benzin", "nafta": "nafta", "elektro": "elektro",
             "cng + benzin": "cng-benzin", "hybridni": "hybridni"}
BODY_BACK = {v: k for k, v in BODY.items() if k != "liftback"} | {"liftback": "liftback"}

_CARD = re.compile(r'<article class="car \| card"(.*?)</article>', re.S)
_CARD_LINK = re.compile(r'<a class="card-link" href="/nabidka-vozu/vuz/([^"]+)">(.*?)</a>', re.S)
_CARD_PRICE = re.compile(r'<p class="text-h4">(.*?)</p>', re.S)
_CARD_FOOT = re.compile(r'<li class="flex-grow">(.*?)</li>', re.S)
_CARD_IMAGE = re.compile(r'data-lazyload="([^"]+)"')
_TOTAL = re.compile(r"Celkem\s+(\d+)\s+voz")
_LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _number(text: str) -> int | None:
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


class KlokockaCollector(Collector):
    """Klokočka collector. Full scan; one instance per run (queries are cached)."""

    source = "klokocka"

    def __init__(self, http: HttpSettings, collection: CollectionSettings,
                 source_config: dict[str, Any]) -> None:
        super().__init__()
        self._collection = collection
        self._client = PoliteClient(BASE_URL, http)
        # model key -> {"brand": "skoda", "model": "octavia"}
        self._model_map: dict[str, dict[str, Any]] = source_config.get("models") or {}
        self._query_cache: dict[tuple, tuple[list[dict[str, Any]], bool]] = {}

    def close(self) -> None:
        self._client.close()

    def search(self, profile: SearchProfile) -> SearchResult:
        listings: dict[str, Listing] = {}
        complete = True
        for make_model in profile.models:
            mapping = self._model_map.get(make_model)
            if not mapping:
                continue
            path = f"/nabidka-vozu/znacka:{mapping['brand']}/model:{mapping['model']}"
            cards, query_complete = self._query(path, build_params(profile))
            complete &= query_complete
            for card in cards:
                listing = listing_from_card(card, make_model)
                listings.setdefault(listing.source_id, listing)
        return SearchResult(list(listings.values()), complete)

    def _query(self, path: str, params: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], bool]:
        key = (path, tuple(params))
        if key not in self._query_cache:
            cards: dict[str, dict[str, Any]] = {}
            complete = False
            for page in range(1, max(1, self._collection.max_results_per_query // PAGE_SIZE) + 1):
                resp = self._client.request("GET", path, params=params + [("page", str(page))])
                if resp.status_code != 200:
                    raise CollectorError(f"Klokočka {path}: HTTP {resp.status_code}")
                page_cards = parse_list_page(resp.text)
                total_m = _TOTAL.search(resp.text)
                total = int(total_m.group(1)) if total_m else 0
                before = len(cards)
                for card in page_cards:
                    cards[card["slug"]] = card
                if len(cards) >= total or len(cards) == before:
                    complete = len(cards) >= total
                    break
            self._query_cache[key] = (list(cards.values()), complete)
        return self._query_cache[key]

    def fetch(self, source_id: str) -> Listing | None:
        resp = self._client.request("GET", f"/nabidka-vozu/vuz/{source_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CollectorError(f"Klokočka car {source_id}: HTTP {resp.status_code}")
        return parse_detail_page(resp.text, source_id)


def build_params(profile: SearchProfile) -> list[tuple[str, str]]:
    params = [("stav[]", "ojete")]
    params += [("palivo[]", FUEL[f]) for f in profile.fuel if f in FUEL]
    params += [("karoserie[]", BODY[b]) for b in profile.body if b in BODY]
    params += [("razeni[]", GEARBOX[g]) for g in profile.gearbox if g in GEARBOX]
    # No price parameter: the site's cena_preset is ignored when sent in the URL, so
    # price is left to the local filters.
    return params


def parse_list_page(page: str) -> list[dict[str, Any]]:
    cards = []
    for block in _CARD.findall(page):
        link = _CARD_LINK.search(block)
        if not link:
            continue
        price = _CARD_PRICE.search(block)
        foot = [_text(x) for x in _CARD_FOOT.findall(block)]
        image = _CARD_IMAGE.search(block)
        cards.append({
            "slug": link.group(1),
            "title": _text(link.group(2)),
            "price": _text(price.group(1)) if price else "",
            "fuel": foot[0] if len(foot) > 0 else "",
            "km": foot[1] if len(foot) > 1 else "",
            "year": foot[2] if len(foot) > 2 else "",
            "image": image.group(1) if image else None,
        })
    return cards


def listing_from_card(card: dict[str, Any], model_key: str | None) -> Listing:
    """Summary listing; power/body/gearbox come with the detail page."""
    title = card["title"]
    specs = textspecs.extract(title, "")
    year = _number(card["year"])
    return Listing(
        source=KlokockaCollector.source,
        source_id=card["slug"],
        url=f"{BASE_URL}/nabidka-vozu/vuz/{card['slug']}",
        title=title,
        price_czk=_number(card["price"]) if "kč" in card["price"].lower() else None,
        model_key=model_key,
        year=year if year and 1990 <= year <= 2100 else None,
        mileage_km=_number(card["km"]),
        fuel=FUEL_BACK.get(fold(card["fuel"])),
        gearbox=specs.gearbox,
        engine_cc=specs.engine_cc,
        seller_type="dealer",
        seller_name="Autosalon Klokočka",
        image_urls=[card["image"]] if card["image"] else [],
        has_details=False,
        raw=card,
    )


def parse_detail_page(page: str, source_id: str) -> Listing:
    """Full listing from a car page: '<title> <year>, <km> km', 'Výkon: 110 Kw', ..."""
    vehicle: dict[str, Any] = {}
    for block in _LD_JSON.findall(page):
        try:
            for item in json.loads(block) if block.strip().startswith("[") else [json.loads(block)]:
                if item.get("@type") == "Vehicle":
                    vehicle = item
        except (json.JSONDecodeError, AttributeError):
            continue
    text = _text(page)
    folded = fold(text)

    def field(label: str) -> str | None:
        m = re.search(label + r":\s*([^:]+?)\s+(?=[A-ZČŘŠŽ][a-zá-ž ]+:|Základní)", text)
        return m.group(1).strip() if m else None

    title = re.sub(r"\s+", " ", vehicle.get("name") or "").strip()
    header = re.search(re.escape(title) + r"\s+(\d{4}),\s*([\d\s]+?)\s*km", text) if title else None
    power = re.search(r"vykon:\s*(\d{2,3})\s*kw", folded)
    gearbox = fold(field("Převodovka") or "")
    offers = vehicle.get("offers") or {}
    price = offers.get("price") if isinstance(offers, dict) else None
    if price is None and (m := re.search(r'"price"\s*:\s*"?(\d+)', page)):
        price = m.group(1)
    specs = textspecs.extract(title, "")
    return Listing(
        source=KlokockaCollector.source,
        source_id=source_id,
        url=f"{BASE_URL}/nabidka-vozu/vuz/{source_id}",
        title=title,
        price_czk=int(price) if price else None,
        make=(vehicle.get("brand") or {}).get("name"),
        model=vehicle.get("model"),
        year=int(header.group(1)) if header else vehicle.get("vehicleModelDate"),
        mileage_km=_number(header.group(2)) if header else None,
        fuel=FUEL_BACK.get(fold(field("Pohon") or "")),
        gearbox="automaticka" if "automat" in gearbox else "manualni" if "manual" in gearbox else None,
        power_kw=int(power.group(1)) if power else None,
        engine_cc=specs.engine_cc,
        body=BODY_BACK.get(fold(field("Karoserie") or "")),
        first_owner=True if "prvni majitel" in folded else None,
        seller_type="dealer",
        seller_name="Autosalon Klokočka",
        image_urls=[str(u) for u in (vehicle.get("image") or [])][:10],
        is_active=True,
        has_details=True,
        raw={"vehicle": {k: v for k, v in vehicle.items() if k != "image"}},
    )
