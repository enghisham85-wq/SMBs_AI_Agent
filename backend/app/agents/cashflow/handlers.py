"""Cash-Flow Agent event handlers. Each runs as the Cash-Flow Agent.

The forecast reads purchase orders and invoices directly, so a drafted, sent or invoiced order is
already a committed outflow once saved; these handlers answer the budget check and refresh today's
forecast when money moves.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.agents.cashflow import budget as budget_mod
from app.agents.cashflow import graphs
from app.core import clock
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.audit import add_audit
from app.models.cash import CashForecastRun, PurchasingBudget

REDUCE_MIN_SHARE = 0.5  # reduce (rather than defer) when at least half the order fits the budget


def _bid(env: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(env["business_id"])


def budget_decision(total_minor: int, remaining_before_minor: int | None, is_critical: bool) -> dict[str, Any]:
    """Pure budget check for one drafted order."""
    if remaining_before_minor is None:
        return {"within_budget": True, "recommendation": "proceed", "conflict": False}
    if total_minor <= remaining_before_minor:
        return {"within_budget": True, "recommendation": "proceed", "conflict": False}
    if is_critical:  # a critical stockout outranks the budget; the owner is told
        return {"within_budget": False, "recommendation": "proceed", "conflict": True}
    if remaining_before_minor >= total_minor * REDUCE_MIN_SHARE:
        return {"within_budget": False, "recommendation": "reduce", "conflict": True}
    return {"within_budget": False, "recommendation": "defer", "conflict": True}


async def on_po_drafted(env: dict[str, Any]) -> None:
    """Check a drafted order against this week's purchasing budget and reply with budget.check_result."""
    bid = _bid(env)
    p = env["payload"]
    po_id = uuid.UUID(p["po_id"])
    total = int(p["total"]["amount_minor"])
    cur = p["total"]["currency"]
    d = clock.today()
    async with read_session() as s:
        left = await budget_mod.remaining(s, bid, d, exclude_po=po_id)
        row = (await s.execute(select(PurchasingBudget).where(PurchasingBudget.business_id == bid,
                                                              PurchasingBudget.week_start == budget_mod.week_start(d)))).scalar_one_or_none()
    decision = budget_decision(total, left, bool(p.get("is_critical")))
    remaining_after = None if left is None else left - total
    async with write_session() as s:
        publish(s, "budget.check_result", {
            "po_id": po_id, "within_budget": decision["within_budget"],
            "remaining_budget": Money(left, cur) if left is not None else None,
            "remaining_after": Money(remaining_after, cur) if remaining_after is not None else None,
            "recommendation": decision["recommendation"], "conflict": decision["conflict"],
            "is_critical": bool(p.get("is_critical")), "total": Money(total, cur),
            "weekly_budget": row.amount if row else None, "tightened": bool(row.tightened) if row else False,
            "budget_reason": row.reason if row else None},
            producer="cashflow", business_id=bid, action_id=uuid.UUID(env["action_id"]) if env.get("action_id") else None)
        add_audit(s, "committed_outflow_added", business_id=bid, agent="cashflow",
                  inputs={"po_id": po_id, "total_minor": total, "expected_date": p.get("expected_date")})


async def _refresh_if_forecast_today(bid: uuid.UUID, reason: str) -> None:
    """Re-run today's forecast (and plan) only if one was already made today; else the daily run covers it."""
    d = clock.today()
    async with read_session() as s:
        today_run = (await s.execute(select(CashForecastRun.id).where(CashForecastRun.business_id == bid,
                                                                      CashForecastRun.generated_on == d).limit(1))).first()
    if today_run is None:
        return
    async with write_session() as s:
        add_audit(s, "forecast_refreshed", business_id=bid, agent="cashflow", inputs={"reason": reason})
    await graphs.refresh(bid)


async def on_po_approved_sent(env: dict[str, Any]) -> None:
    await _refresh_if_forecast_today(_bid(env), f"order {env['payload']['po_id']} sent")


async def on_invoice_posted(env: dict[str, Any]) -> None:
    """The invoice replaces the order's commitment (counted once) and its payment is scheduled."""
    await _refresh_if_forecast_today(_bid(env), f"invoice {env['payload']['invoice_id']} posted")


async def on_invoice_held(env: dict[str, Any]) -> None:
    """A held invoice does not replace its order: the order amount keeps counting."""
    await _refresh_if_forecast_today(_bid(env), f"invoice {env['payload']['invoice_id']} held")


async def on_customer_payment(env: dict[str, Any]) -> None:
    """Cancel reminders for the paid invoice, then re-forecast."""
    await graphs.on_customer_payment(env)
    await _refresh_if_forecast_today(_bid(env), "customer payment received")


def register() -> None:
    from app.core.events import subscribe

    subscribe("po.drafted", "cashflow", on_po_drafted)
    subscribe("po.approved_sent", "cashflow", on_po_approved_sent)
    subscribe("invoice.posted", "cashflow", on_invoice_posted)
    subscribe("invoice.held", "cashflow", on_invoice_held)
    subscribe("customer_payment.received", "cashflow", on_customer_payment)
