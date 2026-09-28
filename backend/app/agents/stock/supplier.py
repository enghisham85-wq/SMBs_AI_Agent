"""Supplier performance (spec Stock "Supplier performance", FR-021, FR-017)."""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import publish
from app.models.master import Supplier
from app.models.purchasing import PurchaseOrder

LEAD_ALPHA = 0.3
LATE_PENALTY = 0.05


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def update_on_delivery(s: AsyncSession, supplier: Supplier, po: PurchaseOrder, received_on: date,
                       complete: bool, price_ok: bool) -> None:
    """Rolling observed lead time and reliability after a delivery (late days count into lead time)."""
    if po.sent_at is not None:
        actual_lead = max(0, (received_on - po.sent_at.date()).days)
        prev = supplier.observed_lead_time_days if supplier.observed_lead_time_days is not None else supplier.stated_lead_time_days
        supplier.observed_lead_time_days = round((1 - LEAD_ALPHA) * prev + LEAD_ALPHA * actual_lead, 2)
    on_time = received_on <= po.expected_date
    score = 1.0 - (0.0 if on_time else 0.3) - (0.0 if complete else 0.3) - (0.0 if price_ok else 0.2)
    if po.late_flagged:
        score = max(score, 0.0)  # the late penalty was already applied once by record_late
    supplier.reliability_score = round(_clamp(0.8 * supplier.reliability_score + 0.2 * score), 3)
    publish(s, "supplier.performance_updated", {"supplier_id": supplier.id, "reliability_score": supplier.reliability_score,
                                                "observed_lead_time": supplier.observed_lead_time_days},
            producer="stock", business_id=supplier.business_id)


def record_late(s: AsyncSession, supplier: Supplier, po: PurchaseOrder, days_late: int) -> bool:
    """Lower reliability once per PO (not once per day late). Returns True the first time."""
    if po.late_flagged:
        return False
    po.late_flagged = True
    po.notes = {**po.notes, "late_days_at_flag": days_late}
    supplier.reliability_score = round(_clamp(supplier.reliability_score - LATE_PENALTY), 3)
    publish(s, "supplier.performance_updated", {"supplier_id": supplier.id, "reliability_score": supplier.reliability_score,
                                                "observed_lead_time": supplier.observed_lead_time_days},
            producer="stock", business_id=supplier.business_id)
    return True


def lead_time(supplier: Supplier) -> float:
    """Order earlier from suppliers whose observed lead time is longer than stated."""
    return max(float(supplier.stated_lead_time_days), float(supplier.observed_lead_time_days or 0))
