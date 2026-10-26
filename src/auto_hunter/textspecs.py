"""Extract car specs from free text (title + description) for unstructured sources.

Sellers write specs in endless formats: "r.v. 2016", "09/2016", "191 620 km",
"145 tis. km", "1.4 TSI 92kW", "Palivo: Benzín". Each extractor prefers explicit,
labelled values and returns None rather than guess when the text is ambiguous.
Everything works on fold()-ed text (lowercase, no diacritics).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from auto_hunter.classify import fold

MIN_YEAR = 1990
_YEAR = r"((?:19|20)\d{2})"
_MONTH_PREFIX = r"(?:\d{1,2}\s*[/.]\s*)?"

# Labelled manufacturing year beats first registration beats a bare year in the title.
_YEAR_LABELLED = re.compile(
    r"(?:rok vyroby|r\.\s*v\.|\brv\b|vyrobeno|rok vyr\.?|modelovy rok|\brok)\s*:?\s*"
    + _MONTH_PREFIX + _YEAR
)
_YEAR_REGISTERED = re.compile(
    r"(?:uvedeni do provozu|1\.\s*registrace|prvni registrace|registrace|\b1\.\s*reg\.?)\s*:?\s*"
    + _MONTH_PREFIX + _YEAR
)
# "09/2016" style; see _NOT_PRODUCTION_DATE for dates that aren't the car's age.
_YEAR_MONTH = re.compile(r"\b(?:0?[1-9]|1[0-2])\s*/\s*" + _YEAR + r"\b")
_NOT_PRODUCTION_DATE = re.compile(r"stk|\btk\b|platn|\bdo\b|servis|zaruk|pneu|rozvod")
_YEAR_BARE = re.compile(r"\b" + _YEAR + r"\b")

_KM_NUMBER = r"(\d{1,3}(?:[ . ]\d{3})+|\d{3,6})"
# A bare "km" label needs a colon: "145000 km 2016" must not read as 2016 km.
_KM_LABELLED = re.compile(
    r"(?:najeto|najezd|najeto km|najazdene km|tachometr|stav tachometru|stav km|\bkm\s*:)"
    r"\s*:?\s*" + _KM_NUMBER
)
_KM_PLAIN = re.compile(_KM_NUMBER + r"\s*km\b")
_THOUSANDS = r"(\d{2,3})\s*(?:tis\.?|tisic|tkm|xxx)"
_KM_THOUSANDS = re.compile(r"\b" + _THOUSANDS + r"\s*(?:km)?\b")
_KM_LABELLED_THOUSANDS = re.compile(
    r"(?:najeto|najezd|tachometr|stav tachometru|stav km|\bkm\s*:)\s*:?\s*(?:cca\s*)?" + _THOUSANDS
)
# "rozvody měněny při 120 000 km" is a service record, not the odometer.
_NOT_ODOMETER = re.compile(r"\bpri\b|kolem|vymen|menen|rozvod|turbo|spojk|\bolej|kazdych|interval")

_KW = re.compile(r"\b(\d{2,3})\s*kw\b")
_LITERS = re.compile(r"(?<![\d.,])([0-3])[.,](\d)(?!\d)")
_VIN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")

_FUEL_LABELLED = re.compile(r"palivo\s*:?\s*([a-z()-]+)")
# Engine codes are often glued to the displacement ("1.6crdi", "1.4tsi"), so they only
# need a non-letter before them, not a word boundary.
_C = r"(?<![a-z])"
_FUEL_WORDS = [
    ("lpg-benzin", r"\blpg\b"),
    ("cng-benzin", r"\bcng\b|g-tec"),
    ("hybridni", r"hybrid"),
    ("elektro", r"\belektro(?:mobil)?\b|\belektricky pohon\b"),
    ("nafta", rf"\bnafta\b|diesel|{_C}tdi\b|{_C}tdci\b|{_C}crdi\b|{_C}cdti\b|{_C}hdi\b"
              rf"|{_C}dci\b|d-4d|{_C}jtd|multijet|{_C}cdi\b"),
    ("benzin", rf"benzin|{_C}tsi\b|{_C}tfsi\b|{_C}mpi\b|{_C}fsi\b|ti-?vct|ecoboost|vvt-?i"
               rf"|valvematic|{_C}gdi\b|{_C}t-gdi\b|{_C}cvvt\b"),
]
# "automatická/manuální klimatizace" (air-con) must not set the gearbox.
_NOT_AIRCON = r"\b(?!\.?\s*klim)"
_AUTOMATIC = re.compile(
    r"\bdsg\b|tiptronic|powershift|\bcvt\b|\bautomat(?:icka|ic)?" + _NOT_AIRCON
)
_MANUAL = re.compile(r"\bmanual(?:ni|na)?" + _NOT_AIRCON)

# No bare "st" for Astra ST: "6 st. manual" (6-speed) would turn every car into an estate.
_BODY_WORDS = [
    ("kombi", r"\bkombi\b|\bcombi\b|\bvariant\b|\bsw\b|touring|\btourer\b|\bcaravan\b|\bestate\b|\bavant\b|\bcw\b"),
    ("liftback", r"\bliftback\b"),
    ("hatchback", r"\bhatchback\b|\bhb\b"),
    ("sedanlimuzina", r"\bsedan\b|\blimuzina\b"),
]
_NO_SERVICE_BOOK = re.compile(r"bez servisni (?:knizky|knihy|historie)")
_SERVICE_BOOK = re.compile(r"servisni (?:knizk|kniha|knihu|historie)|servisovan|servisni zaznamy")


@dataclass
class TextSpecs:
    year: int | None = None
    mileage_km: int | None = None
    power_kw: int | None = None
    engine_cc: int | None = None
    fuel: str | None = None
    gearbox: str | None = None
    body: str | None = None
    vin: str | None = None
    service_book: bool | None = None


def _valid_year(y: int) -> bool:
    return MIN_YEAR <= y <= date.today().year


def extract_year(title: str, text: str) -> int | None:
    for pattern in (_YEAR_LABELLED, _YEAR_REGISTERED):
        for m in pattern.finditer(text):
            if _valid_year(int(m.group(1))):
                return int(m.group(1))
    for m in _YEAR_MONTH.finditer(text):
        context = text[max(0, m.start() - 15):m.start()]
        if _valid_year(int(m.group(1))) and not _NOT_PRODUCTION_DATE.search(context):
            return int(m.group(1))
    # A bare year is trusted only in the title ("Octavia 1.6 MPI 2016").
    years = [int(y) for y in _YEAR_BARE.findall(title) if _valid_year(int(y))]
    return years[0] if len(set(years)) == 1 else None


def _km(raw: str) -> int:
    return int(re.sub(r"[ . ]", "", raw))


def extract_mileage(text: str) -> int | None:
    for m in _KM_LABELLED.finditer(text):
        km = _km(m.group(1))
        if 1000 <= km <= 600_000:
            return km
    for m in _KM_LABELLED_THOUSANDS.finditer(text):
        return int(m.group(1)) * 1000
    for m in _KM_PLAIN.finditer(text):
        km = _km(m.group(1))
        context = text[max(0, m.start() - 25):m.start()]
        if 1000 <= km <= 600_000 and not _NOT_ODOMETER.search(context):
            return km
    for m in _KM_THOUSANDS.finditer(text):
        return int(m.group(1)) * 1000
    return None


def extract_fuel(text: str) -> str | None:
    if m := _FUEL_LABELLED.search(text):
        word = m.group(1)
        for fuel, pattern in _FUEL_WORDS:
            if re.search(pattern, word):
                return fuel
    found = {fuel for fuel, pattern in _FUEL_WORDS if re.search(pattern, text)}
    # LPG/CNG conversions also mention petrol; hybrids also mention benzin.
    for specific in ("lpg-benzin", "cng-benzin", "hybridni", "elektro"):
        if specific in found:
            return specific
    if len(found) == 1:
        return found.pop()
    return None  # both petrol and diesel words: can't tell


def extract_gearbox(text: str) -> str | None:
    auto, manual = bool(_AUTOMATIC.search(text)), bool(_MANUAL.search(text))
    if auto != manual:
        return "automaticka" if auto else "manualni"
    return None


def extract_body(text: str) -> str | None:
    for body, pattern in _BODY_WORDS:
        if re.search(pattern, text):
            return body
    return None


def extract_vin(original_text: str) -> str | None:
    for m in _VIN.finditer(original_text.upper()):
        vin = m.group(1)
        if re.search(r"\d", vin) and re.search(r"[A-Z]", vin) and "XXX" not in vin:
            return vin
    return None


def extract(title: str, description: str) -> TextSpecs:
    """Best-effort specs from a listing's title and description."""
    t, d = fold(title), fold(description)
    both = f"{t}\n{d}"
    specs = TextSpecs(
        year=extract_year(t, both),
        mileage_km=extract_mileage(both),
        fuel=extract_fuel(both),
        gearbox=extract_gearbox(both),
        body=extract_body(both),
        vin=extract_vin(f"{title}\n{description}"),
    )
    if m := _KW.search(t) or _KW.search(d):
        if 30 <= int(m.group(1)) <= 400:
            specs.power_kw = int(m.group(1))
    # Displacement only from the title: descriptions are full of other decimals.
    if m := _LITERS.search(t):
        specs.engine_cc = int(m.group(1)) * 1000 + int(m.group(2)) * 100
    if _NO_SERVICE_BOOK.search(d):
        specs.service_book = False
    elif _SERVICE_BOOK.search(d):
        specs.service_book = True
    return specs
