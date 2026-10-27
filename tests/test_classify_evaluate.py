"""Classification, profile matching and evaluation against the real config files."""

from pathlib import Path

import pytest

from auto_hunter.classify import classify
from auto_hunter.config import load_config
from auto_hunter.evaluate import assess
from auto_hunter.filters import rejection_reason
from auto_hunter.models import Listing

CONFIG = load_config(Path(__file__).parent.parent / "config")
CATALOG = CONFIG.catalog
PROFILES = {p.name: p for p in CONFIG.profiles}


def car(model_key: str, year: int, title: str, **kw) -> Listing:
    return Listing(source="t", source_id="1", url="", title=title, model_key=model_key,
                   year=year, price_czk=kw.pop("price_czk", 140_000), fuel="benzin",
                   body="kombi", **kw)


@pytest.mark.parametrize("listing, generations, engines", [
    (car("skoda:octavia", 2016, "Škoda Octavia, 1.6 MPI", power_kw=81),
     ["octavia-3"], ["ea211-1.6-mpi"]),
    # Changeover year with no power: both generations and both 1.4 TSI families possible.
    (car("skoda:octavia", 2013, "Škoda Octavia, 1.4 TSI Elegance"),
     ["octavia-2", "octavia-3"], ["ea111-1.4-tsi", "ea211-1.4-tsi"]),
    # Power resolves it: 103 kW only exists as EA211 in the Octavia.
    (car("skoda:octavia", 2013, "Škoda Octavia, 1.4 TSI", power_kw=103),
     ["octavia-3"], ["ea211-1.4-tsi"]),
    # Title hint resolves the generation; "2.0" must not read as "Octavia 2".
    (car("skoda:octavia", 2013, "Škoda Octavia, 3 FL COMBI 1.4TSI"),
     ["octavia-3"], ["ea211-1.4-tsi"]),
    (car("volkswagen:golf-variant", 2011, "VW Golf Variant 1.4 TSI 90kW"),
     ["golf-6"], ["ea111-1.4-tsi"]),
    (car("volkswagen:golf", 2013, "Volkswagen Golf, 1.4 TSI 90kW"),
     ["golf-6", "golf-7"], ["ea111-1.4-tsi", "ea211-1.4-tsi"]),
    (car("volkswagen:golf", 2010, "Volkswagen Golf, 1.6 MPI", engine_cc=1595, power_kw=75),
     ["golf-6"], ["ea113-1.6-mpi"]),
    # Same displacement, turbo keyword decides.
    (car("ford:focus", 2014, "Ford Focus, 1.6 Ti-VCT Trend"), ["focus-3"], ["ford-1.6-tivct"]),
    (car("ford:focus", 2014, "Ford Focus, 1.6"), ["focus-3"],
     ["ford-1.6-tivct", "ford-1.6-ecoboost"]),
    # 1.6 GDI and 1.6 MPI share 88 kW; the title keyword decides, power alone can't.
    (car("hyundai:i30", 2013, "Hyundai i30, 1.6 GDI, Serv.kniha", power_kw=99),
     ["i30-2"], ["hyundai-kia-1.6-gdi"]),
    (car("hyundai:i30", 2016, "Hyundai i30, 1.6 GDI 88kW"), ["i30-2"], ["hyundai-kia-1.6-gdi"]),
    (car("hyundai:i30", 2012, "Hyundai i30, Trikolor 1.6 MPI 88 kW", power_kw=88),
     ["i30-1", "i30-2"], ["hyundai-kia-1.6-mpi"]),
    (car("kia:cee-d", 2014, "Kia Cee´d, 1.6 88kW"), ["ceed-2"],
     ["hyundai-kia-1.6-mpi", "hyundai-kia-1.6-gdi"]),
    # "T-GDI" is the turbo, not the 1.6 GDI.
    (car("kia:cee-d-sw", 2016, "Kia Cee´d SW 1.0 T-GDI"), ["ceed-2"], ["hyundai-kia-1.0-tgdi"]),
])
def test_classify(listing, generations, engines):
    cls = classify(listing, CATALOG)
    assert (cls.generations, cls.engines) == (generations, engines)


