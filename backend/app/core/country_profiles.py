"""Configurable country profiles. Default: Egypt (EGP, VAT 14 %).

Nothing country-specific is hard-coded elsewhere: forecasting, the demo feed and weekend logic
call `calendar_for()` and the Business row (filled from a profile by `apply()`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from hijridate import Gregorian, Hijri
from pydantic import BaseModel, Field, field_validator

from app.db.currencies import exponent
from app.db.types import Money

PROFILE_DIR = Path(__file__).resolve().parent.parent / "seed" / "countries"


class FixedHoliday(BaseModel):
    month: int
    day: int
    name_en: str
    name_ar: str


class HijriHoliday(BaseModel):
    month: int
    day: int
    days: int = 1
    name_en: str
    name_ar: str


class CountryProfile(BaseModel):
    code: str
    name_en: str
    name_ar: str
    currency: str
    vat_rate_percent: Decimal = Field(ge=0, le=100)
    vat_period: str
    weekend_days: list[int]
    tax_id_pattern: str
    default_date_format: str
    fixed_holidays: list[FixedHoliday] = []
    hijri_holidays: list[HijriHoliday] = []
    special_holidays: list[str] = []
    # {"2026": {"add": [{"date": "2026-03-21", "name_en": ..., "name_ar": ...}], "remove": ["2026-03-20"]}}
    holiday_overrides: dict[str, dict[str, list[Any]]] = {}
    # {"2026": {"start": "2026-02-18", "end": "2026-03-19"}} for local moon sighting
    ramadan_overrides: dict[str, dict[str, str]] = {}
    money_defaults: dict[str, Decimal]
    seed_price_factor: Decimal

    @field_validator("currency")
    @classmethod
    def _currency_known(cls, v: str) -> str:
        exponent(v)
        return v


@lru_cache
def load(code: str) -> CountryProfile:
    path = PROFILE_DIR / f"{code.upper()}.json"
    if not path.exists():
        raise KeyError(f"unknown country profile {code!r}")
    return CountryProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))


def available() -> list[CountryProfile]:
    return [load(p.stem) for p in sorted(PROFILE_DIR.glob("*.json"))]


def _orthodox_easter(year: int) -> date:
    """Julian-calendar Easter (Meeus) converted to Gregorian (valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    return date(year, month, day) + timedelta(days=13)


def _hijri_years(year: int) -> list[int]:
    start = Gregorian(year, 1, 1).to_hijri().year
    return [start, start + 1]


def _hijri_to_date(hy: int, hm: int, hd: int) -> date | None:
    try:
        g = Hijri(hy, hm, hd).to_gregorian()
    except (ValueError, OverflowError):
        return None
    return date(g.year, g.month, g.day)


@lru_cache(maxsize=64)
def holidays(code: str, year: int) -> dict[date, tuple[str, str]]:
    """Public holidays for a year: {date: (name_en, name_ar)}."""
    p = load(code)
    out: dict[date, tuple[str, str]] = {}
    for h in p.fixed_holidays:
        out[date(year, h.month, h.day)] = (h.name_en, h.name_ar)
    for hy in _hijri_years(year):
        for hh in p.hijri_holidays:
            first = _hijri_to_date(hy, hh.month, hh.day)
            if first is None:
                continue
            for i in range(hh.days):
                d = first + timedelta(days=i)
                if d.year == year:
                    out[d] = (hh.name_en, hh.name_ar)
    if "sham_el_nessim" in p.special_holidays:
        out[_orthodox_easter(year) + timedelta(days=1)] = ("Sham El-Nessim", "شم النسيم")
    ov = p.holiday_overrides.get(str(year), {})
    for d in ov.get("remove", []):
        out.pop(date.fromisoformat(d), None)
    for item in ov.get("add", []):
        out[date.fromisoformat(item["date"])] = (item["name_en"], item["name_ar"])
    return dict(sorted(out.items()))


@lru_cache(maxsize=64)
def ramadan(code: str, year: int) -> tuple[date, ...]:
    """Ramadan days that fall in a Gregorian year."""
    p = load(code)
    ov = p.ramadan_overrides.get(str(year))
    days: list[date] = []
    if ov:
        d, end = date.fromisoformat(ov["start"]), date.fromisoformat(ov["end"])
        while d <= end:
            days.append(d)
            d += timedelta(days=1)
        return tuple(days)
    for hy in _hijri_years(year):
        first = _hijri_to_date(hy, 9, 1)
        if first is None:
            continue
        length = Hijri(hy, 9, 1).month_length()
        for i in range(length):
            d = first + timedelta(days=i)
            if d.year == year:
                days.append(d)
    return tuple(sorted(days))


@dataclass(frozen=True)
class Calendar:
    """What forecasting, the demo feed and weekend logic ask about a date."""

    country: str
    weekend_days: tuple[int, ...]

    def is_weekend(self, d: date) -> bool:
        return d.isoweekday() in self.weekend_days

    def holiday(self, d: date) -> tuple[str, str] | None:
        return holidays(self.country, d.year).get(d)

    def is_holiday(self, d: date) -> bool:
        return self.holiday(d) is not None

    def is_ramadan(self, d: date) -> bool:
        return d in ramadan(self.country, d.year)


def calendar_for(business: Any) -> Calendar:
    return Calendar(country=business.country, weekend_days=tuple(business.weekend_days))


def money_defaults(profile: CountryProfile) -> dict[str, Money]:
    return {k: Money.from_decimal(v, profile.currency) for k, v in profile.money_defaults.items()}


def apply(business: Any, profile: CountryProfile, *, creating: bool, vat_override: Decimal | None = None) -> dict[str, Any]:
    """Copy a profile into a Business row. Returns settings to store (minor units).

    On creation the currency and money defaults are applied too; afterwards only the non-money
    fields change, from the current business date forward (posted records are never rewritten).
    """
    business.country = profile.code
    business.vat_rate_percent = vat_override if (creating and vat_override is not None) else profile.vat_rate_percent
    business.vat_period = profile.vat_period
    business.weekend_days = list(profile.weekend_days)
    business.tax_id_pattern = profile.tax_id_pattern
    business.default_date_format = profile.default_date_format
    settings: dict[str, Any] = {}
    if creating:
        business.currency = profile.currency
        md = money_defaults(profile)
        business.min_cash_buffer = md["min_cash_buffer"]
        settings["journal_value_limit"] = md["journal_value_limit"].amount_minor
        settings["po_auto_approve_limit"] = md["po_auto_approve_limit"].amount_minor
    return settings
