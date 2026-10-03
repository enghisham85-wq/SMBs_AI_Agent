from __future__ import annotations

import functools
import re
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.accountant.extraction import Normalised
from app.core.i18n import normalize_arabic_name, normalize_digits
from app.harness.action_spec import Check
from app.models.books import PayableInvoice
from app.models.master import Item
from app.models.purchasing import Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine

# Real tax numbers are 9-15 digits. The cap also limits backtracking on a badly written owner pattern,
# since `re` has no timeout.
MAX_TAX_ID_LEN = 20


@functools.lru_cache(maxsize=64)
def _tax_id_regex(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def tax_id_valid(value: str | None, pattern: str) -> bool:
    if not value or len(value) > MAX_TAX_ID_LEN:
        return False
    rx = _tax_id_regex(pattern)
    return rx is not None and rx.fullmatch(value) is not None


def normalise_number(number: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", normalize_digits(number or "").upper())


def extraction_arithmetic(n: Normalised, default_vat_percent: Decimal) -> Check:
    """Lines sum to subtotal; subtotal + VAT = total; VAT = sum(line base x line rate / 100)."""
    lines_sum = sum(ln["line_total_minor"] for ln in n.lines)
    vat_calc = 0
    for ln in n.lines:
        rate = Decimal(ln["vat_rate_percent"]) if ln["vat_rate_percent"] is not None else default_vat_percent
        vat_calc += int((Decimal(ln["line_total_minor"]) * rate / 100).quantize(Decimal(1), ROUND_HALF_UP))
    tol = max(1, len(n.lines))  # within 1 minor unit per line
    subtotal = n.subtotal_minor if n.subtotal_minor is not None else lines_sum
    vat = n.vat_minor if n.vat_minor is not None else vat_calc
    total = n.total_minor
    problems = []
    if abs(lines_sum - subtotal) > tol:
        problems.append("lines_vs_subtotal")
    if abs(vat - vat_calc) > tol:
        problems.append("vat_vs_rate")
    if total is None or abs(subtotal + vat - total) > tol:
        problems.append("total_vs_subtotal_plus_vat")
    return Check("extraction_arithmetic", not problems,
                 {"problems": problems, "lines_sum_minor": lines_sum, "subtotal_minor": subtotal, "vat_minor": vat,
                  "vat_calc_minor": vat_calc, "printed_total_minor": total, "calc_total_minor": lines_sum + vat_calc},
                 reason_en="the total does not equal its lines plus VAT", reason_ar="الإجمالي لا يساوي البنود مع الضريبة")


async def duplicate_invoice(s: AsyncSession, business_id: uuid.UUID, supplier_id: uuid.UUID | None, number: str | None,
                            total_minor: int | None, invoice_date: date | None, sha256: str,
                            exclude_id: uuid.UUID | None = None) -> Check:
    q = select(PayableInvoice).where(PayableInvoice.business_id == business_id,
                                     PayableInvoice.status.in_(("held", "posted", "paid")))
    rows = [r for r in (await s.execute(q)).scalars() if r.id != exclude_id]
    norm = normalise_number(number)
    for r in rows:
        if supplier_id is not None and r.supplier_id == supplier_id and norm and r.normalised_number == norm:
            return Check("duplicate_invoice", False, {"existing_invoice_id": r.id, "kind": "same_number",
                                                      "number": r.invoice_number}, ask=True,
                         reason_en=f"invoice {r.invoice_number} from this supplier was already recorded",
                         reason_ar=f"الفاتورة {r.invoice_number} من هذا المورد مسجلة بالفعل")
    for r in rows:
        if (supplier_id is not None and r.supplier_id == supplier_id and total_minor is not None
                and r.total.amount_minor == total_minor and r.invoice_date == invoice_date):
            return Check("duplicate_invoice", False, {"existing_invoice_id": r.id, "kind": "same_amount_and_date",
                                                      "number": r.invoice_number}, ask=True,
                         reason_en=f"an invoice with the same amount and date ({r.invoice_number}) already exists",
                         reason_ar=f"توجد فاتورة بنفس المبلغ والتاريخ ({r.invoice_number})")
    return Check("duplicate_invoice", True, {})


def _swap(d: date) -> date | None:
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def date_sanity(n: Normalised, today: date, supplier_hint: str | None, default_format: str) -> tuple[Check, date | None, str | None]:
    """Returns (check, corrected date or None, format used). An impossible date gets its day/month swapped."""
    d = n.invoice_date
    if d is None:
        return Check("date_sanity", False, {"problem": "missing"}, ask=True, reason_en="the invoice date could not be read",
                     reason_ar="تعذرت قراءة تاريخ الفاتورة"), None, None
    fmt = supplier_hint or default_format
    ambiguous = n.date_format_observed == "ambiguous" and d.day <= 12 and d.month <= 12
    candidate = d
    if ambiguous and n.invoice_date_raw:
        parts = [int(x) for x in re.findall(r"\d+", normalize_digits(n.invoice_date_raw))[:3]]
        if len(parts) == 3:
            a, b, y = parts
            try:
                candidate = date(y, b, a) if fmt == "DMY" else date(y, a, b)
            except ValueError:
                candidate = d

    def impossible(x: date) -> str | None:
        if x > today:
            return "in the future"
        if x < today - timedelta(days=365):
            return "more than a year old"
        if n.due_date is not None and n.due_date < x:
            return "due before it was issued"
        return None

    problem = impossible(candidate)
    if problem is None:
        return Check("date_sanity", True, {"date": candidate, "format": fmt if ambiguous else None}), candidate, (
            fmt if ambiguous else None)
    swapped = _swap(candidate)
    if swapped is not None and impossible(swapped) is None:
        other = "MDY" if fmt == "DMY" else "DMY"
        return Check("date_sanity", False, {"problem": problem, "read_as": candidate, "corrected_to": swapped,
                                            "format": other, "corrected": True},
                     reason_en=f"the date read as {candidate.isoformat()} is {problem}; it must be {swapped.isoformat()} "
                               f"(this supplier writes dates {other})",
                     reason_ar=f"التاريخ المقروء {candidate.isoformat()} غير منطقي؛ الصحيح {swapped.isoformat()}"), swapped, other
    return Check("date_sanity", False, {"problem": problem, "read_as": candidate}, ask=True,
                 reason_en=f"the invoice date {candidate.isoformat()} is {problem}",
                 reason_ar=f"تاريخ الفاتورة {candidate.isoformat()} غير منطقي"), None, None


def balanced_entry(lines: list[tuple[str, int, int]]) -> Check:
    dr = sum(d for _, d, _ in lines)
    cr = sum(c for _, _, c in lines)
    return Check("balanced_entry", dr == cr and dr > 0, {"debit": dr, "credit": cr},
                 reason_en="journal entry does not balance")


def supplier_vat_validity(vat_number: str | None, vat_minor: int | None, pattern: str) -> Check:
    charged = (vat_minor or 0) > 0
    ok = not charged or tax_id_valid(vat_number, pattern)
    return Check("supplier_vat_validity", ok, {"vat_number": vat_number, "vat_charged": charged},
                 reason_en="VAT is charged but the supplier's tax registration number is missing or invalid",
                 reason_ar="تم احتساب ضريبة لكن رقم التسجيل الضريبي للمورد مفقود أو غير صالح")


async def match_items(s: AsyncSession, business_id: uuid.UUID, lines: list[dict[str, Any]]) -> list[uuid.UUID | None]:
    """Map invoice lines to stock items by name (English or Arabic, normalised)."""
    items = list((await s.execute(select(Item).where(Item.business_id == business_id, Item.is_ingredient.is_(True)))).scalars())
    out: list[uuid.UUID | None] = []
    for ln in lines:
        desc = normalize_arabic_name(ln.get("description", ""))
        best = None
        for it in items:
            for name in (it.name_en, it.name_ar):
                key = normalize_arabic_name(name.split("(")[0])
                if key and (key in desc or desc in key):
                    best = it.id
                    break
            if best:
                break
        out.append(best)
    return out


async def three_way_match(s: AsyncSession, business_id: uuid.UUID, supplier_id: uuid.UUID | None,
                          lines: list[dict[str, Any]], item_ids: list[uuid.UUID | None]) -> tuple[Check, uuid.UUID | None]:
    if supplier_id is None or not any(item_ids):
        return Check("three_way_match", True, {"note": "no stock lines"}), None
    pos = list((await s.execute(select(PurchaseOrder).where(
        PurchaseOrder.business_id == business_id, PurchaseOrder.supplier_id == supplier_id,
        PurchaseOrder.status.in_(("sent", "partially_received", "received"))).order_by(PurchaseOrder.created_at.desc()))).scalars())
    invoiced = {iid: Decimal(ln["qty"]) for ln, iid in zip(lines, item_ids, strict=False) if iid}
    prices = {iid: ln["unit_price_minor"] for ln, iid in zip(lines, item_ids, strict=False) if iid}
    for po in pos:
        po_lines = {pl.item_id: pl for pl in (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars()}
        if not set(invoiced) & set(po_lines):
            continue
        billed_before = (await s.execute(select(PayableInvoice.id).where(PayableInvoice.purchase_order_id == po.id,
                                                                         PayableInvoice.status.in_(("posted", "paid"))))).first()
        if billed_before:
            continue
        delivered: dict[uuid.UUID, Decimal] = {}
        for dl in (await s.execute(select(DeliveryLine).join(Delivery, Delivery.id == DeliveryLine.delivery_id)
                                   .where(Delivery.po_id == po.id))).scalars():
            delivered[dl.item_id] = delivered.get(dl.item_id, Decimal(0)) + dl.qty_received
        diffs = []
        for iid, qty in invoiced.items():
            pl = po_lines.get(iid)
            got = delivered.get(iid, Decimal(0))
            if qty != got:
                diffs.append({"item_id": iid, "kind": "quantity", "invoiced": qty, "delivered": got,
                              "ordered": pl.qty if pl else None})
            if pl is not None and abs(prices[iid] - pl.unit_price.amount_minor) > max(1, pl.unit_price.amount_minor // 100):
                diffs.append({"item_id": iid, "kind": "price", "invoiced_price_minor": prices[iid],
                              "ordered_price_minor": pl.unit_price.amount_minor})
        ok = not diffs
        return Check("three_way_match", ok, {"po_id": po.id, "po_number": po.number, "differences": diffs}, ask=True,
                     reason_en="the invoice does not match what was ordered and delivered",
                     reason_ar="الفاتورة لا تطابق ما تم طلبه واستلامه"), po.id
    return Check("missing_po_or_delivery", False, {"note": "no matching purchase order"},
                 reason_en="this invoice has no matching purchase order or delivery",
                 reason_ar="لا يوجد أمر شراء أو توريد مطابق لهذه الفاتورة"), None


VALUATION_MIN_MINOR_UNITS = 1000  # ignore differences below 10.00 in a 2-decimal currency


async def stock_valuation(s: AsyncSession, business_id: uuid.UUID, tolerance_pct: float) -> Check:
    """Inventory account vs the Stock Agent's valuation, minus goods delivered but not yet invoiced."""
    from app.models.books import JournalEntry, JournalLine
    from app.models.finance_master import Account
    from app.models.stock_ops import StockLevel

    accts = [a.id for a in (await s.execute(select(Account).where(Account.business_id == business_id,
                                                                   Account.is_inventory.is_(True)))).scalars()]
    rows = (await s.execute(select(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).where(
        JournalLine.account_id.in_(accts), JournalEntry.status.in_(("posted", "reversed"))))).scalars().all() if accts else []
    ledger = sum(r.debit_minor - r.credit_minor for r in rows)
    items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == business_id))).scalars()}
    stock_value = 0
    currency = None
    for lvl in (await s.execute(select(StockLevel).where(StockLevel.business_id == business_id))).scalars():
        item = items.get(lvl.item_id)
        if item is not None and lvl.quantity > 0:
            stock_value += item.unit_cost.times(lvl.quantity).amount_minor
            currency = item.unit_cost.currency
    invoiced = {i.purchase_order_id for i in (await s.execute(select(PayableInvoice).where(
        PayableInvoice.business_id == business_id, PayableInvoice.status.in_(("posted", "paid"))))).scalars()}
    grni = 0
    for dl, ln in (await s.execute(select(Delivery, DeliveryLine).join(DeliveryLine, DeliveryLine.delivery_id == Delivery.id)
                                   .where(Delivery.business_id == business_id))).all():
        if dl.po_id not in invoiced:
            grni += ln.unit_price_on_note.times(ln.qty_received).amount_minor
    expected_ledger = stock_value - grni
    diff = ledger - expected_ledger
    limit = max(VALUATION_MIN_MINOR_UNITS, int(abs(stock_value) * tolerance_pct / 100))
    passed = abs(diff) <= limit
    return Check("stock_valuation", passed, {"ledger_minor": ledger, "stock_value_minor": stock_value,
                                             "received_not_invoiced_minor": grni, "difference_minor": diff,
                                             "limit_minor": limit, "currency": currency},
                 reason_en="" if passed else "the inventory account does not agree with the stock records",
                 reason_ar="" if passed else "حساب المخزون لا يتفق مع سجلات المخزون")
