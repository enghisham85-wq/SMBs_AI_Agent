"""Reorder planning.

For each purchased item: project stock day by day (arrivals of open orders included), find the
stockout date, and reorder when the item would run short within the lead time plus a buffer.
The trigger window is never shorter than 4 days, so the owner is warned at least 3 days ahead.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.core.country_profiles import Calendar

BUFFER_DAYS = 1
REVIEW_DAYS = 7
MIN_WARNING_DAYS = 3
PEAK_SAFETY_MULTIPLIER = Decimal("1.5")


@dataclass
class ItemPlanInput:
    item_id: uuid.UUID
    name: str
    unit: str
    stock: Decimal
    daily_demand: dict[date, Decimal]  # expected demand per day from today
    incoming: dict[date, Decimal]  # open-order arrivals
    lead_time_days: float  # max(stated, observed)
    safety_stock: Decimal
    min_order_qty: Decimal
    pack_size: Decimal
    shelf_life_days: int | None
    storage_capacity: Decimal | None
    unit_price_minor: int
    supplier_id: uuid.UUID
    is_critical: bool
    margin_class: str = "normal"


@dataclass
class Proposal:
    item_id: uuid.UUID
    name: str
    supplier_id: uuid.UUID
    qty: Decimal
    unit: str
    pack_size: Decimal
    unit_price_minor: int
    arrival: date
    projected_stockout: date | None
    days_of_cover: float
    is_critical: bool
    margin_class: str
    reason: str
    safety_raised: bool = False

    @property
    def total_minor(self) -> int:
        return int((self.qty * self.unit_price_minor).to_integral_value())


def projection(stock: Decimal, demand: dict[date, Decimal], incoming: dict[date, Decimal], start: date,
               days: int) -> list[tuple[date, Decimal]]:
    """End-of-day stock for each day from `start`; arrivals land at the start of their day."""
    out: list[tuple[date, Decimal]] = []
    level = stock
    for i in range(days):
        d = start + timedelta(days=i)
        level = level + incoming.get(d, Decimal(0)) - demand.get(d, Decimal(0))
        out.append((d, level))
    return out


def days_of_cover(stock: Decimal, demand: dict[date, Decimal], start: date) -> float:
    week = [demand.get(start + timedelta(days=i), Decimal(0)) for i in range(7)]
    avg = sum(week) / 7 if week else Decimal(0)
    return float(stock / avg) if avg > 0 else math.inf


# Days of margin on top of the warning target, so a forecast that runs a day short still warns in time.
FORECAST_MARGIN_DAYS = 1


def trigger_window(lead_time_days: float) -> int:
    """Order when the projected stockout is closer than this many days. With a daily review the first warning
    comes MIN_WARNING_DAYS + FORECAST_MARGIN_DAYS ahead (at least 3 days' warning)."""
    return max(math.ceil(lead_time_days) + BUFFER_DAYS, MIN_WARNING_DAYS + FORECAST_MARGIN_DAYS) + 1


def _round_up(qty: Decimal, pack: Decimal) -> Decimal:
    if pack <= 0:
        return qty
    return (qty / pack).to_integral_value(rounding=ROUND_CEILING) * pack


def plan_item(p: ItemPlanInput, today: date, cal: Calendar | None = None) -> Proposal | None:
    lead = math.ceil(p.lead_time_days)
    window = trigger_window(p.lead_time_days)
    horizon = max(window, lead + REVIEW_DAYS) + REVIEW_DAYS
    # Below zero means the shelf ran empty and those sales were lost (or recorded before a count); there is
    # no backlog to refill, so plan from an empty shelf rather than ordering extra that would go to waste.
    on_hand = max(p.stock, Decimal(0))
    proj = projection(on_hand, p.daily_demand, p.incoming, today, horizon)
    stockout = next((d for d, lvl in proj if lvl < 0), None)

    safety = p.safety_stock
    arrival = today + timedelta(days=lead)
    cover_days = REVIEW_DAYS
    if p.shelf_life_days:
        cover_days = max(1, min(cover_days, p.shelf_life_days - 1))
    window_dates = [arrival + timedelta(days=i) for i in range(cover_days)]
    raised = False
    if cal is not None and any(cal.is_holiday(d) or cal.is_ramadan(d) for d in window_dates):
        safety = safety * PEAK_SAFETY_MULTIPLIER  # raise safety stock ahead of known peaks
        raised = True

    min_in_window = min((lvl for d, lvl in proj if (d - today).days < window), default=on_hand)
    short_soon = stockout is not None and (stockout - today).days < window
    below_safety = min_in_window < safety
    if not (short_soon or below_safety):
        return None

    at_arrival = dict(proj).get(arrival - timedelta(days=1), on_hand) if lead > 0 else on_hand
    at_arrival += p.incoming.get(arrival, Decimal(0))
    need = sum(p.daily_demand.get(d, Decimal(0)) for d in window_dates) + safety - at_arrival
    if need <= 0:
        return None
    qty = max(_round_up(need, p.pack_size), p.min_order_qty)
    qty = _round_up(qty, p.pack_size)
    if p.storage_capacity is not None:
        room = p.storage_capacity - max(at_arrival, Decimal(0))
        if qty > room:
            capped = (room / p.pack_size).to_integral_value(rounding=ROUND_FLOOR) * p.pack_size if p.pack_size > 0 else room
            qty = max(capped, Decimal(0))
    if qty <= 0:
        return None
    cover = days_of_cover(on_hand, p.daily_demand, today)
    reason = (f"runs out on {stockout.isoformat()}" if stockout else f"falls below safety stock ({safety} {p.unit})")
    return Proposal(item_id=p.item_id, name=p.name, supplier_id=p.supplier_id, qty=qty, unit=p.unit,
                    pack_size=p.pack_size, unit_price_minor=p.unit_price_minor, arrival=arrival,
                    projected_stockout=stockout, days_of_cover=cover, is_critical=p.is_critical,
                    margin_class=p.margin_class, reason=reason, safety_raised=raised)


@dataclass
class SupplierOrder:
    supplier_id: uuid.UUID
    lines: list[Proposal] = field(default_factory=list)
    deferred: list[Proposal] = field(default_factory=list)

    @property
    def total_minor(self) -> int:
        return sum(p.total_minor for p in self.lines)

    @property
    def is_critical(self) -> bool:
        return any(p.is_critical for p in self.lines)

    @property
    def expected_date(self) -> date:
        return max(p.arrival for p in self.lines)

    @property
    def earliest_stockout(self) -> date | None:
        dates = [p.projected_stockout for p in self.lines if p.projected_stockout]
        return min(dates) if dates else None


def group_by_supplier(proposals: list[Proposal], budget_remaining_minor: int | None = None) -> list[SupplierOrder]:
    """One order per supplier. With a budget, keep critical items first, then high-margin fast movers.

    Only top-ups (an item dipping below safety stock) are held back for the budget. An item that will run
    out is always drafted: if that goes over the budget, the Cash-Flow budget check puts the conflict to the
    owner with both positions instead of the order being dropped silently.
    """
    ranked = sorted(proposals, key=lambda p: (not p.is_critical, p.margin_class != "high",
                                              p.projected_stockout or date.max))
    orders: dict[uuid.UUID, SupplierOrder] = {}
    remaining = budget_remaining_minor
    for p in ranked:
        order = orders.setdefault(p.supplier_id, SupplierOrder(supplier_id=p.supplier_id))
        if (remaining is not None and not p.is_critical and p.projected_stockout is None
                and p.total_minor > remaining):
            order.deferred.append(p)
            continue
        order.lines.append(p)
        if remaining is not None:
            remaining -= p.total_minor
    return [o for o in orders.values() if o.lines or o.deferred]
