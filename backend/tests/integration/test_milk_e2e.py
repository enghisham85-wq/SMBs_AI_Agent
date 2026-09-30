"""US5 end-to-end (T107): the spec's milk example, checking all three agents after each step.

1 forecast -> milk PO drafted     2 Cash-Flow budget check (milk is critical)     3 owner approves
4 short delivery (36 of 40 L)     5 invoice for 40 L held by the three-way match
6 owner confirms: corrected posting, payment updated, supplier score lowered
7 incident logged and a rule proposed: wait for delivery confirmation before posting this supplier's invoices
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.agents.accountant.intake import submit
from app.agents.cashflow import graphs as cash_graphs
from app.agents.stock.graphs import record_delivery
from app.approvals import service as approvals
from app.config import get_settings
from app.core import clock, scheduler
from app.db.engine import read_session
from app.harness import rules
from app.models.books import PayableInvoice
from app.models.events import Event, EventDelivery
from app.models.harness import ApprovalRequest, AuditLogEntry, Incident, LearnedRule
from app.models.master import Item, Supplier
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.models.tenancy import User
from app.seed.sample_cafe import seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(db: None, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    info["samples"] = tmp_path / "samples"
    return info


class _Owner:
    def __init__(self, uid: uuid.UUID) -> None:
        self.id, self.role, self.username = uid, "owner", "owner"


async def _owner(bid: uuid.UUID) -> _Owner:
    async with read_session() as s:
        return _Owner((await s.execute(select(User.id).where(User.business_id == bid, User.role == "owner"))).scalar_one())


async def _events(kind: str) -> list[Event]:
    async with read_session() as s:
        return list((await s.execute(select(Event).where(Event.type == kind).order_by(Event.occurred_at))).scalars())


async def _consumed(event_id: uuid.UUID) -> set[str]:
    async with read_session() as s:
        return {d.consumer for d in (await s.execute(select(EventDelivery).where(EventDelivery.event_id == event_id,
                                                                                  EventDelivery.status == "handled"))).scalars()}


async def _audit(bid: uuid.UUID, event: str) -> list[AuditLogEntry]:
    async with read_session() as s:
        return list((await s.execute(select(AuditLogEntry).where(AuditLogEntry.business_id == bid,
                                                                 AuditLogEntry.event == event))).scalars())


async def _milk_request(bid: uuid.UUID, milk: Item) -> ApprovalRequest:
    for _ in range(5):
        await scheduler.advance(bid, days=1)
        async with read_session() as s:
            reqs = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                                  ApprovalRequest.status == "pending",
                                                                  ApprovalRequest.kind == "approval"))).scalars().all()
            for r in reqs:
                if "po_id" not in r.context:
                    continue
                lines = (await s.execute(select(PurchaseOrderLine).where(
                    PurchaseOrderLine.po_id == uuid.UUID(r.context["po_id"])))).scalars().all()
                if any(ln.item_id == milk.id for ln in lines):
                    return r
    raise AssertionError("no milk order was drafted")


async def _forecast_keys(bid: uuid.UUID) -> set[str]:
    await cash_graphs.step_cash_forecast(bid, clock.today())
    run = await cash_graphs.latest_run(bid, clock.today())
    assert run is not None
    return {f["key"] for f in run.flows}


async def test_milk_end_to_end_across_three_agents(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    async with read_session() as s:
        milk = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Milk"))).scalar_one()
        sup = await s.get(Supplier, milk.preferred_supplier_id)
    assert milk.is_critical and sup is not None
    score_start = sup.reliability_score

    # 1. Stock forecasts the stockout and drafts a milk order.
    req = await _milk_request(bid, milk)
    po_id = uuid.UUID(req.context["po_id"])
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
    assert po is not None and po.notes.get("projected_stockout")
    drafted = [e for e in await _events("po.drafted") if e.payload["po_id"] == str(po_id)]
    assert drafted and "cashflow" in await _consumed(drafted[0].id)

    # 2. Cash-Flow checks the weekly budget and replies; the drafted order is a committed outflow.
    checks = [e for e in await _events("budget.check_result") if e.payload["po_id"] == str(po_id)]
    assert len(checks) == 1 and "stock" in await _consumed(checks[0].id)
    assert checks[0].payload["recommendation"] == "proceed"  # milk is critical: the stockout outranks the budget
    assert po.notes["budget"]["status"] in ("ok", "proceed")
    assert any(a.inputs["po_id"] == str(po_id) for a in await _audit(bid, "committed_outflow_added"))
    assert f"po:{po_id}" in await _forecast_keys(bid)

    # 3. The owner approves (40 L) in one tap; Accountant and Cash-Flow hear about the sent order.
    edits = {"lines": [{"item_id": str(milk.id), "qty": "40"}]}
    res = await approvals.resolve(str(req.id), "edit", await _owner(bid), "dashboard", edits=edits)
    assert res.status == "resolved"
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po_id))).scalars().all()
    assert po is not None and po.status == "sent"
    sent = [e for e in await _events("po.approved_sent") if e.payload["po_id"] == str(po_id)]
    assert sent and {"cashflow", "accountant"} <= await _consumed(sent[0].id)
    assert any(a.inputs["po_id"] == str(po_id) for a in await _audit(bid, "po_open_for_matching"))

    # 4. Delivery arrives short: 36 L instead of 40 L.
    delivered = [{"item_id": str(ln.item_id), "qty_received": "36" if ln.item_id == milk.id else str(ln.qty)} for ln in lines]
    out = await record_delivery(bid, {"po_id": str(po_id), "lines": delivered})
    assert out["outcome"] == "completed" and any(d["kind"] == "quantity" for d in out["discrepancies"])
    async with read_session() as s:
        assert (await s.get(PurchaseOrder, po_id)).status == "partially_received"  # type: ignore[union-attr]
        score_after_delivery = (await s.get(Supplier, sup.id)).reliability_score  # type: ignore[union-attr]
    assert score_after_delivery < score_start
    assert any(a.inputs["po_id"] == str(po_id) for a in await _audit(bid, "delivery_ready_for_match"))

    # 5. The supplier invoices the full 40 L: the three-way match holds it.
    files = {f["name"]: f for f in json.loads((cafe["samples"] / "files.json").read_text())}
    f = files["bi_dairy_full_qty"]
    sub = await submit(bid, (cafe["samples"] / f["file"]).read_bytes(), f["mime"], f["file"], "dashboard", None)
    assert sub["issues"] == ["three_way"]
    inv_id = uuid.UUID(sub["invoice_id"])
    async with read_session() as s:
        inv = await s.get(PayableInvoice, inv_id)
        score_after_hold = (await s.get(Supplier, sup.id)).reliability_score  # type: ignore[union-attr]
    assert inv is not None and inv.status == "held" and inv.purchase_order_id == po_id
    held = [e for e in await _events("invoice.held") if e.payload["invoice_id"] == str(inv_id)]
    assert held and {"stock", "cashflow"} <= await _consumed(held[0].id)
    assert score_after_hold < score_after_delivery  # Stock noted the supplier issue
    keys = await _forecast_keys(bid)
    assert f"po:{po_id}" in keys and f"payable:{inv_id}" not in keys  # Cash-Flow keeps the order amount

    # 6. The owner confirms: post what was delivered; Cash-Flow schedules the payment instead of the order.
    q = next(r for r in (await approvals_pending(bid)) if r.context.get("issue") == "three_way")
    assert (await approvals.resolve(str(q.id), "post_delivered", await _owner(bid), "dashboard")).status == "resolved"
    async with read_session() as s:
        inv = await s.get(PayableInvoice, inv_id)
    assert inv is not None and inv.status == "posted" and Decimal(inv.lines[0]["qty"]) == 36
    posted = [e for e in await _events("invoice.posted") if e.payload["invoice_id"] == str(inv_id)]
    assert posted and {"stock", "cashflow"} <= await _consumed(posted[0].id)
    keys = await _forecast_keys(bid)
    assert f"payable:{inv_id}" in keys and f"po:{po_id}" not in keys  # counted once

    # 7. The harness logged the incident and proposed a rule; approved, it holds the next undelivered invoice.
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.type == "three_way_mismatch"))).scalar_one()
        rule = (await s.execute(select(LearnedRule).where(LearnedRule.source_incident_id == inc.id))).scalar_one()
    assert inc.root_cause and rule.status == "proposed"
    assert rule.rule_text_en.startswith("Always wait for delivery confirmation before posting invoices from")
    await rules.approve(rule.id, bid, (await _owner(bid)).id)
    from app.harness.action_spec import ActionContext, get_spec

    checks_out = await rules.apply_preconditions(get_spec("post_invoice"), ActionContext(bid, uuid.uuid4()),
                                                 {"invoice_id": "x", "supplier_id": str(sup.id), "delivery_confirmed": False})
    assert checks_out and not checks_out[0].passed and checks_out[0].ask


async def approvals_pending(bid: uuid.UUID) -> list[ApprovalRequest]:
    async with read_session() as s:
        return list((await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                                   ApprovalRequest.status == "pending"))).scalars())
