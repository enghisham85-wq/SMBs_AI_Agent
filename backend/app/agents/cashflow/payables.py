"""Supplier payment timing (FR-028): early for a discount, on time, or end of terms when cash is tight.

Never later than the due date: paying beyond terms needs an explicit owner instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

ON_TIME_MARGIN_DAYS = 2  # pay a little before the due date so a transfer lands in time


@dataclass(frozen=True)
class Terms:
    invoice_date: date
    due_date: date
    discount_percent: Decimal | None = None
    discount_days: int | None = None


@dataclass(frozen=True)
class Recommendation:
    pay_on: date
    method: str  # early_discount | on_time | end_of_terms | overdue | owner_instruction
    discount_minor: int
    reason_en: str
    reason_ar: str


def recommend(terms: Terms, amount_minor: int, today: date, *, tight: bool,
              owner_pay_on: date | None = None) -> Recommendation:
    """When to pay one supplier invoice. `owner_pay_on` is the only way past the due date."""
    earliest = today + timedelta(days=1)
    if owner_pay_on is not None:
        return Recommendation(max(owner_pay_on, earliest), "owner_instruction", 0,
                              "paid on the date the owner chose", "الدفع في التاريخ الذي حدده المالك")
    if terms.due_date < earliest:
        return Recommendation(earliest, "overdue", 0, "already due: pay now", "مستحقة بالفعل: ادفع الآن")
    if not tight and terms.discount_percent and terms.discount_days is not None:
        last_discount_day = terms.invoice_date + timedelta(days=terms.discount_days)
        if last_discount_day >= earliest:
            discount = int((Decimal(amount_minor) * terms.discount_percent / 100).to_integral_value())
            return Recommendation(min(last_discount_day, terms.due_date), "early_discount", discount,
                                  f"pay early for a {terms.discount_percent}% discount",
                                  f"ادفع مبكراً للحصول على خصم {terms.discount_percent}%")
    if tight:
        return Recommendation(terms.due_date, "end_of_terms", 0, "cash is tight: pay on the last day of terms",
                              "السيولة محدودة: ادفع في آخر يوم من المهلة")
    return Recommendation(max(earliest, terms.due_date - timedelta(days=ON_TIME_MARGIN_DAYS)), "on_time", 0,
                          "pay on time", "ادفع في الموعد")


def within_terms(rec: Recommendation, terms: Terms) -> bool:
    return rec.method in ("owner_instruction", "overdue") or rec.pay_on <= terms.due_date
