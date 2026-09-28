"""ISO 4217 minor-unit exponents for the currencies the suite supports."""

from __future__ import annotations

EXPONENTS: dict[str, int] = {
    "EGP": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AED": 2,
    "SAR": 2,
    "QAR": 2,
    "MAD": 2,
    "OMR": 3,
    "KWD": 3,
    "BHD": 3,
    "JOD": 3,
    "TND": 3,
    "IQD": 3,
    "LYD": 3,
}


class UnknownCurrencyError(ValueError):
    pass


def exponent(currency: str) -> int:
    try:
        return EXPONENTS[currency.upper()]
    except KeyError as exc:
        raise UnknownCurrencyError(f"Unsupported currency code: {currency!r}") from exc
