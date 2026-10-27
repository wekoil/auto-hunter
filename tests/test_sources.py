"""Bazoš and Škoda Plus collectors against mocked HTTP (markup copied from the real sites)."""

import json

import httpx

from auto_hunter.collectors.bazos import (
    BazosCollector, is_czech_zip, parse_detail_page, parse_list_page,
)
from auto_hunter.collectors.skodaplus import SkodaPlusCollector, parse_car
from auto_hunter.config import CollectionSettings, HttpSettings, SearchProfile

HTTP = HttpSettings(request_delay_s=0)


def bazos_item(id_, title, desc="", price="150 000 Kč", city="Ostrava", zip_="719 00",
               top=False):
    top_html = '<span title="TOP" class="ztop">TOP</span> - ' if top else ""
    return f"""<div class="inzeraty inzeratyflex">
<div class="inzeratynadpis"><a href="/inzerat/{id_}/x.php"><img src="https://www.bazos.cz/img/1t/{id_}.jpg" class="obrazek" alt="" width="170" height="128"></a>
<h2 class=nadpis><a href="/inzerat/{id_}/x.php">{title}</a></h2><span class=velikost10> - {top_html}[20.10. 2026]</span><br>
<div class=popis>{desc}</div><br><br>
</div>
<div class="inzeratycena"><b><span translate="no">  {price}</span></b></div>
<div class="inzeratylok">{city}<br>{zip_}</div>
<div class="inzeratyview">6 x</div>
</div>"""


def bazos_page(*items):
    return "<html><body>" + "".join(items) + "</body></html>"


DETAIL = """<html><head><link rel="canonical" href="https://auto.bazos.cz/inzerat/111/skoda-octavia.php">
<meta property="og:image" content="https://www.bazos.cz/img/1/111.jpg"></head><body>
<div class="inzeratydetnadpis"><H1 class=nadpisdetail>Škoda Octavia 3 Combi 1.6 MPI 81kW</H1></div>
<div class=popisdetail>Rok výroby: 2017<br>Najeto 120 000 km<br>Benzín, manuál<br>VIN: TMBJG7NE0H0000001<br>Servisní knížka</div>
<table><tr><td>Lokalita:</td><td><img></td><td><a href="#" title="Přibližná lokalita" rel="nofollow">719 00</a> <a href="#">Ostrava</a></td></tr>
<tr><td><br>Cena:</td><td colspan=2><br><b><span translate="no"> 155 000 Kč</span></b></td></tr></table>
</body></html>"""

BAZOS_CONFIG = {
    "max_pages_per_category": 5,
    "models": {
        "skoda:octavia": {"category": "skoda", "title": r"\boctavi", "bodies": ["kombi", "liftback"]},
        "volkswagen:golf-variant": {"category": "volkswagen", "title": r"\bgolf\b.*\bvariant\b"},
        "volkswagen:golf": {"category": "volkswagen", "title": r"\bgolf\b(?!\s*(plus|sportsvan))"},
        "seat:leon": {"category": "seat", "title": r"\bleon\b", "body_hints": {"kombi": r"\bst\b"}},
    },
}
PROFILE = SearchProfile(name="p", models=["skoda:octavia"], fuel=["benzin"], body=["kombi"])


def bazos_with(pages: dict[str, str], detail: dict[str, httpx.Response] | None = None):
    requested = []

    def handler(request):
        path = request.url.path
        requested.append(path)
        if path in (detail or {}):
            return detail[path]
        return httpx.Response(200, text=pages.get(path, bazos_page()))

    c = BazosCollector(HTTP, CollectionSettings(), BAZOS_CONFIG)
    c._client._client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    return c, requested


def test_bazos_list_page_parsing():
    items = parse_list_page(bazos_page(
        bazos_item("1", "Škoda Octavia 3 Combi 1.4 TSI", "Najeto: 113 000km", top=True),
        bazos_item("2", "Octavia", price="Dohodou", city="Slovensko", zip_="987 65"),
    ))
    assert [(i["id"], i["top"], i["zip"], i["date"]) for i in items] == [
        ("1", True, "719 00", "2026-10-20"), ("2", False, "987 65", "2026-10-20")]
    assert items[0]["description"] == "Najeto: 113 000km"
    assert is_czech_zip("719 00") and not is_czech_zip("987 65")


