"""Bridges sales history and stored forecasts (FR-014, FR-020).

Sold products are forecast directly; ingredient demand is derived through recipes. The daily run
stores a 30-day horizon so the cash projection never runs short of sales forecast.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.stock.forecasting import forecast_series, mape
from app.agents.stock.tracking import convert
from app.core.country_profiles import calendar_for
from app.models.finance_master import Sale
from app.models.master import Item, RecipeLine
from app.models.stock_ops import DemandForecast
from app.models.tenancy import Business

HISTORY_DAYS = 84
HORIZON_DAYS = 30
KEEP_GENERATIONS_DAYS = 14
Q = Decimal("0.0001")


async def sales_history(s: AsyncSession, business_id: uuid.UUID, end_exclusive: date,
                        days: int = HISTORY_DAYS) -> dict[uuid.UUID, dict[date, float]]:
    """Units sold per product per day. Days with no sales records at all (data gaps) are left out."""
    start = end_exclusive - timedelta(days=days)
    rows = (await s.execute(select(Sale).where(Sale.business_id == business_id, Sale.date >= start,
                                               Sale.date < end_exclusive))).scalars().all()
    observed_days = {r.date for r in rows}
    out: dict[uuid.UUID, dict[date, float]] = defaultdict(dict)
    products = [i.id for i in (await s.execute(select(Item).where(Item.business_id == business_id,
                                                                   Item.is_sold.is_(True)))).scalars()]
    for pid in products:
        for d in observed_days:
            out[pid][d] = 0.0
    for r in rows:
        for line in r.lines:
            pid = uuid.UUID(line["sold_item_id"])
            out[pid][r.date] = out[pid].get(r.date, 0.0) + float(line["qty"])
    return out


async def generate(s: AsyncSession, business_id: uuid.UUID, d: date, horizon: int = HORIZON_DAYS) -> dict[str, str]:
    """Forecast every product and derived ingredient from date d. Returns {item_id: method}."""
    business = await s.get(Business, business_id)
    assert business is not None
    cal = calendar_for(business)
    items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == business_id))).scalars()}
    history = await sales_history(s, business_id, d)
    await s.execute(delete(DemandForecast).where(DemandForecast.business_id == business_id,
                                                 DemandForecast.generated_on == d))
    await s.execute(delete(DemandForecast).where(DemandForecast.business_id == business_id,
                                                 DemandForecast.generated_on < d - timedelta(days=KEEP_GENERATIONS_DAYS)))
    methods: dict[str, str] = {}
    product_fc: dict[uuid.UUID, list[tuple[date, float, float, float]]] = {}
    for pid, hist in history.items():
        item = items[pid]
        fc, used = forecast_series(hist, d, horizon, cal, item.category if item.category in ("drink", "food") else "drink",
                                   item.forecast_method)
        methods[str(pid)] = used
        product_fc[pid] = [(f.date, f.low, f.expected, f.high) for f in fc]
        for f in fc:
            s.add(DemandForecast(business_id=business_id, item_id=pid, forecast_date=f.date,
                                 low=Decimal(str(f.low)).quantize(Q), expected=Decimal(str(f.expected)).quantize(Q),
                                 high=Decimal(str(f.high)).quantize(Q), method=used, generated_on=d,
                                 confidence=0.8 if used == "holt_winters" else 0.6))
    # Derived ingredient demand through recipes.
    recipes = (await s.execute(select(RecipeLine).where(RecipeLine.business_id == business_id))).scalars().all()
    ing: dict[uuid.UUID, dict[date, list[Decimal]]] = defaultdict(lambda: defaultdict(lambda: [Decimal(0)] * 3))
    for r in recipes:
        ingredient = items.get(r.ingredient_item_id)
        if ingredient is None or r.sold_item_id not in product_fc:
            continue
        per_unit = convert(r.quantity, r.unit, ingredient.unit)
        for fd, lo, ex, hi in product_fc[r.sold_item_id]:
            acc = ing[r.ingredient_item_id][fd]
            acc[0] += Decimal(str(lo)) * per_unit
            acc[1] += Decimal(str(ex)) * per_unit
            acc[2] += Decimal(str(hi)) * per_unit
    for iid, by_day in ing.items():
        for fd, (lo, ex, hi) in by_day.items():
            s.add(DemandForecast(business_id=business_id, item_id=iid, forecast_date=fd, low=lo.quantize(Q),
                                 expected=ex.quantize(Q), high=hi.quantize(Q), method="derived", generated_on=d,
                                 confidence=0.8))
        methods[str(iid)] = "derived"
    return methods


async def latest_generation(s: AsyncSession, business_id: uuid.UUID, on_or_before: date) -> date | None:
    return (await s.execute(select(DemandForecast.generated_on).where(
        DemandForecast.business_id == business_id, DemandForecast.generated_on <= on_or_before)
        .order_by(DemandForecast.generated_on.desc()).limit(1))).scalar_one_or_none()


async def daily_demand(s: AsyncSession, business_id: uuid.UUID, generated_on: date,
                       which: str = "expected") -> dict[uuid.UUID, dict[date, Decimal]]:
    rows = (await s.execute(select(DemandForecast).where(DemandForecast.business_id == business_id,
                                                         DemandForecast.generated_on == generated_on))).scalars().all()
    out: dict[uuid.UUID, dict[date, Decimal]] = defaultdict(dict)
    for r in rows:
        out[r.item_id][r.forecast_date] = getattr(r, which)
    return out


async def forecast_vs_actual(s: AsyncSession, business_id: uuid.UUID, item_id: uuid.UUID, day: date) -> tuple[float, float] | None:
    """(forecast made that morning before the day's sales, actual units sold) for one product on one day."""
    fc = (await s.execute(select(DemandForecast).where(DemandForecast.item_id == item_id,
                                                       DemandForecast.forecast_date == day,
                                                       DemandForecast.generated_on == day))).scalar_one_or_none()
    if fc is None:
        fc = (await s.execute(select(DemandForecast).where(DemandForecast.item_id == item_id,
                                                           DemandForecast.forecast_date == day,
                                                           DemandForecast.generated_on < day)
                              .order_by(DemandForecast.generated_on.desc()).limit(1))).scalar_one_or_none()
    if fc is None:
        return None
    sales = (await s.execute(select(Sale).where(Sale.business_id == business_id, Sale.date == day))).scalars().all()
    if not sales:
        return None  # a sales gap pauses the accuracy check for that day
    actual = sum(float(line["qty"]) for sale in sales for line in sale.lines if line["sold_item_id"] == str(item_id))
    return float(fc.expected), actual


async def mape_7d(s: AsyncSession, business_id: uuid.UUID, item_id: uuid.UUID, today: date) -> float | None:
    pairs = []
    for i in range(1, 8):
        pair = await forecast_vs_actual(s, business_id, item_id, today - timedelta(days=i))
        if pair is not None:
            pairs.append(pair)
    return mape(pairs)


async def yesterday_error(s: AsyncSession, business_id: uuid.UUID, item_id: uuid.UUID, today: date) -> float | None:
    pair = await forecast_vs_actual(s, business_id, item_id, today - timedelta(days=1))
    return mape([pair]) if pair else None
