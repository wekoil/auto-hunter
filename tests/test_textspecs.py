"""Free-text spec extraction, against phrasing seen in real Czech listings."""

import pytest

from auto_hunter.classify import fold
from auto_hunter.textspecs import (
    extract, extract_body, extract_fuel, extract_gearbox, extract_mileage, extract_vin,
    extract_year,
)


def f(text):
    return fold(text)


@pytest.mark.parametrize("title, text, year", [
    ("Octavia", "rok výroby: 2016", 2016),
    ("Octavia", "r.v. 2015, STK 2027", 2015),
    ("Octavia", "09/2016 (Německo) | 191 620 km", 2016),
    ("Octavia", "uvedení do provozu:04/2018", 2018),
    # Inspection/service dates are not the car's age.
    ("Octavia", "STK do 09/2025, servis 03/2024", None),
    ("Škoda Octavia 1.6 MPI 2017", "", 2017),
    # Two different bare years in the title: ambiguous.
    ("Octavia 2015 motor 2018", "", None),
])
def test_year(title, text, year):
    assert extract_year(f(title), f(f"{title}\n{text}")) == year


@pytest.mark.parametrize("text, km", [
    ("Najeto: 113 000km", 113000),
    ("191 620 km | Benzín", 191620),
    ("najeto 145 tis. km", 145000),
    ("stav tachometru 98.500", 98500),
    ("km: 120000", 120000),
    # A bare "km" followed by a year must not become the mileage.
    ("145000 km 2016", 145000),
    # Labelled "xxx" mileage beats a service record mentioning another mileage.
    ("Rozvody byly měněny přibližně při 120 000 km. Najeto: 203 xxx km.", 203000),
    ("turbo měněno kolem 140 000 km", None),
    ("bez udání", None),
])
def test_mileage(text, km):
    assert extract_mileage(f(text)) == km


@pytest.mark.parametrize("text, fuel", [
    ("Palivo: Benzín", "benzin"),
    ("1.4 TSI 110 kW", "benzin"),
    ("2.0 TDI 110kW", "nafta"),
    # Code glued to the displacement.
    ("Kia Ceed 1.6CRDi 100kW", "nafta"),
    ("Vw Golf 6 variant 1.6tdi 77kw", "nafta"),
    ("Octavia 1.4tsi 110kw", "benzin"),
    ("1.6 MPI + LPG, benzín", "lpg-benzin"),
    ("ev. č. vozidla 1AB2345, 1.6 MPI", "benzin"),   # "ev." is not electric
    ("benzín i nafta k dispozici", None),
])
def test_fuel(text, fuel):
    assert extract_fuel(f(text)) == fuel


@pytest.mark.parametrize("text, gearbox", [
    ("1.4 TSI DSG", "automaticka"),
    ("Automat, klima", "automaticka"),
    ("6-st. manuál", "manualni"),
    ("manuální převodovka, automatická klimatizace", "manualni"),
    ("automatická klimatizace", None),    # air-con, not the gearbox
    ("manuální klimatizace", None),
])
def test_gearbox(text, gearbox):
    assert extract_gearbox(f(text)) == gearbox


@pytest.mark.parametrize("text, body", [
    ("Škoda Octavia 3 Combi 1.4 TSI", "kombi"),
    ("VW Golf Variant 1.6", "kombi"),
    ("Kia cee'd SW 1.4", "kombi"),
    ("Opel Astra Sports Tourer", "kombi"),
    ("Škoda Octavia liftback", "liftback"),
    ("Škoda Octavia 1.6 MPI, 6 st. manuál", None),   # "st." is not Astra ST
])
def test_body(text, body):
    assert extract_body(f(text)) == body


def test_vin():
    assert extract_vin("VIN: TMBJC7NE0H0062499 | Černá") == "TMBJC7NE0H0062499"
    assert extract_vin("VIN WVWZZZXXXXXXXX259") is None   # masked
    assert extract_vin("ABCDEFGHJKLMNPRST") is None       # no digits: a word, not a VIN


def test_extract_full_listing():
    s = extract(
        "Škoda Octavia 3 Combi 1.4 TSI 110 kW DSG",
        "09/2016 (Německo) | 191 620 km | Benzín | STK 09/2027\n"
        "VIN: TMBJC7NE0H0062499\nKompletní servisní historie",
    )
    assert (s.year, s.mileage_km, s.power_kw, s.engine_cc, s.fuel, s.gearbox, s.body) == (
        2016, 191620, 110, 1400, "benzin", "automaticka", "kombi")
    assert s.vin == "TMBJC7NE0H0062499" and s.service_book is True
    assert extract("Octavia", "prodám bez servisní knížky").service_book is False