def test_bazos_search_filters_model_country_body_fuel():
    c, _ = bazos_with({"/skoda/": bazos_page(
        bazos_item("1", "Škoda Octavia 3 Combi 1.6 MPI"),
        bazos_item("2", "Škoda Octavia 1.6 MPI"),                    # body not stated
        bazos_item("3", "Škoda Octavia Combi 2.0 TDI"),               # diesel
        bazos_item("4", "Škoda Fabia Combi 1.2"),                     # other model
        bazos_item("5", "Škoda Octavia Combi 1.6", zip_="987 65"),    # Slovakia
    )})
    result = c.search(PROFILE)
    assert [l.source_id for l in result.listings] == ["1"]
    l = result.listings[0]
    assert (l.model_key, l.body, l.fuel, l.engine_cc, l.price_czk) == (
        "skoda:octavia", "kombi", "benzin", 1600, 150000)
    assert result.complete is False and not l.has_details


def test_bazos_unstated_body_kept_when_every_body_of_the_model_fits():
    c, _ = bazos_with({"/skoda/": bazos_page(
        bazos_item("1", "Škoda Octavia 3 Combi 1.6 MPI"),
        bazos_item("2", "Škoda Octavia 1.6 MPI"),             # liftback or combi
        bazos_item("3", "Škoda Octavia 1.6 MPI hatchback"),   # says so, not wanted
    )})
    profile = SearchProfile(name="p", models=["skoda:octavia"], body=["kombi", "liftback"])
    assert [l.source_id for l in c.search(profile).listings] == ["1", "2"]


def test_bazos_body_hint_from_model_specific_title_word():
    c, _ = bazos_with({"/seat/": bazos_page(
        bazos_item("1", "Seat Leon ST 1.4 TSI"),
        bazos_item("2", "Seat Leon 1.4 TSI", "6 st. manuál"),   # hint is title-only
    )})
    profile = SearchProfile(name="p", models=["seat:leon"], body=["kombi"])
    assert [(l.source_id, l.body) for l in c.search(profile).listings] == [("1", "kombi")]


def test_bazos_model_order_golf_variant_before_golf():
    c, _ = bazos_with({})
    assert c.model_key_for("VW Golf VI Variant 1.6", "volkswagen") == "volkswagen:golf-variant"
    assert c.model_key_for("VW Golf 1.6 kombi", "volkswagen") == "volkswagen:golf"
    assert c.model_key_for("VW Golf Plus 1.6", "volkswagen") is None


def test_bazos_crawl_stops_where_previous_crawl_left_off():
    known = [bazos_item(str(i), "Škoda Fabia") for i in range(100, 140)]
    pages = {
        "/skoda/": bazos_page(bazos_item("1", "Škoda Octavia Combi 1.6 MPI"), *known[:19]),
        "/skoda/20/": bazos_page(*known[19:39]),
        "/skoda/40/": bazos_page(*known[:20]),
        "/skoda/60/": bazos_page(bazos_item("999", "Škoda Octavia Combi old")),
    }
    c, requested = bazos_with(pages)
    c.state = {"seen": {"skoda": [str(i) for i in range(100, 140)]}}
    c.search(PROFILE)
    # Two consecutive all-known pages (20/, 40/) end the crawl before 60/.
    assert requested == ["/skoda/", "/skoda/20/", "/skoda/40/"]
    assert "1" in c.state["seen"]["skoda"]


def test_bazos_first_crawl_is_capped():
    c, requested = bazos_with({f"/skoda/{n}/" if n else "/skoda/":
                               bazos_page(bazos_item(str(n), "Škoda Fabia")) for n in range(0, 200, 20)})
    c.search(PROFILE)
    assert len(requested) == BAZOS_CONFIG["max_pages_per_category"]


def test_bazos_detail_and_gone():
    c, _ = bazos_with({}, detail={
        "/inzerat/111/inzerat.php": httpx.Response(404, text=DETAIL),   # wrong slug: 404 + content
        "/inzerat/222/inzerat.php": httpx.Response(301, headers={"Location": "/inzeraty/x/"}),
    })
    l = c.fetch("111")
    assert l.url == "https://auto.bazos.cz/inzerat/111/skoda-octavia.php"
    assert (l.title, l.price_czk, l.year, l.mileage_km, l.power_kw, l.fuel, l.gearbox) == (
        "Škoda Octavia 3 Combi 1.6 MPI 81kW", 155000, 2017, 120000, 81, "benzin", "manualni")
    assert (l.vin, l.service_book, l.municipality, l.model_key, l.has_details) == (
        "TMBJG7NE0H0000001", True, "Ostrava", "skoda:octavia", True)
    assert c.fetch("222") is None


