"""Stock Agent event handlers. Each runs as the Stock Agent."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.approvals import service as approvals
from app.core import clock, settings_store
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.audit import add_audit
from app.harness.graph import run_action
from app.harness.incidents import open_incident
from app.models.master import Item, Supplier
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine

DEFER_DAYS = 5
UNSENT = ("draft", "pending_approval", "approved")
INVOICE_ISSUE_PENALTY = 0.05


def _bid(env: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(env["business_id"])


# ------------------------------------------------------------------ budget.check_result
async def on_budget_check_result(env: dict[str, Any]) -> None:
    """Proceed, or hand the disagreement to conflict_graph (the owner sees both positions)."""
    bid = _bid(env)
    p = env["payload"]
    po_id = uuid.UUID(p["po_id"])
    conflict = bool(p.get("conflict"))
    status = "ok" if not conflict else ("proceed" if p.get("is_critical") else "conflict")
    async with write_session() as s:
        po = await s.get(PurchaseOrder, po_id)
        if po is None:
            return
        po.notes = {**(po.notes or {}), "budget": {"within_budget": p["within_budget"],
                                                   "recommendation": p["recommendation"], "conflict": conflict,
                                                   "remaining_budget": p.get("remaining_budget"), "status": status}}
    if conflict:
        from app.graphs.conflict import start_conflict

        await start_conflict(bid, po_id, p)


# ------------------------------------------------------------------ budget.updated / shortfall.predicted
async def can_wait(po: PurchaseOrder, d: date, days: int = DEFER_DAYS) -> bool:
    """A non-critical order can wait if its items will not run out before the deferral ends plus delivery."""
    if po.is_critical_order:
        return False
    stockout = (po.notes or {}).get("projected_stockout")
    if not stockout:
        return True
    lead = max(1, (po.expected_date - po.created_at.date()).days) if po.created_at else 1
    return date.fromisoformat(stockout) > d + timedelta(days=days + lead)


async def defer_non_critical(bid: uuid.UUID, reason: str, days: int = DEFER_DAYS) -> list[str]:
    """Defer unsent non-critical orders that can wait; critical items and high-margin fast movers go first."""
    d = clock.today()
    async with read_session() as s:
        pos = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == bid,
                                                           PurchaseOrder.status.in_(UNSENT)))).scalars().all()
        ids = [po.id for po in pos]
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id.in_(ids)))).scalars().all() if ids else []
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == bid))).scalars()}
    high_margin = {ln.po_id for ln in lines if items.get(ln.item_id) and items[ln.item_id].margin_class == "high"}
    deferred = []
    for po in pos:
        if po.id in high_margin or (po.notes or {}).get("budget", {}).get("status") in ("proceed", "deferred"):
            continue
        if not await can_wait(po, d, days):
            continue
        out = await run_action("defer_po", {"po_id": str(po.id), "days": days, "reason": reason}, bid)
        if out["outcome"] == "completed":
            deferred.append(po.number)
    return deferred


async def on_budget_updated(env: dict[str, Any]) -> None:
    if not env["payload"].get("tightened"):
        return
    bid = _bid(env)
    deferred = await defer_non_critical(bid, "purchasing budget tightened")
    if deferred:
        await approvals.post_alert(
            business_id=bid, agent="stock", dedupe_key=f"budget_defer:{env['event_id']}",
            text_en=f"The weekly purchasing budget was tightened, so I delayed {', '.join(deferred)} by {DEFER_DAYS} days. "
                    "Critical items and high-margin fast movers are still ordered first.",
            text_ar=f"تم تخفيض ميزانية المشتريات الأسبوعية، لذا أجّلت {'، '.join(deferred)} {DEFER_DAYS} أيام. "
                    "ما زالت الأصناف الحرجة والأصناف سريعة البيع عالية الربح تُطلب أولاً.")


async def on_shortfall_predicted(env: dict[str, Any]) -> None:
    bid = _bid(env)
    p = env["payload"]
    deferred = await defer_non_critical(bid, f"cash shortfall predicted on {p['gap_date']}")
    async with write_session() as s:
        add_audit(s, "shortfall_reprioritised", business_id=bid, agent="stock",
                  inputs={"plan_id": p.get("plan_id"), "gap_date": p["gap_date"]}, outputs={"deferred": deferred})


# ------------------------------------------------------------------ invoice.posted / invoice.held
async def on_invoice_posted(env: dict[str, Any]) -> None:
    """Update item cost from the invoice; a change above the threshold publishes price.changed."""
    bid = _bid(env)
    p = env["payload"]
    pct_limit = float(await settings_store.get(bid, "price_change_pct"))
    async with write_session() as s:
        for uc in p.get("unit_costs", []):
            if not uc.get("item_id"):
                continue
            item = await s.get(Item, uuid.UUID(str(uc["item_id"])))
            if item is None or item.business_id != bid:
                continue
            old = item.unit_cost.amount_minor
            new = int(uc["unit_price_minor"])
            if new <= 0 or new == old:
                continue
            item.unit_cost = Money(new, item.unit_cost.currency)
            pct = (new - old) / old * 100 if old else 100.0
            if abs(pct) > pct_limit:
                publish(s, "price.changed", {"supplier_id": p.get("supplier_id"), "item_id": item.id,
                                             "old_price": Money(old, item.unit_cost.currency),
                                             "new_price": Money(new, item.unit_cost.currency), "pct": round(pct, 1),
                                             "source": "invoice", "invoice_id": p["invoice_id"]},
                        producer="stock", business_id=bid)


async def on_invoice_held(env: dict[str, Any]) -> None:
    """Note the supplier issue and lower its reliability once per held invoice."""
    bid = _bid(env)
    p = env["payload"]
    reasons = p.get("reason") or []
    supplier_issue = any(r in ("three_way", "duplicate", "price") for r in reasons)
    if not supplier_issue or not p.get("supplier_id"):
        return
    async with write_session() as s:
        sup = await s.get(Supplier, uuid.UUID(str(p["supplier_id"])))
        if sup is None:
            return
        sup.reliability_score = round(max(0.0, sup.reliability_score - INVOICE_ISSUE_PENALTY), 3)
        publish(s, "supplier.performance_updated", {"supplier_id": sup.id, "reliability_score": sup.reliability_score,
                                                    "observed_lead_time": sup.observed_lead_time_days,
                                                    "reason": f"invoice held: {', '.join(reasons)}"},
                producer="stock", business_id=bid)
        add_audit(s, "supplier_issue_noted", business_id=bid, agent="stock",
                  inputs={"supplier_id": sup.id, "invoice_id": p["invoice_id"], "reasons": reasons})


# ------------------------------------------------------------------ stock_valuation.mismatch
async def on_stock_valuation_mismatch(env: dict[str, Any]) -> None:
    bid = _bid(env)
    p = env["payload"]
    diff = p["difference"]
    text = (f"Books and stock disagree on stock value: ledger {p['ledger_value']['display']}, "
            f"stock records {p['stock_value']['display']} (difference {diff['display']}).")
    inc = await open_incident(business_id=bid, agent="stock", type="stock_valuation", detected_by="stock_valuation_check",
                              summary=text, refs={**p, "joint_with": "accountant"}, dedupe_key="stock_valuation",
                              action_taken="asked for a stock count; the Accountant will review unposted invoices")
    await approvals.post_alert(business_id=bid, agent="stock", dedupe_key=f"stock_valuation:{inc}", urgency=2,
                               context={"incident_id": str(inc)},
                               text_en=text + " A stock count would settle it.",
                               text_ar=f"قيمة المخزون في الدفاتر تختلف عن سجلات المخزون (الفرق {diff['display']}). الجرد سيحسم الأمر.")


def register() -> None:
    from app.core.events import subscribe

    subscribe("budget.check_result", "stock", on_budget_check_result)
    subscribe("budget.updated", "stock", on_budget_updated)
    subscribe("shortfall.predicted", "stock", on_shortfall_predicted)
    subscribe("invoice.posted", "stock", on_invoice_posted)
    subscribe("invoice.held", "stock", on_invoice_held)
    subscribe("stock_valuation.mismatch", "stock", on_stock_valuation_mismatch)

