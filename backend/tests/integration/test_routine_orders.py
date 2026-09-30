"""Routine-order auto-approval offered to the owner (owner effort, SC-006): after 5 orders to a supplier
approved unchanged, the Stock Agent proposes a rule; once the owner approves it, orders within the limit go
out without a question, larger ones still ask, and a declined offer is not repeated."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.approvals import service as approvals
from app.config import get_settings
from app.core import clock
from app.db.engine import read_session
from app.harness import rules
from app.harness.graph import run_action
from app.models.harness import ApprovalRequest, LearnedRule
from app.models.master import Item, SupplierPrice
from app.models.tenancy import User
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(api: Any, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    await api.login("owner", PASSWORDS["owner"])
    return info


class _Owner:
    def __init__(self, u: User) -> None:
        self.id, self.role, self.username = u.id, u.role, u.username


async def _owner(bid: uuid.UUID) -> _Owner:
    async with read_session() as s:
        return _Owner((await s.execute(select(User).where(User.business_id == bid, User.role == "owner"))).scalar_one())


async def _send_milk(bid: uuid.UUID, qty: str) -> dict[str, Any]:
    """Draft and send a milk order; returns the send_po run (interrupted when it waits for the owner)."""
    async with read_session() as s:
        milk = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Milk"))).scalar_one()
        price = (await s.execute(select(SupplierPrice).where(SupplierPrice.item_id == milk.id))).scalars().first()
    assert price is not None
    drafted = await run_action("draft_po", {
        "supplier_id": str(milk.preferred_supplier_id), "expected_date": (clock.today() + timedelta(days=1)).isoformat(),
        "lines": [{"item_id": str(milk.id), "qty": qty, "unit": "L", "pack_size": "1", "unit_price_minor": price.price.amount_minor}],
        "reason": "routine", "is_critical": True, "projected_stockout": (clock.today() + timedelta(days=4)).isoformat()}, bid)
    assert drafted["outcome"] == "completed", drafted
    return await run_action("send_po", {"po_id": drafted["result"]["po_id"]}, bid, parent_action_id=drafted["action_id"])


async def _answer(bid: uuid.UUID, run: dict[str, Any], key: str, edits: dict[str, Any] | None = None) -> None:
    async with read_session() as s:
        req = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == run["action_id"],
                                                             ApprovalRequest.status == "pending"))).scalar_one()
    res = await approvals.resolve(str(req.id), key, await _owner(bid), "dashboard", edits)  # type: ignore[arg-type]
    assert res.status == "resolved"


async def _offers(bid: uuid.UUID) -> list[LearnedRule]:
    async with read_session() as s:
        rows = (await s.execute(select(LearnedRule).where(LearnedRule.business_id == bid, LearnedRule.kind == "policy"))).scalars()
        return [r for r in rows if r.trigger.get("auto_approve_up_to_minor") is not None]


async def test_five_unchanged_approvals_offer_auto_approval_which_then_applies(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    for i in range(5):
        run = await _send_milk(bid, str(30 + i))
        assert run["interrupted"]
        await _answer(bid, run, "approve")
        assert (await _offers(bid)) == [] or i == 4
    [offer] = await _offers(bid)
    assert offer.status == "proposed" and "Al Noor Dairy" in offer.rule_text_en and "without asking me" in offer.rule_text_en
    assert offer.trigger["auto_approve_up_to_minor"] >= 34 * 4500  # above the largest approved order

    r = await api.client.post(f"/api/v1/harness/rules/{offer.id}/approve")
    assert r.status_code == 200 and r.json()["status"] == "active"
    routine = await _send_milk(bid, "32")
    assert routine["outcome"] == "completed" and not routine["interrupted"]  # no question for a routine order
    async with read_session() as s:
        assert (await s.get(LearnedRule, offer.id)).times_applied == 1  # type: ignore[union-attr]
    # The owner lowers the limit below this order's size: the same routine order now waits for approval.
    r = await api.client.patch(f"/api/v1/harness/rules/{offer.id}",
                               json={"trigger": {**offer.trigger, "auto_approve_up_to_minor": 100_00}})
    assert r.status_code == 200, r.text
    over = await _send_milk(bid, "32")
    assert over["interrupted"]


async def test_an_edited_order_in_the_streak_means_no_offer(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    for i in range(5):
        run = await _send_milk(bid, str(30 + i))
        if i == 2:
            async with read_session() as s:
                req = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == run["action_id"]))).scalar_one()
            line = req.context["edit"]["lines"][0]
            await _answer(bid, run, "edit", {"lines": [{"item_id": line["item_id"], "qty": "25"}]})
        else:
            await _answer(bid, run, "approve")
    assert await _offers(bid) == []


async def test_a_declined_offer_is_not_repeated(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    for i in range(5):
        await _answer(bid, await _send_milk(bid, str(30 + i)), "approve")
    [offer] = await _offers(bid)
    await rules.reject(offer.id, bid, (await _owner(bid)).id)
    for i in range(5):
        await _answer(bid, await _send_milk(bid, str(20 + i)), "approve")
    assert [r.id for r in await _offers(bid)] == [offer.id]
