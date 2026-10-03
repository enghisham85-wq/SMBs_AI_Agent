"""Money arithmetic/display and Arabic normalisation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.i18n import normalize_arabic_name, normalize_digits
from app.db.currencies import UnknownCurrencyError
from app.db.types import CurrencyMismatchError, Money


def test_two_decimal_currency_display_and_arithmetic() -> None:
    a = Money.from_decimal("1800", "EGP")
    assert a.amount_minor == 180000
    assert a.to_display() == "EGP 1,800.00"
    assert (a + Money.from_decimal("0.50", "EGP")).to_display() == "EGP 1,800.50"
    assert a.percent(14) == Money.from_decimal("252.00", "EGP")


def test_three_decimal_currency_display() -> None:
    m = Money.from_decimal("36", "OMR")
    assert m.amount_minor == 36000
    assert m.to_display() == "OMR 36.000"
    assert m.to_json()["decimals"] == 3


def test_rounding_half_up_to_minor_unit() -> None:
    assert Money.from_decimal("10.005", "EGP").amount_minor == 1001
    assert Money(1005, "EGP").percent(Decimal("14")).amount_minor == 141  # 140.7 -> 141


def test_unknown_currency_raises() -> None:
    with pytest.raises(UnknownCurrencyError):
        Money(1, "XXX")


def test_mismatched_currency_raises() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(1, "EGP") + Money(1, "OMR")


def test_digit_normalisation() -> None:
    assert normalize_digits("٣٦٫٥٠٠") == "36.500"
    assert normalize_digits("۱۲۳") == "123"
    assert normalize_digits("١٬٨٠٠٫٠٠") == "1800.00"


def test_arabic_name_variants_match() -> None:
    variants = ["مزرعة النور", "مَزْرَعَة النُّور", "مزرعـة النور", "مزرعه النور"]
    assert len({normalize_arabic_name(v) for v in variants}) == 1
    assert normalize_arabic_name("إبراهيم") == normalize_arabic_name("ابراهيم")
    assert normalize_arabic_name("  Al Noor  Dairy ") == "al noor dairy"