def test_bazos_parse_detail_without_canonical():
    l = parse_detail_page(DETAIL.replace('<link rel="canonical"', "<link"), "111")
    assert l.url.endswith("/inzerat/111/inzerat.php")


# --- Škoda Plus -----------------------------------------------------------------------

CAR = {
    "id": "Car-10630466", "prettyUrl": "skoda-octavia-combi-1.6-mpi",
    "modelType": "OCT.COM AMBITION 1.6 MPI / 81KW", "engineCapacity": 1598, "enginePower": 81,
    "mileage": 98000, "manufactureYear": 2017, "firstRegistration": "2017-05-02",
    "vin": "TMBJG7NE5J0000001", "crashed": False, "hasServiceBook": True, "firstOwner": True,
    "reserved": False, "createdAt": "2026-10-19T10:00:00", "note": "Garážované",
    "model": {"id": "model_404", "modelName": "Octavia", "carMake": {"name": "Škoda"}},
    "price": {"value": 189000}, "fuel": {"value": "Benzín"},
    "transmission": {"value": "DSG"}, "bodyType": {"value": "Kombi"},
    "dealer": {"name": "Auto Praha", "address": {"city": "Praha", "zip": "190 00"}},
    "images": [{"normalUrl": "/images/car/a.jpg"}],
}


def test_skodaplus_parse_car():
    l = parse_car(CAR, "skoda:octavia")
    assert l.url == "https://www.skodaplus.cz/Car/10630466/skoda-octavia-combi-1.6-mpi/"
    assert (l.title, l.price_czk, l.year, l.engine_cc, l.power_kw) == (
        "Škoda Octavia OCT.COM AMBITION 1.6 MPI / 81KW", 189000, 2017, 1598, 81)
    assert (l.fuel, l.gearbox, l.body, l.seller_type, l.damaged, l.has_details) == (
        "benzin", "automaticka", "kombi", "dealer", False, True)
    assert l.image_urls == ["https://www.skodaplus.cz/images/car/a.jpg"]


def test_skodaplus_search_resolves_ids_and_filters_czech():
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        q = body["query"]
        if "makes: carMakes" in q:
            return httpx.Response(200, json={"data": {
                "makes": [{"id": "brand_32", "name": "Škoda"}],
                "fuel": [{"id": "fuel_type_1", "value": "Benzín"}],
                "body": [{"id": "body_type_4", "value": "Kombi"}],
                "gearbox": [{"id": "transmission_1", "value": "Manuální"}],
            }})
        if "carModels" in q:
            return httpx.Response(200, json={"data": {"carModels": [
                {"id": "model_404", "modelName": "Octavia", "carMake": {"name": "Škoda"}},
                {"id": "model_101834", "modelName": "Octavia Combi", "carMake": {"name": "Škoda"}},
            ]}})
        page2 = body["variables"]["after"] == "c1"
        return httpx.Response(200, json={"data": {"cars": {
            "pageInfo": {"hasNextPage": not page2, "endCursor": "c1"},
            "edges": [{"node": {**CAR, "id": "Car-2" if page2 else "Car-1"}}],
        }}})

    c = SkodaPlusCollector(HTTP, CollectionSettings(), {
        "models": {"skoda:octavia": {"make": "Škoda", "models": ["Octavia", "Octavia Combi"]}},
    })
    c._client._client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    profile = SearchProfile(name="p", models=["skoda:octavia", "ford:focus"], price_max=200000,
                            fuel=["benzin"], body=["kombi"])
    result = c.search(profile)
    assert sorted(l.source_id for l in result.listings) == ["Car-1", "Car-2"]
    assert result.complete
    flt = sent[2]["variables"]["filter"]
    assert flt == {"model": ["model_404", "model_101834"], "regionCountry": "CZ", "isNew": False,
                   "fuelTypes": ["fuel_type_1"], "bodyTypes": ["body_type_4"],
                   "priceRange": {"max": 200000}}
    # ford:focus has no mapping, so it's skipped rather than failing.
    assert len(sent) == 4


# --- Das WeltAuto -----------------------------------------------------------------------

