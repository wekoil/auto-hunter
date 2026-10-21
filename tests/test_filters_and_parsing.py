from auto_hunter.collectors.sauto import build_search_params, parse_item
from auto_hunter.config import SearchProfile
from auto_hunter.filters import rejection_reason
from auto_hunter.models import Listing

SAUTO_ITEM = {
    "additional_model_name": "1.4TSI 92kw, COMFORTLINE",
    "id": 211125480,
    "images": [{"url": "//d19-a.sdn.cz/d_19/a.jpeg"}],
    "manufacturer_cb": {"name": "Volkswagen", "seo_name": "volkswagen", "value": 103},
    "model_cb": {"name": "Golf Sportsvan", "seo_name": "golf-sportsvan", "value": 6851},
    "fuel_cb": {"name": "Benzín", "seo_name": "benzin", "value": 1},
    "gearbox_cb": {"name": "Manuální", "seo_name": "manualni", "value": 1},
    "locality": {"district": "Zlín", "region": "Zlínský kraj", "municipality": "Bohuslavice"},
    "manufacturing_date": "2014-10-01",
    "name": "Volkswagen Golf Sportsvan, 1.4TSI 92kw, COMFORTLINE",
    "premise": {"name": "Danny Cars"},
    "price": 229000,
    "price_by_agreement": False,
    "tachometer": 134000,
    "create_date": "2026-09-17T20:35:49",
}


def test_parse_sauto_search_item():
    l = parse_item(SAUTO_ITEM)
    assert l.source_id == "211125480"
    assert l.model_key == "volkswagen:golf-sportsvan"
    assert l.url == "https://www.sauto.cz/osobni/detail/volkswagen/golf-sportsvan/211125480"
    assert (l.make, l.model, l.year, l.mileage_km, l.price_czk) == (
        "Volkswagen", "Golf Sportsvan", 2014, 134000, 229000)
    assert (l.fuel, l.gearbox, l.seller_type, l.seller_name) == (
        "benzin", "manualni", "dealer", "Danny Cars")
    assert l.image_urls == ["https://d19-a.sdn.cz/d_19/a.jpeg"]
    assert l.is_active and not l.has_details


def test_parse_sauto_detail_inactive_and_negotiable():
    l = parse_item({**SAUTO_ITEM, "status": "inactive", "price_by_agreement": True,
                    "premise": None, "vin": ""}, has_details=True)
    assert not l.is_active and l.has_details
    assert l.price_czk is None and l.vin is None and l.seller_type == "private"


def test_parse_sauto_year_falls_back_to_first_registration():
    item = {k: v for k, v in SAUTO_ITEM.items() if k != "manufacturing_date"}
    assert parse_item({**item, "in_operation_date": "2010-08-01"}).year == 2010


def test_search_params():
    p = SearchProfile(name="x", models=["skoda:octavia"], price_max=300000, mileage_max=150000,
                      fuel=["benzin", "nafta"], body=["kombi"])
    params = build_search_params(p, "skoda:octavia")
    assert params["manufacturer_model_seo"] == "skoda:octavia"
    assert params["price_to"] == 300000 and params["tachometer_to"] == 150000
    assert params["fuel_seo"] == "benzin,nafta" and params["vehicle_body_seo"] == "kombi"
    assert "gearbox_seo" not in params


def _l(**kw):
    return Listing(source="t", source_id="1", url="", title=kw.pop("title", "Škoda Octavia"),
                   price_czk=kw.pop("price_czk", 200000), **kw)


def test_filters():
    p = SearchProfile(name="x", models=["a:b"], year_min=2015, year_max=2020, mileage_max=150000,
                      body=["kombi"], exclude_keywords=["havarovaný", "na díly"])
    assert rejection_reason(_l(year=2016, mileage_km=100000, body="kombi"), p) is None
    assert rejection_reason(_l(year=2014), p) == "too old"
    assert rejection_reason(_l(year=2021), p) == "too new"
    assert rejection_reason(_l(mileage_km=200000), p) == "mileage above max"
    assert rejection_reason(_l(body="hatchback"), p) == "body hatchback"
    # Unknown values pass.
    assert rejection_reason(_l(), p) is None
    # Keyword match is case/diacritic-insensitive, but not inside another word.
    assert rejection_reason(_l(title="Octavia HAVAROVANY"), p) is not None
    assert rejection_reason(_l(variant="1.6 TDI - na dily"), p) is not None
    assert rejection_reason(_l(title="Octavia nehavarovaný, 1. majitel"), p) is None


def _collector(items_by_model):
    import httpx

    from auto_hunter.collectors.sauto import SautoCollector
    from auto_hunter.config import CollectionSettings, HttpSettings

    calls = []

    def handler(request):
        calls.append(request.url.params["manufacturer_model_seo"])
        items = items_by_model(request.url.params["manufacturer_model_seo"])
        return httpx.Response(200, json={"pagination": {"total": len(items)}, "results": items})

    c = SautoCollector(HttpSettings(request_delay_s=0), CollectionSettings())
    c._client._client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    return c, calls


def test_sauto_rejects_silently_ignored_model():
    import pytest

    from auto_hunter.collectors.base import CollectorError

    c, _ = _collector(lambda m: [SAUTO_ITEM])  # returns a Golf Sportsvan for anything
    with pytest.raises(CollectorError, match="doesn't recognise"):
        c.search(SearchProfile(name="x", models=["kia:ceed"]))


def test_sauto_shares_identical_queries_between_profiles():
    c, calls = _collector(lambda m: [SAUTO_ITEM])
    p1 = SearchProfile(name="a", models=["volkswagen:golf-sportsvan"], price_max=200000)
    p2 = SearchProfile(name="b", models=["volkswagen:golf-sportsvan"], price_max=200000)
    assert len(c.search(p1).listings) == len(c.search(p2).listings) == 1
    assert calls == ["volkswagen:golf-sportsvan"]
