"""Customer (receivable) invoices (T082)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.db.types import Money
from app.models.books import ReceivableInvoice
from app.models.tenancy import Business


class ReceivableError(ValueError):
    def __init__(self, code: str, message_en: str, message_ar: str) -> None:
        super().__init__(message_en)
        self.code, self.message_en, self.message_ar = code, message_en, message_ar


@dataclass
class ComputedLines:
    lines: list[dict[str, Any]]
    subtotal: int
    vat: int


def compute(lines: list[dict[str, Any]], default_rate: Decimal, currency: str) -> ComputedLines:
    out, sub, vat = [], 0, 0
    for ln in lines:
        qty = Decimal(str(ln["qty"]))
        if qty <= 0:
            raise ReceivableError("invalid_qty", "Quantity must be greater than 0.", "يجب أن تكون الكمية أكبر من صفر.")
        price = Money.from_decimal(Decimal(str(ln["unit_price"])), currency) if "unit_price" in ln else Money(int(ln["unit_price_minor"]), currency)
        rate = Decimal(str(ln["vat_rate_percent"])) if ln.get("vat_rate_percent") is not None else default_rate
        total = price.times(qty).amount_minor
        line_vat = int((Decimal(total) * rate / 100).quantize(Decimal(1), ROUND_HALF_UP))
        out.append({"description": ln.get("description", ""), "qty": str(qty), "unit_price_minor": price.amount_minor,
                    "vat_rate_percent": str(rate), "line_total_minor": total, "vat_minor": line_vat})
        sub += total
        vat += line_vat
    return ComputedLines(out, sub, vat)


async def next_number(s: AsyncSession, business_id: uuid.UUID) -> str:
    n = (await s.execute(select(func.count()).select_from(ReceivableInvoice).where(
        ReceivableInvoice.business_id == business_id))).scalar_one()
    while True:
        n += 1
        cand = f"INV-{n:03d}"
        exists = (await s.execute(select(ReceivableInvoice.id).where(ReceivableInvoice.business_id == business_id,
                                                                     ReceivableInvoice.number == cand))).first()
        if not exists:
            return cand


async def build(s: AsyncSession, business: Business, data: dict[str, Any]) -> ReceivableInvoice:
    """Validate and build (not yet posted) a receivable invoice."""
    inv_date = date.fromisoformat(str(data["invoice_date"]))
    due = date.fromisoformat(str(data["due_date"]))
    if inv_date > clock.today():
        raise ReceivableError("future_date", "The invoice date cannot be in the future.", "لا يمكن أن يكون تاريخ الفاتورة في المستقبل.")
    if due < inv_date:
        raise ReceivableError("due_before_invoice", "The due date cannot be before the invoice date.", "لا يمكن أن يسبق تاريخ الاستحقاق تاريخ الفاتورة.")
    if not data.get("lines"):
        raise ReceivableError("no_lines", "Add at least one line.", "أضف بنداً واحداً على الأقل.")
    number = (data.get("number") or "").strip() or await next_number(s, business.id)
    dup = (await s.execute(select(ReceivableInvoice.id).where(ReceivableInvoice.business_id == business.id,
                                                              ReceivableInvoice.number == number))).first()
    if dup:
        raise ReceivableError("duplicate_number", f"Invoice number {number} already exists.", f"رقم الفاتورة {number} موجود بالفعل.")
    c = compute(data["lines"], business.vat_rate_percent, business.currency)
    return ReceivableInvoice(business_id=business.id, customer_name=data["customer_name"],
                             customer_contact=data.get("customer_contact"), customer_telegram=data.get("customer_telegram"),
                             number=number, invoice_date=inv_date, due_date=due, lines=c.lines,
                             subtotal=Money(c.subtotal, business.currency), vat_amount=Money(c.vat, business.currency),
                             total=Money(c.subtotal + c.vat, business.currency), amount_paid_minor=0, status="open",
                             source=data.get("source", "manual"),
                             late_payment_history_score=float(data.get("late_payment_history_score", 0.0)))


def outstanding(inv: ReceivableInvoice) -> int:
    return inv.total.amount_minor - inv.amount_paid_minor


def apply_payment(inv: ReceivableInvoice, amount_minor: int, paid_on: date) -> None:
    inv.amount_paid_minor += amount_minor
    if inv.amount_paid_minor >= inv.total.amount_minor:
        inv.status = "paid"
        inv.paid_on = paid_on
    else:
        inv.status = "partially_paid"


def ageing_bucket(inv: ReceivableInvoice, today: date) -> str:
    if inv.status in ("paid", "void"):
        return "settled"
    days = (today - inv.due_date).days
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    return "60+"
