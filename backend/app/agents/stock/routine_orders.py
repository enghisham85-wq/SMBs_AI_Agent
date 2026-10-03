"""Routine orders: offer to stop asking about them.

After a streak of unchanged approvals we propose a rule like "approve routine orders from Golden Bakery
up to EGP 1,500 without asking me". Only the owner can turn it on; a rejected offer isn't repeated.
"""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.db.engine import read_session
from app.db.types import Money
from app.harness import rules
from app.harness.calibration import auto_approve_factor
from app.models.harness import ApprovalRequest
from app.models.master import Supplier
from app.models.purchasing import PurchaseOrder

STREAK = 5  # unchanged approvals in a row before offering
HEADROOM = Decimal("1.25")  # the limit sits this far above the largest of those orders


async def within_owner_rule(business_id: uuid.UUID, po: PurchaseOrder) -> uuid.UUID | None:
    """The auto-approval rule that covers this order, if any (and marks it applied)."""
    rule = await rules.auto_approval(business_id, "send_po", str(po.supplier_id))
    if rule is None:
        return None
    limit = int(Decimal(str(rule.trigger["auto_approve_up_to_minor"])) * Decimal(str(await auto_approve_factor(business_id, "stock"))))
    if not 0 < po.total.amount_minor <= limit:
        return None
    await rules.mark_applied(rule.id)
    return rule.id


async def suggest_after_approval(spec: Any, state: dict[str, Any]) -> None:
    """FINALIZE hook: offer auto-approval once there's a streak of unchanged approvals."""
    answer = state.get("approval") or {}
    if (spec.name != "send_po" or state.get("outcome") != "completed" or answer.get("via") == "auto_approve"
            or answer.get("option_key") != "approve" or answer.get("timed_out")):
        return
    bid = uuid.UUID(state["business_id"])
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(state["inputs"]["po_id"]))
        if po is None:
            return
        sup = await s.get(Supplier, po.supplier_id)
        asked = (await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.business_id == bid, ApprovalRequest.agent == "stock", ApprovalRequest.kind == "approval",
            ApprovalRequest.status.in_(("resolved", "timed_out"))).order_by(ApprovalRequest.created_at.desc()).limit(200))).scalars().all()
        recent = []
        for req in asked:
            other = await s.get(PurchaseOrder, uuid.UUID(req.context["po_id"])) if req.context.get("po_id") else None
            if other is not None and other.supplier_id == po.supplier_id:
                recent.append((req, other))
            if len(recent) == STREAK:
                break
    if sup is None or len(recent) < STREAK:
        return
    if any(req.status != "resolved" or req.resolved_option != "approve" or req.resolved_via == "system" for req, _ in recent):
        return  # an edit, rejection or timeout in the streak: the owner still wants to look
    seen = await rules.recent_for_trigger(bid, "policy", {"action_type": "send_po", "supplier_id": str(sup.id)},
                                          ("proposed", "active", "rejected"))
    if any(r.trigger.get("auto_approve_up_to_minor") is not None for r in seen):
        return  # already offered (and possibly declined)
    cur = po.total.currency
    largest = max(other.total.amount_minor for _, other in recent)
    step = 100 * 10 ** Money(0, cur).decimals  # round up to a whole 100 in the currency
    limit = int(math.ceil(Decimal(largest) * HEADROOM / step) * step)
    shown = Money(limit, cur)
    await rules.propose(
        business_id=bid, agent="stock", kind="policy", source_incident_id=None,
        rule_text_en=f"Approve routine orders from {sup.name_en} up to {shown.to_display()} without asking me",
        rule_text_ar=f"وافق على الطلبات المعتادة من {sup.name_ar or sup.name_en} حتى {shown.to_display('ar')} دون سؤالي",
        trigger={"action_type": "send_po", "supplier_id": str(sup.id), "auto_approve_up_to_minor": limit})


def register() -> None:
    from app.harness.graph import FINALIZE_HOOKS

    if suggest_after_approval not in FINALIZE_HOOKS:
        FINALIZE_HOOKS.append(suggest_after_approval)  # type: ignore[arg-type]
