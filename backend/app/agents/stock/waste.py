"""Expiry risk and dead stock (FR-022)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.master import Item
from app.models.stock_ops import StockMovement


@dataclass
class ExpiryRisk:
    item_id: uuid.UUID
    name: str
    stock: Decimal
    days_to_sell: float
    shelf_life_days: int
    suggestion: str


def expiry_risk(item: Item, stock: Decimal, avg_daily_use: Decimal) -> ExpiryRisk | None:
    """Stock that will not be used before it expires (assumes the oldest stock is used first)."""
    if not item.shelf_life_days or stock <= 0:
        return None
    days_to_sell = float(stock / avg_daily_use) if avg_daily_use > 0 else float("inf")
    if days_to_sell <= item.shelf_life_days:
        return None
    suggestion = "use in specials or discount" if avg_daily_use > 0 else "stop reordering"
    return ExpiryRisk(item.id, item.name_en, stock, days_to_sell, item.shelf_life_days, suggestion)


async def dead_stock(s: AsyncSession, business_id: uuid.UUID, today: date, days: int) -> list[uuid.UUID]:
    """Items with stock that had no usage movement in the last `days` days."""
    since = today - timedelta(days=days)
    used = {r for r in (await s.execute(select(StockMovement.item_id).where(
        StockMovement.business_id == business_id, StockMovement.type == "sale", StockMovement.date >= since))).scalars()}
    stocked = (await s.execute(select(StockMovement.item_id, func.sum(StockMovement.quantity)).where(
        StockMovement.business_id == business_id).group_by(StockMovement.item_id))).all()
    items = {i.id for i in (await s.execute(select(Item).where(Item.business_id == business_id,
                                                               Item.is_ingredient.is_(True)))).scalars()}
    return [item_id for item_id, qty in stocked if item_id in items and qty > 0 and item_id not in used]
