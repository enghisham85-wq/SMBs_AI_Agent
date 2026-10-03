"""Demand forecasting: Holt-Winters plus holiday/Ramadan uplift, or a same-weekday average as the safe fallback."""

from __future__ import annotations

import math
import statistics
import warnings
from dataclasses import dataclass
from datetime import date, timedelta

from app.core.country_profiles import Calendar

PRIOR_HOLIDAY = 1.2
PRIOR_RAMADAN = {"drink": 1.1, "food": 0.9}
MIN_HISTORY_DAYS = 21


@dataclass
class DayForecast:
    date: date
    low: float
    expected: float
    high: float


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def learn_factors(history: dict[date, float], cal: Calendar, kind: str) -> dict[str, float]:
    """Holiday/Ramadan uplift = mean(actual / same-weekday baseline) on those days; priors if unseen."""
    normal = [d for d in history if not cal.is_holiday(d) and not cal.is_ramadan(d)]
    by_wd: dict[int, list[float]] = {}
    for d in normal:
        by_wd.setdefault(d.weekday(), []).append(history[d])
    base = {wd: statistics.fmean(v) for wd, v in by_wd.items() if v}
    hol =[history[d] / base[d.weekday()] for d in history if cal.is_holiday(d) and base.get(d.weekday())]
    ram = [history[d] / base[d.weekday()] for d in history
           if cal.is_ramadan(d) and not cal.is_holiday(d) and base.get(d.weekday())]
    return {
        "holiday": statistics.fmean(hol) if len(hol) >= 2 else PRIOR_HOLIDAY,
        "ramadan": statistics.fmean(ram) if len(ram) >= 5 else PRIOR_RAMADAN.get(kind, 1.0),
    }


def calendar_factor(d: date, cal: Calendar, factors: dict[str, float]) -> float:
    f = 1.0
    if cal.is_holiday(d):
        f *= factors["holiday"]
    if cal.is_ramadan(d):
        f *= factors["ramadan"]
    return f


def same_weekday_avg(history: dict[date, float], start: date, horizon: int, cal: Calendar,
                     factors: dict[str, float]) -> list[DayForecast]:
    days = sorted(history)
    out: list[DayForecast] = []
    for i in range(horizon):
        d = start + timedelta(days=i)
        same = [history[x] / calendar_factor(x, cal, factors) for x in days if x.weekday() == d.weekday()][-4:]
        if not same:
            same = [0.0]
        f = calendar_factor(d, cal, factors)
        out.append(DayForecast(d, min(same) * f, statistics.fmean(same) * f, max(same) * f))
    return out


def holt_winters(history: dict[date, float], start: date, horizon: int, cal: Calendar,
                 factors: dict[str, float]) -> list[DayForecast] | None:
    days = sorted(history)
    if len(days) < MIN_HISTORY_DAYS or sum(history.values()) == 0:
        return None
    # Continuous daily series ending the day before `start`.
    series = [history.get(days[0] + timedelta(days=i), 0.0) for i in range((days[-1] - days[0]).days + 1)]
    adj = [v / calendar_factor(days[0] + timedelta(days=i), cal, factors) for i, v in enumerate(series)]
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = ExponentialSmoothing(adj, trend=None, seasonal="add", seasonal_periods=7,
                                       initialization_method="estimated").fit()
        fitted = list(fit.fittedvalues)
        pred = list(fit.forecast(horizon + (start - days[-1]).days - 1))
    except Exception:
        return None
    ratios = [(a - f) / max(f, 1.0) for a, f in zip(adj, fitted, strict=False)]
    q10, q90 = _quantile(ratios, 0.10), _quantile(ratios, 0.90)
    offset = (start - days[-1]).days - 1
    out: list[DayForecast] = []
    for i in range(horizon):
        d = start + timedelta(days=i)
        base = max(0.0, pred[offset + i])
        exp = base * calendar_factor(d, cal, factors)
        out.append(DayForecast(d, max(0.0, exp * (1 + q10)), exp, max(exp, exp * (1 + q90))))
    return out


def forecast_series(history: dict[date, float], start: date, horizon: int, cal: Calendar, kind: str,
                    method: str) -> tuple[list[DayForecast], str]:
    """Return (daily forecasts, method actually used)."""
    factors = learn_factors(history, cal, kind)
    if method == "holt_winters":
        hw = holt_winters(history, start, horizon, cal, factors)
        if hw is not None:
            return hw, "holt_winters"
    return same_weekday_avg(history, start, horizon, cal, factors), "same_weekday_avg"


def mape(pairs: list[tuple[float, float]]) -> float | None:
    """Mean absolute percentage error of (forecast, actual) pairs; actual floored at 1 unit."""
    if not pairs:
        return None
    return statistics.fmean(abs(f - a) / max(a, 1.0) for f, a in pairs)
