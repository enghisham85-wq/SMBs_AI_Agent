"""Agent coordination: budget checks, conflicts, critical stockouts, shortfall deferrals."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.agents.cashflow import budget as budget_mod
from app.agents.stock import graphs as stock_graphs
from app.approvals import service as approvals
from app.config import get_settings
from app.core import clock
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.graph import run_action
from app.models.cash import PurchasingBudget
from app.models.events import Event
from app.models.harness import ApprovalRequest
from app.models.master import Item, SupplierPrice
from app.models.purchasing import PurchaseOrder
from app.models.tenancy import User
from app.seed.sample_cafe import seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(db: None, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    wiring.register_all()
    return await seed(start_date=START, history_days=42)


class _Owner:
    def __init__(self, uid: uuid.UUID) -> None:
        self.id, self.role, self.username = uid, "owner", "owner"


async def _owner(bid: uuid.UUID) -> _Owner:
    async with read_session() as s:
        return _Owner((await s.execute(select(User.id).where(User.business_id == bid, User.role == "owner"))).scalar_one())


async def _order(bid: uuid.UUID, name: str, qty: str, stockout: date | None = None) -> uuid.UUID:
    async with read_session() as s:
        item = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == name))).scalar_one()
        price = (await s.execute(select(SupplierPrice).where(SupplierPrice.item_id == item.id))).scalars().first()
    assert price is not None
    out = await run_action("draft_po", {
        "supplier_id": str(item.preferred_supplier_id), "expected_date": (clock.today() + timedelta(days=1)).isoformat(),
        "lines": [{"item_id": str(item.id), "qty": qty, "unit": item.unit, "pack_size": str(price.pack_size),
                   "unit_price_minor": price.price.amount_minor}],
        "reason": "test", "is_critical": item.is_critical,
        "projected_stockout": stockout.isoformat() if stockout else None}, bid)
    assert out["outcome"] == "completed", out
    return uuid.UUID(out["result"]["po_id"])


async def _budget(bid: uuid.UUID, amount: str) -> None:
    async with write_session() as s:
        s.add(PurchasingBudget(business_id=bid, week_start=budget_mod.week_start(clock.today()),
                               amount=Money.from_decimal(amount, "EGP"), reason="test", tightened=True))


async def _po(po_id: uuid.UUID) -> PurchaseOrder:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
    assert po is not None
    return po


async def _conflict_questions(bid: uuid.UUID) -> list[ApprovalRequest]:
    async with read_session() as s:
        return [r for r in (await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.business_id == bid, ApprovalRequest.status == "pending",
            ApprovalRequest.kind != "alert"))).scalars()
            if r.context.get("kind") == "conflict"]


async def test_drafted_order_is_checked_against_the_budget(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    await _budget(bid, "100000")
    po_id = await _order(bid, "Oranges", "20")
    async with read_session() as s:
        res = [e for e in (await s.execute(select(Event).where(Event.type == "budget.check_result"))).scalars()
               if e.payload["po_id"] == str(po_id)]
    assert len(res) == 1 and res[0].payload["within_budget"] and res[0].payload["recommendation"] == "proceed"
    assert (await _po(po_id)).notes["budget"]["status"] == "ok"
    assert not await stock_graphs.budget_decision_pending(po_id)


async def test_over_budget_shows_both_positions_and_owner_decides(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    await _budget(bid, "100")
    po_id = await _order(bid, "Oranges", "20")
    assert await stock_graphs.budget_decision_pending(po_id)  # held while the owner decides
    [q] = await _conflict_questions(bid)
    assert "Stock Agent:" in q.text_en and "Cash-Flow Agent:" in q.text_en and "Recommended:" in q.text_en
    assert "EGP 100.00" in q.text_en
    assert {o["key"] for o in q.options} >= {"defer", "proceed"} and q.safe_default == "defer"
    assert (await approvals.resolve(str(q.id), "proceed", await _owner(bid), "dashboard")).status == "resolved"
    po = await _po(po_id)
    assert po.status == "pending_approval" and po.notes["budget"]["status"] == "proceed"
    async with read_session() as s:
        done = [e for e in (await s.execute(select(Event).where(Event.type == "budget.conflict_resolved"))).scalars()]
    assert done and done[0].payload["decision"] == "proceed"


async def test_deferred_order_waits_then_resumes(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    await _budget(bid, "100")
    po_id = await _order(bid, "Oranges", "20")
    [q] = await _conflict_questions(bid)
    await approvals.resolve(str(q.id), "defer", await _owner(bid), "dashboard")
    po = await _po(po_id)
    assert po.status == "on_hold" and po.notes["deferred_until"] == (clock.today() + timedelta(days=5)).isoformat()
    later = clock.today() + timedelta(days=5)
    clock.set_state("simulated", later)
    out = await stock_graphs.ro_deferred({"business_id": str(bid), "date": later.isoformat(), "notes": {}})
    assert out["notes"]["resumed"] == [po.number]
    assert (await _po(po_id)).status == "pending_approval"


async def test_critical_stockout_outranks_budget_and_others_can_wait(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    oranges = await _order(bid, "Oranges", "20")  # drafted before the budget, stays a draft
    await _budget(bid, "100")
    milk = await _order(bid, "Milk", "40", stockout=clock.today() + timedelta(days=2))
    assert (await _po(milk)).notes["budget"]["status"] == "proceed"
    assert not await stock_graphs.budget_decision_pending(milk)
    async with read_session() as s:
        alerts = [r for r in (await s.execute(select(ApprovalRequest).where(ApprovalRequest.kind == "alert"))).scalars()
                  if r.context.get("kind") == "conflict"]
    assert alerts and "outranks the budget" in alerts[0].text_en
    [q] = await _conflict_questions(bid)
    assert (await _po(oranges)).number in q.text_en
    await approvals.resolve(str(q.id), "defer_others", await _owner(bid), "dashboard")
    assert (await _po(oranges)).status == "on_hold"
    assert (await _po(milk)).status == "draft"


async def test_shortfall_defers_non_critical_orders_that_can_wait(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    oranges = await _order(bid, "Oranges", "20")
    urgent = await _order(bid, "Croissant (baked)", "20", stockout=clock.today() + timedelta(days=1))
    milk = await _order(bid, "Milk", "40")
    async with write_session() as s:
        publish(s, "shortfall.predicted", {"run_id": str(uuid.uuid4()), "gap_amount": Money(1500000, "EGP"),
                                           "gap_date": (clock.today() + timedelta(days=20)).isoformat(), "days_to_act": 20,
                                           "plan_id": str(uuid.uuid4())}, producer="cashflow", business_id=bid)
    assert (await _po(oranges)).status == "on_hold"  # non-critical and can wait
    assert (await _po(urgent)).status == "draft"  # would run out before a deferred delivery
    assert (await _po(milk)).status == "draft"
