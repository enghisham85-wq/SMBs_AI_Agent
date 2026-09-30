"""Accountant Agent actions run through harness_graph (T078, T082)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select

from app.agents.accountant import checks, posting, receivables
from app.core import clock, settings_store
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.action_spec import ActionContext, ActionSpec, Check, VerifyOutcome, register
from app.harness.verifier import build_packet
from app.models.books import JournalEntry, JournalLine, PayableInvoice, ReceivableInvoice
from app.models.finance_master import BankAccount, BankTransaction, Obligation, Sale
from app.models.master import Item, Supplier
from app.models.stock_ops import StockMovement
from app.models.tenancy import Business


async def _business(bid: uuid.UUID) -> Business:
    async with read_session() as s:
        b = await s.get(Business, bid)
    assert b is not None
    return b


async def _entry_balanced(entry_id: uuid.UUID | None) -> bool:
    if entry_id is None:
        return False
    async with read_session() as s:
        lines = (await s.execute(select(JournalLine).where(JournalLine.entry_id == entry_id))).scalars().all()
    dr, cr = sum(ln.debit_minor for ln in lines), sum(ln.credit_minor for ln in lines)
    return bool(lines) and dr == cr


async def _reverse(entry_id: str | None, action_id: uuid.UUID) -> None:
    if not entry_id:
        return
    async with write_session() as s:
        entry = await s.get(JournalEntry, uuid.UUID(entry_id))
        if entry is not None and entry.status == "posted":
            await posting.reverse(s, entry, action_id=action_id)


# =========================================================================== post_invoice
async def _inv(invoice_id: str) -> PayableInvoice:
    async with read_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(invoice_id))
    assert inv is not None
    return inv


async def _post_invoice_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    inv = await _inv(inputs["invoice_id"])
    out: list[Check] = []
    if not inputs.get("dup_ok"):
        async with read_session() as s:
            out.append(await checks.duplicate_invoice(s, ctx.business_id, inv.supplier_id, inv.invoice_number,
                                                      inv.total.amount_minor, inv.invoice_date, "", exclude_id=inv.id))
    draft = _invoice_draft(inv, "")
    out.append(checks.balanced_entry([(c, d, cr) for c, d, cr, _ in draft.lines]))
    return out


def _invoice_draft(inv: PayableInvoice, supplier_name: str) -> posting.Draft:
    return posting.payable_invoice(inv.invoice_date, str(inv.id), supplier_name, inv.lines, inv.vat_amount.amount_minor,
                                   inv.total.amount_minor)


async def _post_invoice_packet(ctx: ActionContext, inputs: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any] | None:
    inv = await _inv(inputs["invoice_id"])
    limit = int(await settings_store.get(ctx.business_id, "journal_value_limit"))
    if inv.total.amount_minor <= limit:
        return None
    async with read_session() as s:
        sup = await s.get(Supplier, inv.supplier_id) if inv.supplier_id else None
    draft = _invoice_draft(inv, sup.name_en if sup else "")
    known = [] if draft.balanced else [{"field": "entry", "problem": "entry does not balance", "severity": "high"}]
    return build_packet("post_invoice",
                        {"invoice": {"supplier": sup.name_en if sup else None, "number": inv.invoice_number,
                                     "date": inv.invoice_date, "lines": inv.lines, "subtotal": inv.subtotal,
                                     "vat": inv.vat_amount, "total": inv.total}},
                        {"journal": [{"account": c, "debit": d, "credit": cr} for c, d, cr, _ in draft.lines]},
                        known_issues=known)


async def _post_invoice_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(inputs["invoice_id"]))
        assert inv is not None
        sup = await s.get(Supplier, inv.supplier_id) if inv.supplier_id else None
        entry = await posting.post(s, ctx.business_id, _invoice_draft(inv, sup.name_en if sup else ""), inv.total.currency,
                                   action_id=ctx.action_id)
        inv.status = "posted"
        inv.hold_reason = None
        inv.journal_entry_id = entry.id
        unit_costs = [{"item_id": ln["item_id"], "unit_price_minor": ln["unit_price_minor"]} for ln in inv.lines if ln.get("item_id")]
        publish(s, "invoice.posted", {"invoice_id": inv.id, "supplier_id": inv.supplier_id, "po_id": inv.purchase_order_id,
                                      "due_date": inv.due_date, "total": inv.total, "unit_costs": unit_costs},
                producer="accountant", business_id=ctx.business_id, action_id=ctx.action_id)
        return {"invoice_id": str(inv.id), "entry_id": str(entry.id)}


async def _post_invoice_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    inv = await _inv(inputs["invoice_id"])
    balanced = await _entry_balanced(uuid.UUID(result["entry_id"]))
    dup_ok = True
    if not inputs.get("dup_ok"):
        async with read_session() as s:
            dup_ok = (await checks.duplicate_invoice(s, ctx.business_id, inv.supplier_id, inv.invoice_number,
                                                     inv.total.amount_minor, inv.invoice_date, "", exclude_id=inv.id)).passed
    return VerifyOutcome(inv.status == "posted" and balanced and dup_ok,
                         {"status": inv.status, "balanced": balanced, "no_duplicate": dup_ok})


async def _post_invoice_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    await _reverse(result.get("entry_id"), ctx.action_id)
    async with write_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(inputs["invoice_id"]))
        if inv is not None:
            inv.status = "held"
            inv.journal_entry_id = None


# =========================================================================== post_sales_summary
async def _existing_summary(bid: uuid.UUID, day: str) -> JournalEntry | None:
    async with read_session() as s:
        return (await s.execute(select(JournalEntry).where(JournalEntry.business_id == bid,
                                                           JournalEntry.reference_type == "sales_summary",
                                                           JournalEntry.reference_id == day,
                                                           JournalEntry.status == "posted"))).scalar_one_or_none()


async def _sales_summary_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    day = date.fromisoformat(inputs["date"])
    existing = await _existing_summary(ctx.business_id, inputs["date"])
    if existing is not None:
        return {"entry_id": str(existing.id), "already_posted": True}
    b = await _business(ctx.business_id)
    async with write_session() as s:
        by: dict[str, int] = defaultdict(int)
        for r in (await s.execute(select(Sale).where(Sale.business_id == ctx.business_id, Sale.date == day))).scalars():
            by[r.payment_method] += r.amount_total.amount_minor
        if not by:
            return {"entry_id": None, "no_sales": True}
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == ctx.business_id))).scalars()}
        cogs = 0
        for mv in (await s.execute(select(StockMovement).where(StockMovement.business_id == ctx.business_id,
                                                               StockMovement.date == day, StockMovement.type == "sale"))).scalars():
            item = items.get(mv.item_id)
            if item is not None:
                cogs += item.unit_cost.times(-mv.quantity).amount_minor
        entry = await posting.post(s, ctx.business_id, posting.sales_summary(day, dict(by), b.vat_rate_percent, cogs),
                                   b.currency, action_id=ctx.action_id)
        return {"entry_id": str(entry.id), "gross_minor": sum(by.values()), "cogs_minor": cogs}


async def _entry_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    if result.get("no_sales"):
        return VerifyOutcome(True, {"note": "no sales that day"})
    ok = await _entry_balanced(uuid.UUID(result["entry_id"])) if result.get("entry_id") else False
    return VerifyOutcome(ok, {"balanced": ok})


async def _entry_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    if not result.get("already_posted"):
        await _reverse(result.get("entry_id"), ctx.action_id)


# =========================================================================== apply_bank_match
async def _match_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    c = inputs["candidate"]
    today = clock.today()
    async with write_session() as s:
        txn = await s.get(BankTransaction, uuid.UUID(inputs["txn_id"]))
        assert txn is not None
        acct = await s.get(BankAccount, txn.account_id)
        assert acct is not None
        bank = acct.ledger_account_code
        amt = abs(txn.amount.amount_minor)
        cur = txn.amount.currency
        draft: posting.Draft | None = None
        undo: dict[str, Any] = {"prev_status": txn.match_status}
        kind = c["type"]
        if kind == "settlement":
            draft = posting.settlement(txn.date, str(txn.id), amt, bank)
        elif kind == "card_fee":
            draft = posting.bank_expense(txn.date, str(txn.id), "Card processing fee", posting.BANK_CHARGES, amt, bank)
        elif kind == "obligation":
            ob = await s.get(Obligation, uuid.UUID(c["ref"]))
            code = c.get("extra", {}).get("account_code", "5900")
            draft = posting.bank_expense(txn.date, str(txn.id), f"{ob.type if ob else ''} {ob.description if ob else ''}", code, amt, bank)
        elif kind == "supplier_invoice":
            inv = await s.get(PayableInvoice, uuid.UUID(c["ref"]))
            assert inv is not None
            draft = posting.supplier_payment(txn.date, str(txn.id), c.get("name", ""), amt, bank)
            undo["invoice"] = {"id": str(inv.id), "status": inv.status, "paid": inv.amount_paid_minor}
            inv.amount_paid_minor += amt
            if inv.amount_paid_minor >= inv.total.amount_minor:
                inv.status, inv.paid_on = "paid", txn.date
        elif kind == "customer_payment":
            rec = await s.get(ReceivableInvoice, uuid.UUID(c["ref"]))
            assert rec is not None
            draft = posting.customer_payment(txn.date, str(txn.id), rec.customer_name, amt, bank)
            undo["receivable"] = {"id": str(rec.id), "status": rec.status, "paid": rec.amount_paid_minor, "paid_on": rec.paid_on}
            receivables.apply_payment(rec, amt, txn.date)
            publish(s, "customer_payment.received", {"receivable_invoice_id": rec.id, "amount": Money(amt, cur),
                                                     "bank_txn_id": txn.id, "fully_paid": rec.status == "paid"},
                    producer="accountant", business_id=ctx.business_id, action_id=ctx.action_id)
        elif kind == "transfer":
            other = await s.get(BankTransaction, uuid.UUID(c["extra"]["counterpart_id"]))
            assert other is not None
            other_acct = await s.get(BankAccount, other.account_id)
            assert other_acct is not None
            src, dst = (other_acct.ledger_account_code, bank) if txn.amount.amount_minor > 0 else (bank, other_acct.ledger_account_code)
            draft = posting.transfer(txn.date, str(txn.id), src, dst, amt)
            other.match_status, other.matched_type, other.matched_id = "auto_matched" if inputs["source"] == "auto" else "confirmed", "transfer", txn.id
            other.match_confidence, other.match_source = inputs.get("confidence"), inputs["source"]
            undo["counterpart"] = str(other.id)
        elif kind == "expense":
            draft = posting.bank_expense(txn.date, str(txn.id), txn.description, c["extra"]["account_code"], amt, bank)
        entry_id = None
        if draft is not None:
            entry = await posting.post(s, ctx.business_id, draft, cur, action_id=ctx.action_id)
            entry_id = str(entry.id)
        txn.match_status = "auto_matched" if inputs["source"] == "auto" else "confirmed"
        txn.matched_type = kind
        try:
            txn.matched_id = uuid.UUID(c["ref"])
        except (ValueError, KeyError):
            txn.matched_id = None
        txn.match_confidence = inputs.get("confidence")
        txn.match_source = inputs["source"]
        undo["matched_on"] = today.isoformat()
        return {"entry_id": entry_id, "undo": undo}


async def _match_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        txn = await s.get(BankTransaction, uuid.UUID(inputs["txn_id"]))
    ok = txn is not None and txn.match_status in ("auto_matched", "confirmed")
    if result.get("entry_id"):
        ok = ok and await _entry_balanced(uuid.UUID(result["entry_id"]))
    return VerifyOutcome(ok, {"status": txn.match_status if txn else None})


async def _match_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    await _reverse(result.get("entry_id"), ctx.action_id)
    undo = result.get("undo", {})
    async with write_session() as s:
        txn = await s.get(BankTransaction, uuid.UUID(inputs["txn_id"]))
        if txn is not None:
            txn.match_status, txn.matched_type, txn.matched_id, txn.match_source = undo.get("prev_status", "unmatched"), None, None, None
        if "invoice" in undo:
            inv = await s.get(PayableInvoice, uuid.UUID(undo["invoice"]["id"]))
            if inv is not None:
                inv.status, inv.amount_paid_minor, inv.paid_on = undo["invoice"]["status"], undo["invoice"]["paid"], None
        if "receivable" in undo:
            rec = await s.get(ReceivableInvoice, uuid.UUID(undo["receivable"]["id"]))
            if rec is not None:
                rec.status, rec.amount_paid_minor = undo["receivable"]["status"], undo["receivable"]["paid"]
                rec.paid_on = date.fromisoformat(undo["receivable"]["paid_on"]) if undo["receivable"].get("paid_on") else None
        if "counterpart" in undo:
            other = await s.get(BankTransaction, uuid.UUID(undo["counterpart"]))
            if other is not None:
                other.match_status, other.matched_type, other.matched_id = "unmatched", None, None


# =========================================================================== quarantine_entry
async def _quarantine_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        e = await s.get(JournalEntry, uuid.UUID(inputs["entry_id"]))
        assert e is not None
        prev = e.status
        e.status = "quarantined"
        e.memo = f"{e.memo} [quarantined: {inputs.get('reason', '')}]"[:1000]
        return {"prev_status": prev}


async def _quarantine_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        e = await s.get(JournalEntry, uuid.UUID(inputs["entry_id"]))
    return VerifyOutcome(e is not None and e.status == "quarantined")


async def _quarantine_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        e = await s.get(JournalEntry, uuid.UUID(inputs["entry_id"]))
        if e is not None:
            e.status = result.get("prev_status", "posted")


# =========================================================================== receivables
async def _create_receivable_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    b = await _business(ctx.business_id)
    async with write_session() as s:
        rec = await receivables.build(s, b, inputs)
        s.add(rec)
        await s.flush()
        entry = await posting.post(s, ctx.business_id,
                                   posting.receivable_invoice(rec.invoice_date, str(rec.id), rec.customer_name,
                                                              rec.subtotal.amount_minor, rec.vat_amount.amount_minor),
                                   b.currency, action_id=ctx.action_id)
        rec.journal_entry_id = entry.id
        publish(s, "receivable.created", {"receivable_invoice_id": rec.id, "total": rec.total, "due_date": rec.due_date},
                producer="accountant", business_id=ctx.business_id, action_id=ctx.action_id)
        return {"invoice_id": str(rec.id), "entry_id": str(entry.id), "number": rec.number, "total_minor": rec.total.amount_minor}


async def _create_receivable_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(result["invoice_id"]))
        ar = (await s.execute(select(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                              .where(JournalEntry.id == uuid.UUID(result["entry_id"])))).scalars().all()
        accts = await posting.accounts(s, ctx.business_id)
    ar_debit = sum(ln.debit_minor for ln in ar if ln.account_id == accts[posting.AR].id)
    ok = rec is not None and await _entry_balanced(uuid.UUID(result["entry_id"])) and ar_debit == result["total_minor"]
    return VerifyOutcome(ok, {"ar_increase": ar_debit, "total": result["total_minor"]})


async def _create_receivable_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    await _reverse(result.get("entry_id"), ctx.action_id)
    if result.get("invoice_id"):
        async with write_session() as s:
            rec = await s.get(ReceivableInvoice, uuid.UUID(result["invoice_id"]))
            if rec is not None:
                rec.status = "void"


async def _void_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    async with read_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(inputs["invoice_id"]))
    ok = rec is not None and rec.amount_paid_minor == 0 and rec.status == "open"
    return [Check("void_allowed", ok, {"paid_minor": rec.amount_paid_minor if rec else None},
                  reason_en="only an unpaid invoice can be voided")]


async def _void_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(inputs["invoice_id"]))
        assert rec is not None
        rev_id = None
        if rec.journal_entry_id:
            entry = await s.get(JournalEntry, rec.journal_entry_id)
            if entry is not None and entry.status == "posted":
                rev = await posting.reverse(s, entry, action_id=ctx.action_id, memo=f"Void {rec.number}: {inputs.get('reason', '')}")
                rev_id = str(rev.id)
        rec.status = "void"
        return {"reversal_id": rev_id}


async def _void_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(inputs["invoice_id"]))
    return VerifyOutcome(rec is not None and rec.status == "void")


def register_specs() -> None:
    register(ActionSpec(name="post_invoice", agent="accountant", risk_class="reversible", execute=_post_invoice_execute,
                        title_en="Post supplier invoice", title_ar="ترحيل فاتورة مورد", preconditions=_post_invoice_checks,
                        verify=_post_invoice_verify, compensate=_post_invoice_compensate, verifier_packet=_post_invoice_packet))
    register(ActionSpec(name="post_sales_summary", agent="accountant", risk_class="reversible", execute=_sales_summary_execute,
                        title_en="Post daily sales", title_ar="ترحيل مبيعات اليوم", verify=_entry_verify,
                        compensate=_entry_compensate))
    register(ActionSpec(name="apply_bank_match", agent="accountant", risk_class="reversible", execute=_match_execute,
                        title_en="Reconcile bank line", title_ar="مطابقة حركة بنكية", verify=_match_verify,
                        compensate=_match_compensate))
    register(ActionSpec(name="quarantine_entry", agent="accountant", risk_class="reversible", execute=_quarantine_execute,
                        title_en="Quarantine journal entry", title_ar="عزل قيد", verify=_quarantine_verify,
                        compensate=_quarantine_compensate))
    register(ActionSpec(name="create_receivable", agent="accountant", risk_class="reversible", execute=_create_receivable_execute,
                        title_en="Create customer invoice", title_ar="إنشاء فاتورة عميل", verify=_create_receivable_verify,
                        compensate=_create_receivable_compensate))
    register(ActionSpec(name="void_receivable", agent="accountant", risk_class="reversible", execute=_void_execute,
                        title_en="Void customer invoice", title_ar="إلغاء فاتورة عميل", preconditions=_void_checks,
                        verify=_void_verify))
