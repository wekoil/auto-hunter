"""Collector for auto.bazos.cz: free-text classifieds, read from HTML.

robots.txt disallows Bazoš search and filter URLs (hledat=, cenaod=, ...), so we only
read brand category pages (/skoda/, /skoda/20/, ...) and listing pages (/inzerat/<id>/).

Category pages are ordered by last bump: new listings and paid "TOP" re-bumps (about
2,000 a day in the Škoda category) mix at the top. Reading every page is impossible
(31,000 Škodas), so the crawl is incremental: newest first, stopping after two
consecutive pages that contain nothing new since the previous crawl. Ids seen in
recent crawls are kept in `state` (stored in the database between runs).

Consequences:
- full_scan is False: a tracked listing missing from a crawl means nothing; the
  pipeline re-checks it individually after `recheck_after_days`
- the first crawl only reaches max_pages_per_category deep; older listings are
  discovered as they get bumped
- specs come from text (textspecs.py), so they are less reliable than Sauto's fields
- a deleted listing redirects to a category page; a wrong URL slug still shows the
  listing (with HTTP 404), so "gone" is judged by content, not status code
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date
from typing import Any

from auto_hunter import textspecs
from auto_hunter.classify import fold
from auto_hunter.collectors.base import Collector, CollectorError, SearchResult
from auto_hunter.collectors.http import PoliteClient
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile
from auto_hunter.models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://auto.bazos.cz"
PAGE_SIZE = 20
# Ids remembered per category for the stop rule; several crawls' worth.
SEEN_IDS_PER_CATEGORY = 5000

_ITEM_SPLIT = re.compile(r'<div class="inzeraty inzeratyflex">')
_ITEM_LINK = re.compile(r'<h2 class=nadpis><a href="(/inzerat/(\d+)/[^"]*)">(.*?)</a>', re.S)
_ITEM_DATE = re.compile(r"\[(\d{1,2})\.(\d{1,2})\.\s*(\d{4})\]")
_ITEM_DESC = re.compile(r"<div class=popis>(.*?)</div>", re.S)
_ITEM_PRICE = re.compile(r'<div class="inzeratycena">(.*?)</div>', re.S)
_ITEM_PLACE = re.compile(r'<div class="inzeratylok">(.*?)<br>(.*?)</div>', re.S)
_ITEM_IMAGE = re.compile(r'<img src="([^"]+)" class="obrazek"')

_DETAIL_TITLE = re.compile(r"<h1 class=nadpisdetail>(.*?)</h1>", re.S | re.I)
_DETAIL_DESC = re.compile(r"<div class=popisdetail>(.*?)</div>", re.S)
_DETAIL_PRICE = re.compile(r"Cena:</td>.*?<b>(.*?)</b>", re.S)
_DETAIL_PLACE = re.compile(r'Lokalita:</td>.*?title="Přibližná lokalita"[^>]*>([^<]*)</a>\s*<a[^>]*>([^<]*)</a>', re.S)
_DETAIL_IMAGE = re.compile(r'<meta property="og:image" content="([^"]+)"')
_DETAIL_CANONICAL = re.compile(r'<link rel="canonical" href="([^"]+)"')


def _text(fragment: str) -> str:
    """HTML fragment to plain text, keeping line breaks."""
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _price(text: str) -> int | None:
    """'279 000 Kč' -> 279000; 'Dohodou', 'V textu', 'Nabídněte' -> None."""
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits and "kč" in text.lower() else None


def is_czech_zip(zip_code: str) -> bool:
    """Czech postcodes start with 1-7; Slovak ones with 8-9 (Bazoš mixes both)."""
    digits = re.sub(r"\D", "", zip_code)
    return len(digits) == 5 and digits[0] in "1234567"


class BazosCollector(Collector):
    """Bazoš collector. Incremental; see the module docstring for how and why."""

    source = "bazos"
    full_scan = False
    summary_is_authoritative = False

    def __init__(self, http: HttpSettings, collection: CollectionSettings,
                 source_config: dict[str, Any]) -> None:
        super().__init__()
        self._client = PoliteClient(BASE_URL, http, follow_redirects=False)
        self._max_pages = int(source_config.get("max_pages_per_category", 40))
        self.recheck_after_days = float(source_config.get("recheck_after_days", 2))
        # [(model key, category, compiled title regex)], in config order.
        self._models = [
            (key, m["category"], re.compile(m["title"]))
            for key, m in (source_config.get("models") or {}).items()
        ]
        self._crawl_cache: dict[str, list[Listing]] = {}

    def close(self) -> None:
        self._client.close()

    def model_key_for(self, title: str, category: str | None = None) -> str | None:
        folded = fold(title)
        for key, cat, pattern in self._models:
            if (category is None or cat == category) and pattern.search(folded):
                return key
        return None

    def search(self, profile: SearchProfile) -> SearchResult:
        categories = {cat for key, cat, _ in self._models if key in profile.models}
        listings = []
        for category in sorted(categories):
            for listing in self._crawl(category):
                if listing.model_key not in profile.models:
                    continue
                # Done here because Bazoš can't filter server-side. A listing that
                # never says it's an estate can't be told from a hatchback, so when a
                # body is required, silence counts as "no".
                if profile.body and listing.body not in profile.body:
                    continue
                if profile.fuel and listing.fuel and listing.fuel not in profile.fuel:
                    continue
                listings.append(listing)
        return SearchResult(listings, complete=False)

    def _crawl(self, category: str) -> list[Listing]:
        """New and bumped listings in a category since the previous crawl."""
        if category in self._crawl_cache:
            return self._crawl_cache[category]
        seen_before = set(self.state.get("seen", {}).get(category, []))
        found: dict[str, Listing] = {}
        crawled_ids: list[str] = []
        quiet_pages = 0
        for page in range(self._max_pages):
            path = f"/{category}/" + (f"{page * PAGE_SIZE}/" if page else "")
            resp = self._client.request("GET", path)
            if resp.status_code != 200:
                raise CollectorError(f"Bazoš {path}: HTTP {resp.status_code}")
            items = parse_list_page(resp.text)
            if not items:
                break
            page_ids = [item["id"] for item in items]
            crawled_ids.extend(page_ids)
            for item in items:
                if not is_czech_zip(item["zip"]):
                    continue
                key = self.model_key_for(item["title"], category)
                if key is not None:
                    found[item["id"]] = listing_from_item(item, key)
            quiet_pages = quiet_pages + 1 if seen_before.issuperset(page_ids) else 0
            if quiet_pages >= 2:
                break
        else:
            if seen_before:
                log.warning("Bazoš %s: reached %d pages without meeting the previous crawl; "
                            "some listings may have been missed", category, self._max_pages)
        log.info("Bazoš %s: crawled %d listings, %d of watched models",
                 category, len(crawled_ids), len(found))
        merged = list(dict.fromkeys(crawled_ids + list(seen_before)))[:SEEN_IDS_PER_CATEGORY]
        self.state.setdefault("seen", {})[category] = merged
        self._crawl_cache[category] = list(found.values())
        return self._crawl_cache[category]

    def fetch(self, source_id: str) -> Listing | None:
        # Any slug works for a live listing; deleted ones redirect elsewhere.
        resp = self._client.request("GET", f"/inzerat/{source_id}/inzerat.php")
        if 300 <= resp.status_code < 400 or "popisdetail" not in resp.text:
            return None
        listing = parse_detail_page(resp.text, source_id)
        listing.model_key = self.model_key_for(listing.title)
        return listing


def parse_list_page(page: str) -> list[dict[str, Any]]:
    """Raw fields of each listing on a category page."""
    items = []
    for block in _ITEM_SPLIT.split(page)[1:]:
        link = _ITEM_LINK.search(block)
        if not link:
            continue
        place = _ITEM_PLACE.search(block)
        when = _ITEM_DATE.search(block)
        desc = _ITEM_DESC.search(block)
        price = _ITEM_PRICE.search(block)
        image = _ITEM_IMAGE.search(block)
        items.append({
            "id": link.group(2),
            "path": link.group(1),
            "title": _text(link.group(3)),
            "description": _text(desc.group(1)) if desc else "",
            "price": _text(price.group(1)) if price else "",
            "city": _text(place.group(1)) if place else "",
            "zip": _text(place.group(2)) if place else "",
            "date": (date(int(when.group(3)), int(when.group(2)), int(when.group(1))).isoformat()
                     if when else None),
            "top": 'class="ztop"' in block,
            "image": image.group(1) if image else None,
        })
    return items


def _listing(source_id: str, url: str, title: str, description: str, price: str,
             city: str, images: list[str], has_details: bool, raw: dict[str, Any],
             created: str | None = None, model_key: str | None = None) -> Listing:
    specs = textspecs.extract(title, description)
    return Listing(
        source=BazosCollector.source,
        source_id=source_id,
        url=url,
        title=title,
        price_czk=_price(price),
        model_key=model_key,
        year=specs.year,
        mileage_km=specs.mileage_km,
        fuel=specs.fuel,
        gearbox=specs.gearbox,
        power_kw=specs.power_kw,
        engine_cc=specs.engine_cc,
        body=specs.body,
        vin=specs.vin,
        municipality=city or None,
        description=description or None,
        service_book=specs.service_book,
        image_urls=images,
        source_created_at=created,
        has_details=has_details,
        raw=raw,
    )


def listing_from_item(item: dict[str, Any], model_key: str | None) -> Listing:
    """Summary listing from a category page (description is truncated)."""
    return _listing(
        item["id"], BASE_URL + item["path"], item["title"], item["description"],
        item["price"], item["city"], [item["image"]] if item["image"] else [],
        has_details=False, raw=item, created=item["date"], model_key=model_key,
    )


def parse_detail_page(page: str, source_id: str) -> Listing:
    """Full listing from its own page."""
    title = _DETAIL_TITLE.search(page)
    desc = _DETAIL_DESC.search(page)
    price = _DETAIL_PRICE.search(page)
    place = _DETAIL_PLACE.search(page)
    image = _DETAIL_IMAGE.search(page)
    canonical = _DETAIL_CANONICAL.search(page)
    raw = {
        "title": _text(title.group(1)) if title else "",
        "description": _text(desc.group(1)) if desc else "",
        "price": _text(price.group(1)) if price else "",
        "zip": _text(place.group(1)) if place else "",
        "city": _text(place.group(2)) if place else "",
    }
    url = canonical.group(1) if canonical else f"{BASE_URL}/inzerat/{source_id}/inzerat.php"
    return _listing(
        source_id, url, raw["title"], raw["description"],
        raw["price"], raw["city"], [image.group(1)] if image else [],
        has_details=True, raw=raw,
    )
