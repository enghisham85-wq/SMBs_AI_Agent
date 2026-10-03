"""Reorder vs the weekly budget: top-ups wait, items about to run out are still drafted."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from app.agents.stock import reorder

TODAY = date(2026, 10, 22)
SUPPLIER = uuid.uuid4()


def _proposal(name: str, total_minor: int, *, runs_out: bool, critical: bool = False) -> reorder.Proposal:
    return reorder.Proposal(item_id=uuid.uuid4(), name=name, supplier_id=SUPPLIER, qty=Decimal(1), unit="kg",
                            pack_size=Decimal(1), unit_price_minor=total_minor, arrival=TODAY + timedelta(days=2),
                            projected_stockout=TODAY + timedelta(days=4) if runs_out else None, days_of_cover=4.0,
                            is_critical=critical, margin_class="normal", reason="test")


def test_top_ups_wait_for_the_budget_but_items_running_out_are_ordered() -> None:
    oranges = _proposal("Oranges", 5000, runs_out=True)
    tea = _proposal("Tea bags", 5000, runs_out=False)
    [order] = reorder.group_by_supplier([oranges, tea], budget_remaining_minor=1000)
    assert [p.name for p in order.lines] == ["Oranges"]  # over budget: the budget check will ask the owner
    assert [p.name for p in order.deferred] == ["Tea bags"]


def test_critical_items_first_within_the_budget() -> None:
    milk = _proposal("Milk", 4000, runs_out=False, critical=True)
    tea = _proposal("Tea bags", 3000, runs_out=False)
    cups = _proposal("Paper cup", 500, runs_out=False)
    [order] = reorder.group_by_supplier([tea, cups, milk], budget_remaining_minor=5000)
    assert [p.name for p in order.lines] == ["Milk", "Paper cup"] and [p.name for p in order.deferred] == ["Tea bags"]


def test_no_budget_orders_everything() -> None:
    [order] = reorder.group_by_supplier([_proposal("Oranges", 5000, runs_out=True), _proposal("Tea", 5000, runs_out=False)])
    assert len(order.lines) == 2 and order.deferred == []


def test_first_warning_leaves_a_day_for_forecast_error() -> None:
    # 2-day supplier, so the first warning lands 4 days out (target is 3+).
    assert reorder.trigger_window(2) - 1 == reorder.MIN_WARNING_DAYS + reorder.FORECAST_MARGIN_DAYS