from auto_hunter.collectors.dasweltauto import DasWeltAutoCollector, build_params, parse_vehicle  # noqa: E402
from auto_hunter.collectors.klokocka import (  # noqa: E402
    KlokockaCollector, build_params as klo_params, listing_from_card, parse_list_page as klo_list,
)
from auto_hunter.collectors.louda import (  # noqa: E402
    LoudaCollector, build_filter, parse_car as louda_parse_car, parse_detail_page as louda_detail,
)

DWA_ITEM = {
    "id": {"fiveDigitsDealerId": "02028", "fiveDigitsVehicleId": "00435"},
    "imageUrls": ["https://img/1"], "price": 129900, "powerKw": 77, "mileage": 119517,
    "modelVariant": "VW Golf Variant Trendline 1,2 TSI 77 kW", "registration": "2013-05-02",
    "dealer": {"name": "AMOND", "postalCode": "27204", "city": "Kladno"}, "brand": "Volkswagen",
    "fuelText": "Benzín", "bodyType": "Kombi", "gearTypeText": "Manuální převodovka",
    "nameLabel": "Golf Variant", "variantLabel": "Trendline 1,2 TSI 77 kW", "vin": "WVWZZZAUZDP000001",
}


def test_dwa_parse_and_params():
    l = parse_vehicle(DWA_ITEM, "volkswagen:golf-variant")
    assert (l.source_id, l.url) == ("0202800435", "https://www.dasweltauto.cz/vehicle/0202800435")
    assert (l.year, l.mileage_km, l.power_kw, l.fuel, l.body, l.gearbox, l.vin) == (
        2013, 119517, 77, "benzin", "kombi", "manualni", "WVWZZZAUZDP000001")
    p = SearchProfile(name="p", models=[], fuel=["benzin"], body=["kombi"], price_max=200000)
    assert build_params(p, "vw", "vw-golf-variant") == {
        "brands": "vw", "models": "vw-golf-variant", "fuelType": "benzin",
        "bodyTypes": "kombi", "maxPrice": 200000}


def test_dwa_one_request_per_model_and_fetch_by_id():
    seen = []
    OTHER = {**DWA_ITEM, "id": {"fiveDigitsDealerId": "99999", "fiveDigitsVehicleId": "99999"}}

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        if "vehicleIds" in params:
            # A gone car still gets a page of other cars, but count 0.
            count = int(params["vehicleIds"] == "0202800435")
            return httpx.Response(200, json={"count": count, "resultListPage": {"content": [DWA_ITEM]}})
        if params["models"] == "vw-golf-variant-8-fl":
            return httpx.Response(200, json={"count": 0, "resultListPage": {"content": [OTHER]}})
        return httpx.Response(200, json={"count": 1, "resultListPage": {"content": [DWA_ITEM]}})

    c = DasWeltAutoCollector(HTTP, CollectionSettings(), {"models": {
        "volkswagen:golf-variant": {"brand": "vw", "models": ["vw-golf-variant", "vw-golf-variant-8-fl"]}}})
    c._client._client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    result = c.search(SearchProfile(name="p", models=["volkswagen:golf-variant"]))
    assert [s["models"] for s in seen] == ["vw-golf-variant", "vw-golf-variant-8-fl"]
    assert all(s["brands"] == "vw" for s in seen)          # models alone would be ignored
    assert len(result.listings) == 1 and result.complete
    assert c.fetch("0202800435").source_id == "0202800435"
    assert c.fetch("0000000000") is None


# --- Louda ------------------------------------------------------------------------------

LOUDA_CAR = {
    "id": 63560, "name": "HYUNDAI I30 Trikolor 1.6 MPI 88 kW manuál", "manufacturer": "Hyundai",
    "model": "i30 combi", "yearOfProduction": 2012, "mileage": 80332, "performace": 88,
    "engineVolume": 1.6, "fuelType": "Benzín", "transmissionType": "Manuální", "price": 144990,
    "carWiseUrl": "/koupit/hyundai-i30-trikolor-16-mpi-88-kw-manual-63560", "pictureList": ["https://p/1"],
}


def test_louda_filter_and_parse():
    p = SearchProfile(name="p", models=[], fuel=["benzin"], body=["kombi"], price_max=200000)
    flt = build_filter(p, {"make": "Hyundai", "models": ["I30", "I30 COMBI"]})
    assert flt["Manufacturers"] == [{"Name": "Hyundai", "Models": ["I30", "I30 COMBI"]}]
    assert (flt["FuelTypes"], flt["BodyTypes"], flt["Prices"]["PriceTo"]) == (["Benzín"], ["Kombi"], 200000)
    l = louda_parse_car(LOUDA_CAR, "hyundai:i30", "kombi")
    assert (l.source_id, l.year, l.mileage_km, l.power_kw, l.engine_cc, l.fuel, l.gearbox, l.body) == (
        "63560", 2012, 80332, 88, 1600, "benzin", "manualni", "kombi")
    assert l.url == "https://autolouda.cz/koupit/hyundai-i30-trikolor-16-mpi-88-kw-manual-63560"


