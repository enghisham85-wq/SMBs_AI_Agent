"""The "no assistant" baseline for the sample metrics.

Every Sunday it orders each item's average weekly use over the last 4 weeks from the preferred
supplier. No forecast, no safety stock, no expiry handling, no budget. Both runs go through
`simulate_stock` with the same demand and opening stock.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.agents.stock.tracking import UnitMismatchError, convert
from app.models.master import Item, RecipeLine


@dataclass(frozen=True)
class Event:
    day: date
    item_id: uuid.UUID
    qty: Decimal  # signed: + delivery, - use


@dataclass
class StockResult:
    stockout_days: int = 0
    stockout_days_by_item: dict[uuid.UUID, int] = field(default_factory=dict)
    stockout_episodes: list[tuple[uuid.UUID, date]] = field(default_factory=list)  # (item, first day short)
    waste_minor: int = 0
    waste_qty: dict[uuid.UUID, Decimal] = field(default_factory=dict)


def usage_by_day(sales: list[Any], items: dict[uuid.UUID, Item], recipes: list[RecipeLine]) -> dict[date, dict[uuid.UUID, Decimal]]:
    """Same deduction the Stock Agent does for sales."""
    by_sold: dict[uuid.UUID, list[RecipeLine]] = defaultdict(list)
    for r in recipes:
        by_sold[r.sold_item_id].append(r)
    out: dict[date, dict[uuid.UUID, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for sale in sales:
        for line in sale.lines:
            sold = uuid.UUID(line["sold_item_id"])
            qty = Decimal(str(line["qty"]))
            lines = by_sold.get(sold)
            if not lines:
                if sold in items and items[sold].is_ingredient:
                    out[sale.date][sold] += qty
                continue
            for r in lines:
                ing = items.get(r.ingredient_item_id)
                if ing is None:
                    continue
                try:
                    out[sale.date][ing.id] += convert(qty * r.quantity, r.unit, ing.unit)
                except UnitMismatchError:
                    continue
    return out


def baseline_orders(usage: dict[date, dict[uuid.UUID, Decimal]], items: list[Item], lead_days: dict[uuid.UUID, int],
                    start: date, days: int) -> list[Event]:
    events: list[Event] = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.isoweekday() != 7:
            continue
        for item in items:
            if item.preferred_supplier_id is None:
                continue
            used = sum((usage.get(d - timedelta(days=k), {}).get(item.id, Decimal(0)) for k in range(28)), Decimal(0))
            qty = (used / 4).quantize(Decimal("0.001"))
            if qty > 0:
                events.append(Event(d + timedelta(days=lead_days.get(item.preferred_supplier_id, 1)), item.id, qty))
    return events


class Shelf:
    """FIFO stock by delivery date, one day at a time.

    Expiry first, then deliveries, then use. Use the shelf can't cover is lost, there's no backlog.
    """

    def __init__(self, opening: dict[uuid.UUID, Decimal], items: dict[uuid.UUID, Item], start: date) -> None:
        self.items = items
        self.lots: dict[uuid.UUID, list[list[Any]]] = {iid: [[start - timedelta(days=1), q]] for iid, q in opening.items() if q > 0}
        self.short: set[uuid.UUID] = set()
        self.result = StockResult()

    def step(self, d: date, deliveries: list[Event], usage: dict[uuid.UUID, Decimal]) -> dict[uuid.UUID, Decimal]:
        """Returns what expired that morning, per item."""
        res, expired = self.result, {}
        for iid, item_lots in self.lots.items():
            life = self.items[iid].shelf_life_days if iid in self.items else None
            if not life:
                continue
            while item_lots and (d - item_lots[0][0]).days > life:
                _, q = item_lots.pop(0)
                expired[iid] = expired.get(iid, Decimal(0)) + q
                res.waste_qty[iid] = res.waste_qty.get(iid, Decimal(0)) + q
                res.waste_minor += int((Decimal(self.items[iid].unit_cost.amount_minor) * q).to_integral_value())
        for e in deliveries:
            if e.qty > 0:
                self.lots.setdefault(e.item_id, []).append([d, e.qty])
        for iid, q in usage.items():
            if iid not in self.items or not self.items[iid].is_ingredient:
                continue
            item_lots = self.lots.setdefault(iid, [])
            need = q
            while need > 0 and item_lots:
                take = min(need, item_lots[0][1])
                item_lots[0][1] -= take
                need -= take
                if item_lots[0][1] <= 0:
                    item_lots.pop(0)
            if need > 0:  # lost sales
                res.stockout_days += 1
                res.stockout_days_by_item[iid] = res.stockout_days_by_item.get(iid, 0) + 1
                if iid not in self.short:
                    res.stockout_episodes.append((iid, d))
                    self.short.add(iid)
            elif iid in self.short:
                self.short.discard(iid)  # full day's use met, episode over
        return expired


def simulate_stock(opening: dict[uuid.UUID, Decimal], usage: dict[date, dict[uuid.UUID, Decimal]], deliveries: list[Event],
                   items: dict[uuid.UUID, Item], start: date, days: int) -> StockResult:
    shelf = Shelf(opening, items, start)
    arriving: dict[date, list[Event]] = defaultdict(list)
    for e in deliveries:
        arriving[e.day].append(e)
    for i in range(days):
        d = start + timedelta(days=i)
        shelf.step(d, arriving.get(d, []), usage.get(d, {}))
    return shelf.result
