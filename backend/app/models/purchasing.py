"""Purchase orders and deliveries. Written by the Stock Agent."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, Money, TenantMixin, money_col
from app.models.master import QTY

PO_STATUSES = (
    "draft", "pending_approval", "approved", "sent", "partially_received", "received", "closed",
    "rejected", "on_hold", "cancelled",
)
OPEN_STATUSES = ("draft", "pending_approval", "approved", "sent", "partially_received", "on_hold")

# Allowed purchase order status transitions
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_approval", "approved", "on_hold", "cancelled", "rejected"},
    "on_hold": {"draft", "cancelled"},
    "pending_approval": {"approved", "rejected", "draft"},
    "approved": {"sent", "cancelled"},
    "sent": {"partially_received", "received", "cancelled"},
    "partially_received": {"received", "closed"},
    "received": {"closed"},
    "closed": set(),
    "rejected": set(),
    "cancelled": set(),
}


class IllegalTransitionError(ValueError):
    pass


class PurchaseOrder(TenantMixin, Base):
    __tablename__ = "purchase_order"

    number: Mapped[str] = mapped_column(String(20), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("supplier.id"), index=True)
    status: Mapped[str] = mapped_column(Enum(*PO_STATUSES, native_enum=False), default="draft", index=True)
    total: Mapped[Money] = money_col("total")
    expected_date: Mapped[date] = mapped_column(Date)
    is_critical_order: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    late_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    # Why it was drafted: projected stockout, forecast used, hold reasons ...
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PurchaseOrderLine(TenantMixin, Base):
    __tablename__ = "purchase_order_line"

    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("purchase_order.id"), index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(String(20))
    pack_size: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    unit_price: Mapped[Money] = money_col("unit_price")
    line_total: Mapped[Money] = money_col("line_total")


class Delivery(TenantMixin, Base):
    __tablename__ = "delivery"

    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("purchase_order.id"), index=True)
    received_on: Mapped[date] = mapped_column(Date)
    received_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    photo_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("file_ref.id"), nullable=True)
    # [{item_id, ordered, received, ordered_price_minor, note_price_minor, kind}]
    discrepancies: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class DeliveryLine(TenantMixin, Base):
    __tablename__ = "delivery_line"

    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("delivery.id"), index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"))
    qty_received: Mapped[Decimal] = mapped_column(QTY)
    unit_price_on_note: Mapped[Money] = money_col("unit_price_on_note")


def transition(po: PurchaseOrder, to: str) -> None:
    """Move a PO to a new status, refusing moves the data model does not allow."""
    if to not in TRANSITIONS.get(po.status, set()):
        raise IllegalTransitionError(f"PO {po.number}: {po.status} -> {to} is not allowed")
    po.status = to
