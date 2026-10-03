"""Stock assistant: stockout warning >= 3 days ahead with a one-tap PO, recipe deduction,
price-spike hold, duplicate-PO merge, delivery discrepancy, forecast-error fallback, late delivery."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.approvals import service as approvals
from app.core import clock, scheduler
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.graph import run_action
from app.models.events import FeedOverride
from app.models.harness import ApprovalRequest, Incident
from app.models.master import Item, Supplier, SupplierPrice
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.models.stock_ops import StockLevel
from app.seed.sample_cafe import seed

START = date(2026, 10, 4)  # Sunday


@pytest.fixture
async def cafe(db: None) -> dict[str, Any]:
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    return info


async def _item(bid: uuid.UUID, name: str) -> Item:
    async with read_session() as s:
        return (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == name))).scalar_one()


async def _pending(bid: uuid.UUID, kind: str | None = None) -> list[ApprovalRequest]:
    async with read_session() as s:
        q = select(ApprovalRequest).where(ApprovalRequest.business_id == bid, ApprovalRequest.status == "pending")
        if kind:
            q = q.where(ApprovalRequest.kind == kind)
        return list((await s.execute(q.order_by(ApprovalRequest.created_at))).scalars())


async def _level(item_id: uuid.UUID) -> Decimal:
    async with read_session() as s:
        return (await s.execute(select(StockLevel.quantity).where(StockLevel.item_id == item_id))).scalar_one()


class Owner:
    def __init__(self, uid: uuid.UUID) -> None:
        self.id, self.role, self.username = uid, "owner", "owner"


async def _owner(bid: uuid.UUID) -> Owner:
    from app.models.tenancy import User

    async with read_session() as s:
        u = (await s.execute(select(User).where(User.business_id == bid, User.role == "owner"))).scalar_one()
    return Owner(u.id)


async def _advance_until_milk_request(bid: uuid.UUID, max_days: int = 5) -> ApprovalRequest:
    milk = await _item(bid, "Milk")
    for _ in range(max_days):
        await scheduler.advance(bid, days=1)
        for req in await _pending(bid, "approval"):
            if "po_id" not in req.context:
                continue  # e.g. a payment reminder waiting for approval
            async with read_session() as s:
                po = await s.get(PurchaseOrder, uuid.UUID(req.context["po_id"]))
                lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
            if any(ln.item_id == milk.id for ln in lines):
                return req
    raise AssertionError("no milk purchase order request was raised")


async def test_milk_warning_at_least_3_days_ahead_and_one_tap_approval(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    req = await _advance_until_milk_request(bid)
    today = clock.today()
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(req.context["po_id"]))
        sup = await s.get(Supplier, po.supplier_id)
        line = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().first()
        price = (await s.execute(select(SupplierPrice).where(SupplierPrice.item_id == line.item_id))).scalars().first()
    stockout = date.fromisoformat(po.notes["projected_stockout"])
    assert (stockout - today).days >= 3, f"warned only {(stockout - today).days} days ahead"
    assert sup.name_en == "Al Noor Dairy"
    assert line.qty >= price.min_order_qty and line.qty % price.pack_size == 0
    assert po.expected_date == today + timedelta(days=1)
    assert "Milk will run out" in req.text_en and "EGP" in req.text_en
    assert [o["key"] for o in req.options] == ["approve", "edit", "reject"]

    res = await approvals.resolve(req.request_token, "approve", await _owner(bid), "telegram")
    assert res.status == "resolved"
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po.id)
    assert po.status == "sent" and po.sent_at is not None


async def test_recipe_deduction_for_a_latte(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    from app.models.finance_master import Sale

    latte = await _item(bid, "Latte")
    coffee, milk = await _item(bid, "Coffee beans"), await _item(bid, "Milk")
    before_c, before_m = await _level(coffee.id), await _level(milk.id)
    day = START + timedelta(days=30)  # a day with no generated sales
    async with write_session() as s:
        s.add(Sale(business_id=bid, date=day, lines=[{"sold_item_id": str(latte.id), "qty": 1, "amount_minor": 7500}],
                   amount_total=Money(7500, "EGP"), payment_method="cash", source="manual"))
    out = await run_action("apply_sales", {"date": day.isoformat()}, bid)
    assert out["outcome"] == "completed"
    assert before_c - await _level(coffee.id) == Decimal("0.018")
    assert before_m - await _level(milk.id) == Decimal("0.2")


async def _milk_order_inputs(bid: uuid.UUID, price_minor: int | None = None) -> dict[str, Any]:
    milk = await _item(bid, "Milk")
    async with read_session() as s:
        price = (await s.execute(select(SupplierPrice).where(SupplierPrice.item_id == milk.id))).scalars().first()
    return {"supplier_id": str(milk.preferred_supplier_id), "expected_date": (clock.today() + timedelta(days=1)).isoformat(),
            "lines": [{"item_id": str(milk.id), "qty": "40", "unit": "L", "pack_size": "1",
                       "unit_price_minor": price_minor or price.price.amount_minor}],
            "reason": "test", "is_critical": True, "projected_stockout": (clock.today() + timedelta(days=3)).isoformat()}


async def test_price_spike_over_15_percent_is_held_and_owner_asked(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    inputs = await _milk_order_inputs(bid)
    inputs["lines"][0]["unit_price_minor"] = int(inputs["lines"][0]["unit_price_minor"] * 1.25)
    out = await run_action("draft_po", inputs, bid)
    assert out["interrupted"]
    q = [r for r in await _pending(bid, "question")][-1]
    assert "price" in q.text_en and "+25%" in q.text_en
    assert {o["key"] for o in q.options} >= {"continue", "alternative", "cancel"}
    async with read_session() as s:
        po = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.created_by_action_id == out["action_id"]))).scalar_one()
    assert po.status == "on_hold"


async def test_duplicate_open_po_is_blocked_with_merge_offered(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    first = await run_action("draft_po", await _milk_order_inputs(bid), bid)
    assert first["outcome"] == "completed"
    second = await run_action("draft_po", await _milk_order_inputs(bid), bid)
    assert second["interrupted"]
    q = (await _pending(bid, "question"))[-1]
    assert "merge" in {o["key"] for o in q.options}
    await approvals.resolve(str(q.id), "merge", await _owner(bid), "dashboard")
    async with read_session() as s:
        pos = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == bid,
                                                           PurchaseOrder.status.in_(("draft", "pending_approval"))))).scalars().all()
    assert len(pos) == 1  # merged into the first order, no duplicate left


async def test_short_delivery_is_flagged_and_supplier_score_drops(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    from app.agents.stock.graphs import record_delivery

    drafted = await run_action("draft_po", await _milk_order_inputs(bid), bid)
    po_id = drafted["result"]["po_id"]
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(po_id))
        po.status, po.sent_at = "sent", clock.clock_now()
        sup = await s.get(Supplier, po.supplier_id)
        before = sup.reliability_score
    milk = await _item(bid, "Milk")
    level_before = await _level(milk.id)
    res = await record_delivery(bid, {"po_id": po_id, "lines": [{"item_id": str(milk.id), "qty_received": "36"}]})
    assert res["outcome"] == "completed"
    assert res["discrepancies"][0]["kind"] == "quantity"
    assert await _level(milk.id) - level_before == Decimal("36")
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(po_id))
        sup = await s.get(Supplier, po.supplier_id)
        alerts = [r for r in await _pending(bid, "alert") if "36" in r.text_en]
    assert po.status == "partially_received"
    assert sup.reliability_score < before
    assert alerts


async def test_forecast_error_switches_to_same_weekday_average(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    latte = await _item(bid, "Latte")
    await scheduler.advance(bid, days=1)  # produces a forecast for the next days
    spike_day = clock.today() + timedelta(days=1)
    async with write_session() as s:
        s.add(FeedOverride(business_id=bid, date=spike_day, overrides={"sales_multiplier": {str(latte.id): 3}}))
    await scheduler.advance(bid, days=2)  # spike day, then the morning check
    latte = await _item(bid, "Latte")
    assert latte.forecast_method == "same_weekday_avg"
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.business_id == bid, Incident.type == "forecast_accuracy"))).scalars().all()
    assert inc
    assert any("safer method" in r.text_en for r in await _pending(bid, "alert"))


async def test_late_po_flagged_once_with_one_reliability_drop(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    from app.agents.stock.graphs import step_reorder

    drafted = await run_action("draft_po", await _milk_order_inputs(bid), bid)
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(drafted["result"]["po_id"]))
        po.status, po.sent_at = "sent", clock.clock_now()
        po.expected_date = clock.today() - timedelta(days=2)
        sup = await s.get(Supplier, po.supplier_id)
        before = sup.reliability_score
    await step_reorder(bid, clock.today())
    await step_reorder(bid, clock.today())  # second run must not penalise again
    async with read_session() as s:
        sup = await s.get(Supplier, sup.id)
        po = await s.get(PurchaseOrder, po.id)
    assert po.late_flagged
    assert round(before - sup.reliability_score, 3) == 0.05
    questions = [q for q in await _pending(bid, "question") if "late" in q.text_en]
    assert len(questions) == 1
    assert {o["key"] for o in questions[0].options} == {"wait", "reorder_other", "call"}


async def test_no_expiry_warnings_before_the_first_forecast(cafe: dict[str, Any]) -> None:
    """A freshly seeded business has no forecast yet: expected use is unknown, not zero."""
    import json as _json

    from app.api.v1.stock import list_items
    from app.core.auth import CurrentUser

    bid = cafe["business_id"]
    owner = await _owner(bid)
    body = _json.loads(bytes((await list_items(CurrentUser(owner.id, bid, "owner", "owner", "en", "x"))).body))
    assert body["forecast_generated_on"] is None
    assert all(i["expiry_risk"] is None for i in body["items"])