def test_octavia2_title_hint_not_confused_by_displacement():
    cls = classify(car("skoda:octavia", 2013, "Škoda Octavia 2.0 TSI RS", power_kw=162), CATALOG)
    assert cls.generations == ["octavia-3"]


def test_vin_type_code_settles_changeover_year():
    title = "Škoda Octavia, 1.2 TSI 77kW"
    assert classify(car("skoda:octavia", 2013, title), CATALOG).generations == [
        "octavia-2", "octavia-3"]
    octavia3 = classify(car("skoda:octavia", 2013, title, vin="TMBJG7NE0D0000001"), CATALOG)
    assert (octavia3.generation, octavia3.engine) == ("octavia-3", "ea211-1.2-tsi")
    octavia2 = classify(car("skoda:octavia", 2013, title, vin="TMBHT61Z0D2000001"), CATALOG)
    assert (octavia2.generation, octavia2.engine) == ("octavia-2", "ea111-1.2-tsi")
    # An unknown code doesn't narrow anything.
    assert classify(car("skoda:octavia", 2013, title, vin="XXXXXXXXXXXXXXXXX"),
                    CATALOG).generations == ["octavia-2", "octavia-3"]


def test_uncataloged_model():
    cls = classify(car("opel:astra", 2015, "Opel Astra Sports Tourer 1.4 Turbo"), CATALOG)
    assert not cls.cataloged_model and cls.engine_known


def reject(listing: Listing, profile: str):
    return rejection_reason(listing, PROFILES[profile], classify(listing, CATALOG), CATALOG)


def test_profiles_route_by_engine():
    tsi = car("skoda:octavia", 2016, "Škoda Octavia, 1.4 TSI", power_kw=103)
    assert reject(tsi, "A-octavia3-1.6mpi") is not None
    assert reject(tsi, "D-octavia3-other-petrol") is None
    mpi = car("skoda:octavia", 2016, "Škoda Octavia, 1.6 MPI", power_kw=81)
    assert reject(mpi, "A-octavia3-1.6mpi") is None
    assert reject(mpi, "D-octavia3-other-petrol") is not None
    # Unknown engine stays in both, to be flagged rather than dropped.
    unknown = car("skoda:octavia", 2016, "Škoda Octavia Combi Style")
    assert reject(unknown, "A-octavia3-1.6mpi") is None
    assert reject(unknown, "D-octavia3-other-petrol") is None
    # Engine outside the catalog (e.g. RS) is dropped from engine-restricted profiles.
    rs = car("skoda:octavia", 2016, "Škoda Octavia RS 2.0 TSI", power_kw=169)
    assert reject(rs, "D-octavia3-other-petrol") is not None


def test_gdi_profile():
    gdi = car("kia:cee-d", 2014, "Kia Cee´d, 1.6 GDI", engine_cc=1591, power_kw=99)
    assert reject(gdi, "G-i30-ceed-1.6gdi") is None
    assert reject(car("kia:cee-d", 2014, "Kia Cee´d, 1.4 CVVT", power_kw=73),
                  "G-i30-ceed-1.6gdi") is not None
    # 88 kW without a keyword could be either 1.6, so it stays and gets flagged.
    assert reject(car("hyundai:i30", 2015, "Hyundai i30, 1.6 88kW"), "G-i30-ceed-1.6gdi") is None
    assert reject(car("hyundai:i30", 2015, "Hyundai i30, 1.6 MPI 88kW"),
                  "G-i30-ceed-1.6gdi") is not None
    assert reject(car("hyundai:i30", 2019, "Hyundai i30, 1.0 T-GDI"), "G-i30-ceed-1.6gdi") is not None


