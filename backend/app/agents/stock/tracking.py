"""Stock tracking (FR-013): sales deduct ingredients via recipes, deliveries add stock,
waste/spoilage/adjustments are recorded with reasons. Every change is a StockMovement and the
StockLevel is kept equal to the running sum.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import clock_now
from app.models.finance_master import Sale
from app.models.master import Item, RecipeLine
from app.models.stock_ops import REASON_REQUIRED, StockLevel, StockMovement

# Recipe unit -> stock unit conversion factors.
UNIT_FACTORS: dict[tuple[str, str], Decimal] = {
    ("g", "kg"): Decimal("0.001"),
    ("kg", "g"): Decimal("1000"),
    ("ml", "L"): Decimal("0.001"),
    ("L", "ml"): Decimal("1000"),
}


class UnitMismatchError(ValueError):
    pass


class ReasonRequiredError(ValueError):
    pass


def convert(qty: Decimal, from_unit: str, to_unit: str) -> Decimal:
    if from_unit == to_unit:
        return qty
    factor = UNIT_FACTORS.get((from_unit, to_unit))
    if factor is None:
        raise UnitMismatchError(f"cannot convert {from_unit} to {to_unit}")
    return qty * factor


async def level_row(s: AsyncSession, business_id: uuid.UUID, item_id: uuid.UUID, location: str = "main") -> StockLevel:
    row = (await s.execute(select(StockLevel).where(StockLevel.item_id == item_id, StockLevel.location == location))).scalar_one_or_none()
    if row is None:
        row = StockLevel(business_id=business_id, item_id=item_id, location=location, quantity=Decimal(0))
        s.add(row)
        await s.flush()
    return row


async def move(
    s: AsyncSession,
    business_id: uuid.UUID,
    item_id: uuid.UUID,
    qty: Decimal,
    type: str,
    d: date,
    *,
    reason: str | None = None,
    source: str = "agent",
    reference_type: str | None = None,
    reference_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
) -> StockMovement:
    if type in REASON_REQUIRED and not (reason and reason.strip()):
        raise ReasonRequiredError(f"a reason is required for {type}")
    mv = StockMovement(business_id=business_id, item_id=item_id, type=type, quantity=qty, date=d, reason=reason,
                       source=source, reference_type=reference_type, reference_id=reference_id, action_id=action_id)
    s.add(mv)
    lvl = await level_row(s, business_id, item_id)
    lvl.quantity = (lvl.quantity or Decimal(0)) + qty
    lvl.last_updated_at = clock_now()
    return mv


@dataclass
class SalesApplication:
    deducted: dict[str, Decimal] = field(default_factory=dict)
    missing_mapping: list[str] = field(default_factory=list)
    unit_errors: list[str] = field(default_factory=list)
    sale_ids: list[str] = field(default_factory=list)


async def sold_quantities(s: AsyncSession, business_id: uuid.UUID, d: date, only_unapplied: bool) -> tuple[dict[str, Decimal], list[Sale]]:
    q = select(Sale).where(Sale.business_id == business_id, Sale.date == d)
    if only_unapplied:
        q = q.where(Sale.stock_applied.is_(False))
    sales = list((await s.execute(q)).scalars())
    sold: dict[str, Decimal] = defaultdict(Decimal)
    for sale in sales:
        for line in sale.lines:
            sold[line["sold_item_id"]] += Decimal(str(line["qty"]))
    return sold, sales


async def apply_sales(s: AsyncSession, business_id: uuid.UUID, d: date, action_id: uuid.UUID | None = None) -> SalesApplication:
    """Deduct ingredients for every not-yet-applied sale on date d (inside the caller's transaction)."""
    out = SalesApplication()
    sold, sales = await sold_quantities(s, business_id, d, only_unapplied=True)
    if not sales:
        return out
    items = {str(i.id): i for i in (await s.execute(select(Item).where(Item.business_id == business_id))).scalars()}
    recipes: dict[str, list[RecipeLine]] = defaultdict(list)
    for r in (await s.execute(select(RecipeLine).where(RecipeLine.business_id == business_id))).scalars():
        recipes[str(r.sold_item_id)].append(r)
    usage: dict[str, Decimal] = defaultdict(Decimal)
    for sold_id, qty in sold.items():
        lines = recipes.get(sold_id)
        item = items.get(sold_id)
        if not lines:
            if item is not None and item.is_ingredient:
                usage[sold_id] += qty  # sold as-is
            else:
                out.missing_mapping.append(item.name_en if item else sold_id)
            continue
        for r in lines:
            ing = items.get(str(r.ingredient_item_id))
            if ing is None:
                continue
            try:
                usage[str(ing.id)] += convert(qty * r.quantity, r.unit, ing.unit)
            except UnitMismatchError:
                out.unit_errors.append(f"{ing.name_en}: recipe unit {r.unit} vs stock unit {ing.unit}")
    for item_id, qty in usage.items():
        await move(s, business_id, uuid.UUID(item_id), -qty, "sale", d, reference_type="sales_day", action_id=action_id)
        out.deducted[item_id] = qty
    for sale in sales:
        sale.stock_applied = True
        out.sale_ids.append(str(sale.id))
    return out


async def undo_action_movements(s: AsyncSession, action_id: uuid.UUID) -> int:
    """Compensation: reverse every movement an action created and restore stock levels."""
    moves = list((await s.execute(select(StockMovement).where(StockMovement.action_id == action_id))).scalars())
    for mv in moves:
        lvl = await level_row(s, mv.business_id, mv.item_id, mv.location)
        lvl.quantity = lvl.quantity - mv.quantity
        await s.delete(mv)
    return len(moves)


async def current_levels(s: AsyncSession, business_id: uuid.UUID) -> dict[uuid.UUID, Decimal]:
    rows = (await s.execute(select(StockLevel).where(StockLevel.business_id == business_id))).scalars()
    levels: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for r in rows:
        levels[r.item_id] += r.quantity
    return levels


def movement_summary(moves: list[StockMovement]) -> dict[str, Any]:
    return {str(m.item_id): str(m.quantity) for m in moves}