def test_louda_paging_and_gone():
    pages = {1: [LOUDA_CAR], 2: [{**LOUDA_CAR, "id": 2}]}

    def handler(request):
        if request.method == "GET":
            gone = "x-1" in request.url.path
            title = "Vozidlo již není v nabídce | Louda Auto+" if gone else "HYUNDAI I30 1.6 | Louda Auto+"
            return httpx.Response(200, text=f"<title>{title}</title><p>Výkon 88 kW</p>")
        page = json.loads(request.content)["FilterResult"]["Page"]
        return httpx.Response(200, json={"FilterResult": {"TotalCarsCount": 2}, "Cars": pages[page]})

    c = LoudaCollector(HTTP, CollectionSettings(), {"models": {"hyundai:i30": {"make": "Hyundai", "models": ["I30"]}}})
    c._client._client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    result = c.search(SearchProfile(name="p", models=["hyundai:i30"], body=["kombi"]))
    assert sorted(l.source_id for l in result.listings) == ["2", "63560"] and result.complete
    assert c.fetch("1") is None
    live = c.fetch("63560")
    assert live.is_active and live.power_kw == 88 and live.price_czk is None   # merge keeps stored price
    assert louda_detail("<title>Vozidlo již není v nabídce | Louda</title>", "1") is None


# --- Klokočka ---------------------------------------------------------------------------

KLO_CARD = """<article class="car | card" data-car-id="4233" data-car-type="sale"> <div class="card-head">
<img class="img-responsive lazyload-img" src="t.jpg" data-lazyload="https://autoklokocka.cz/getFile/id:1/type:small/a.jpg" /></div>
<div class="card-body | flex-column | text-center"> <h3 class="text-h5"> <a class="card-link" href="/nabidka-vozu/vuz/ojete-skoda-octavia-1-6-mpi-2017-1">Škoda Octavia 1,6 MPI Ambition</a> </h3>
<div class="flow flow-space-3xs"> <p class="text-h4">189 900 Kč</p> <p class="text-s color-grey-100">156 942 Kč bez DPH</p> </div> </div>
<div class="card-foot"> <ul class="flex-row | text-s text-center" role="list"> <li class="flex-grow">Benzín</li> <li class="flex-grow">98 643 km</li> <li class="flex-grow">2017</li> </ul> </div>
</article>"""


def test_klokocka_card_and_params():
    cards = klo_list("<div>Celkem 1 vůz</div>" + KLO_CARD)
    l = listing_from_card(cards[0], "skoda:octavia")
    assert (l.source_id, l.title, l.price_czk, l.year, l.mileage_km, l.fuel, l.engine_cc) == (
        "ojete-skoda-octavia-1-6-mpi-2017-1", "Škoda Octavia 1,6 MPI Ambition", 189900, 2017,
        98643, "benzin", 1600)
    assert not l.has_details
    p = SearchProfile(name="p", models=[], fuel=["benzin"], body=["kombi"], gearbox=["manualni"])
    assert klo_params(p) == [("stav[]", "ojete"), ("palivo[]", "benzin"), ("karoserie[]", "kombi"),
                             ("razeni[]", "manualni")]


def test_klokocka_paging_and_gone():
    def handler(request):
        if "/vuz/" in request.url.path:
            return httpx.Response(404, text="not found")
        page = int(request.url.params.get("page", 1))
        card = KLO_CARD.replace("2017-1", f"2017-{page}")
        return httpx.Response(200, text=f"<div>Celkem 2 vozů</div>{card}")

    c = KlokockaCollector(HTTP, CollectionSettings(), {"models": {"skoda:octavia": {"brand": "skoda", "model": "octavia"}}})
    c._client._client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    result = c.search(SearchProfile(name="p", models=["skoda:octavia"]))
    assert sorted(l.source_id for l in result.listings) == [
        "ojete-skoda-octavia-1-6-mpi-2017-1", "ojete-skoda-octavia-1-6-mpi-2017-2"]
    assert result.complete
    assert c.fetch("gone") is None