def test_profiles_route_by_generation():
    old = car("skoda:octavia", 2010, "Škoda Octavia, 1.6 MPI", power_kw=75)
    assert reject(old, "A-octavia3-1.6mpi") == "too old"
    assert reject(car("skoda:fabia", 2016, "Škoda Fabia"), "A-octavia3-1.6mpi").startswith("model")


def test_profile_resolution():
    a = PROFILES["A-octavia3-1.6mpi"]
    assert a.models == ["skoda:octavia"]
    assert (a.year_min, a.year_max, a.price_max, a.fuel, a.body) == (
        2013, 2020, 200000, ["benzin", "hybridni"],
        ["kombi", "liftback", "hatchback", "sedanlimuzina"])
    assert "havarovan" in a.exclude_keywords and a.price_min == 20000
    b = PROFILES["B-golf-variant-1.6mpi"]
    assert b.models == ["volkswagen:golf", "volkswagen:golf-variant"] and b.body == ["kombi"]


def codes(a):
    return {f.code for f in a.flags}


def test_assess_preferred_engine_full_details():
    l = car("skoda:octavia", 2016, "Škoda Octavia, 1.6 MPI", power_kw=81, mileage_km=120_000,
            service_book=True, vin="TMBJC7NE0H0062499", has_details=True, gearbox="manualni")
    a = assess(l, classify(l, CATALOG), CATALOG, CONFIG.evaluation)
    assert (a.generation, a.engine, a.engine_status, a.price_tier, a.confidence) == (
        "octavia-3", "ea211-1.6-mpi", "preferred", "target", "high")
    assert codes(a) == {"tier_target"}


def test_assess_flags_risks_and_uncertainty():
    l = car("volkswagen:golf", 2013, "Volkswagen Golf, 1.4 TSI", price_czk=180_000,
            mileage_km=195_000, gearbox="automaticka", has_details=True)
    a = assess(l, classify(l, CATALOG), CATALOG, CONFIG.evaluation)
    assert {"generation_unclear", "engine_unclear", "engine_avoid", "gearbox_caution",
            "mileage_high", "tier_stretch", "service_unknown", "vin_missing"} <= codes(a)
    assert a.price_tier == "stretch" and a.confidence == "low"
    assert a.engine is None


def test_assess_exploratory_model():
    l = car("opel:astra", 2015, "Opel Astra Sports Tourer 1.6", price_czk=95_000)
    a = assess(l, classify(l, CATALOG), CATALOG, CONFIG.evaluation)
    assert "engine_unvetted" in codes(a) and a.price_tier == "budget"


def test_avoided_engines_rejected_only_when_certain():
    # Golf VI 1.4 TSI can only be EA111: rejected everywhere.
    ea111 = car("volkswagen:golf-variant", 2011, "VW Golf Variant 1.4 TSI 90kW")
    assert reject(ea111, "C-golf-variant-1.4tsi") is not None
    assert reject(ea111, "B-golf-variant-1.6mpi") is not None
    octavia2_12 = car("skoda:octavia", 2012, "Škoda Octavia, 1.2 TSI", power_kw=77)
    assert reject(octavia2_12, "D-octavia3-other-petrol") is not None
    # 2013 Golf 1.4 TSI 90 kW could be EA111 (Golf VI) or EA211 (Golf VII): kept, warned.
    maybe = car("volkswagen:golf", 2013, "Volkswagen Golf, 1.4 TSI 90kW")
    assert reject(maybe, "C-golf-variant-1.4tsi") is None
    a = assess(maybe, classify(maybe, CATALOG), CATALOG, CONFIG.evaluation)
    assert "engine_avoid" in codes(a)
    # Golf VII 1.4 TSI 92 kW is unambiguous EA211.
    ok = car("volkswagen:golf", 2016, "Volkswagen Golf, 1.4 TSI", power_kw=92)
    assert reject(ok, "C-golf-variant-1.4tsi") is None
