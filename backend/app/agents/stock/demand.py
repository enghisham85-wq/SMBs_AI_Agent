"""Bridges sales history and stored forecasts; ingredient demand comes through recipes.

We store 30 days ahead so the cash projection never runs out of sales forecast.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.stock.forecasting import forecast_series, mape
from app.agents.stock.tracking import convert
from app.core.country_profiles import Calendar, calendar_for
from app.db.engine import read_session, write_session
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


@dataclass(frozen=True)
class _Recipe:
    sold_item_id: uuid.UUID
    ingredient_item_id: uuid.UUID
    quantity: Decimal
    unit: str


def _compute(business_id: uuid.UUID, d: date, horizon: int, cal: Calendar, history: dict[uuid.UUID, dict[date, float]],
             products: dict[uuid.UUID, tuple[str, str]], units: dict[uuid.UUID, str],
             recipes: list[_Recipe]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Pure CPU (statsmodels), so it runs in a worker thread."""
    rows: list[dict[str, Any]] = []
    methods: dict[str, str] = {}
    product_fc: dict[uuid.UUID, list[tuple[date, float, float, float]]] = {}
    for pid, hist in history.items():
        category, method = products[pid]
        fc, used = forecast_series(hist, d, horizon, cal, category if category in ("drink", "food") else "drink", method)
        methods[str(pid)] = used
        product_fc[pid] = [(f.date, f.low, f.expected, f.high) for f in fc]
        for f in fc:
            rows.append(dict(business_id=business_id, item_id=pid, forecast_date=f.date,
                             low=Decimal(str(f.low)).quantize(Q), expected=Decimal(str(f.expected)).quantize(Q),
                             high=Decimal(str(f.high)).quantize(Q), method=used, generated_on=d,
                             confidence=0.8 if used == "holt_winters" else 0.6))
    ing: dict[uuid.UUID, dict[date, list[Decimal]]] = defaultdict(lambda: defaultdict(lambda: [Decimal(0)] * 3))
    for r in recipes:
        unit = units.get(r.ingredient_item_id)
        if unit is None or r.sold_item_id not in product_fc:
            continue
        per_unit = convert(r.quantity, r.unit, unit)
        for fd, lo, ex, hi in product_fc[r.sold_item_id]:
            acc = ing[r.ingredient_item_id][fd]
            acc[0] += Decimal(str(lo)) * per_unit
            acc[1] += Decimal(str(ex)) * per_unit
            acc[2] += Decimal(str(hi)) * per_unit
    for iid, by_day in ing.items():
        for fd, (lo_d, ex_d, hi_d) in by_day.items():
            rows.append(dict(business_id=business_id, item_id=iid, forecast_date=fd, low=lo_d.quantize(Q),
                             expected=ex_d.quantize(Q), high=hi_d.quantize(Q), method="derived", generated_on=d,
                             confidence=0.8))
        methods[str(iid)] = "derived"
    return rows, methods


async def generate(business_id: uuid.UUID, d: date, horizon: int = HORIZON_DAYS) -> dict[str, str]:
    """Forecast and store every product and ingredient from d. Fitting happens outside the write lock."""
    async with read_session() as s:
        business = await s.get(Business, business_id)
        assert business is not None
        cal = calendar_for(business)
        items = list((await s.execute(select(Item).where(Item.business_id == business_id))).scalars())
        history = await sales_history(s, business_id, d)
        recipes = [_Recipe(r.sold_item_id, r.ingredient_item_id, r.quantity, r.unit) for r in (await s.execute(
            select(RecipeLine).where(RecipeLine.business_id == business_id))).scalars()]
    products = {i.id: (i.category, i.forecast_method) for i in items}
    units = {i.id: i.unit for i in items}
    rows, methods = await asyncio.to_thread(_compute, business_id, d, horizon, cal, history, products, units, recipes)
    async with write_session() as s:
        await s.execute(delete(DemandForecast).where(DemandForecast.business_id == business_id,
                                                     DemandForecast.generated_on == d))
        await s.execute(delete(DemandForecast).where(DemandForecast.business_id == business_id,
                                                     DemandForecast.generated_on < d - timedelta(days=KEEP_GENERATIONS_DAYS)))
        s.add_all([DemandForecast(**row) for row in rows])
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


async def forecast_errors(s: AsyncSession, business_id: uuid.UUID, item_ids: list[uuid.UUID],
                          today: date) -> dict[uuid.UUID, tuple[float | None, float | None]]:
    """(yesterday's error, 7-day MAPE) per product. Days with no sales records at all are skipped."""
    days = [today - timedelta(days=i) for i in range(1, 8)]
    sales = (await s.execute(select(Sale).where(Sale.business_id == business_id, Sale.date >= days[-1],
                                                Sale.date < today))).scalars().all()
    sold_days = {r.date for r in sales}
    actual: dict[tuple[date, str], float] = defaultdict(float)
    for sale in sales:
        for line in sale.lines:
            actual[(sale.date, line["sold_item_id"])] += float(line["qty"])
    latest: dict[tuple[uuid.UUID, date], DemandForecast] = {}
    if item_ids:
        for row in (await s.execute(select(DemandForecast).where(
                DemandForecast.item_id.in_(item_ids), DemandForecast.forecast_date >= days[-1],
                DemandForecast.forecast_date < today, DemandForecast.generated_on <= DemandForecast.forecast_date))).scalars():
            key = (row.item_id, row.forecast_date)
            if key not in latest or row.generated_on > latest[key].generated_on:
                latest[key] = row
    out: dict[uuid.UUID, tuple[float | None, float | None]] = {}
    for item_id in item_ids:
        pairs: dict[date, tuple[float, float]] = {}
        for day in days:
            fc = latest.get((item_id, day))
            if fc is not None and day in sold_days:
                pairs[day] = (float(fc.expected), actual.get((day, str(item_id)), 0))
        y = pairs.get(days[0])
        out[item_id] = (mape([y]) if y else None, mape([pairs[day] for day in days if day in pairs]))
    return out
