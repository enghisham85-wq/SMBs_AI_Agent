"""Weekly purchasing budget for the Stock Agent, tightened when the pessimistic forecast dips below the buffer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import Money

HEADROOM = 1.25
MIN_CUT, MAX_CUT = 0.15, 0.40


@dataclass(frozen=True)
class Budget:
    week_start: date
    amount_minor: int
    tightened: bool
    reason_en: str
    reason_ar: str


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def compute(d: date, usual_weekly_minor: int, pessimistic_gap_minor: int, weeks_to_low: float,
            currency: str) -> Budget:
    """`pessimistic_gap_minor` > 0 when the pessimistic scenario falls below the buffer."""
    ws = week_start(d)
    normal = int(usual_weekly_minor * HEADROOM)
    if pessimistic_gap_minor <= 0 or usual_weekly_minor <= 0:
        return Budget(ws, normal, False, "usual weekly supplier spending plus 25% headroom",
                      "متوسط إنفاق الموردين الأسبوعي مع هامش 25%")
    cut = pessimistic_gap_minor / (usual_weekly_minor * max(1.0, weeks_to_low))
    cut = min(MAX_CUT, max(MIN_CUT, cut))
    amount = int(usual_weekly_minor * (1 - cut))
    gap = Money(pessimistic_gap_minor, currency)
    return Budget(ws, amount, True,
                  f"tightened by {cut * 100:.0f}%: the pessimistic forecast falls {gap.to_display()} below the buffer",
                  f"تم التخفيض بنسبة {cut * 100:.0f}%: التوقع المتشائم أقل من الحد الأدنى بمقدار {gap.to_display('ar')}")


async def remaining(s: AsyncSession, business_id: uuid.UUID, d: date, exclude_po: uuid.UUID | None = None) -> int | None:
    """What's left of this week's budget (None = no budget). Cancelled, rejected and deferred orders don't count."""
    from app.models.cash import PurchasingBudget
    from app.models.purchasing import PurchaseOrder

    ws = week_start(d)
    row = (await s.execute(select(PurchasingBudget).where(PurchasingBudget.business_id == business_id,
                                                          PurchasingBudget.week_start == ws))).scalar_one_or_none()
    if row is None:
        return None
    orders = (await s.execute(select(PurchaseOrder).where(
        PurchaseOrder.business_id == business_id, PurchaseOrder.created_at >= datetime.combine(ws, time.min),
        PurchaseOrder.status.not_in(("cancelled", "rejected", "on_hold"))))).scalars().all()
    spent = sum(po.total.amount_minor for po in orders if po.id != exclude_po)
    return max(0, row.amount.amount_minor - spent)
