"""VAT summary per period and the reminder before a VAT period closes.

Input and output VAT come from the ledger (VAT input / VAT output accounts), so they match the books
exactly. The supporting list shows the period's supplier invoices and customer invoices; each supplier
invoice is flagged when VAT is charged without a valid tax registration number, or when its VAT
breakdown is missing or does not add up. Tax figures are high-impact: the summary is prepared
by the reversible `prepare_vat_summary` action, whose proposal the independent second check reviews
before the summary is marked ready; a disagreement escalates to the owner.
"""

from __future__ import annotations

import re
import uuid
from calendar import monthrange
from datetime import date
from typing import Any

from sqlalchemy import select

from app.agents.accountant import posting
from app.agents.accountant.checks import tax_id_valid
from app.db.engine import read_session
from app.db.types import Money
from app.harness.action_spec import ActionContext, ActionSpec, VerifyOutcome, register
from app.harness.verifier import build_packet
from app.models.books import JournalEntry, JournalLine, PayableInvoice, ReceivableInvoice
from app.models.finance_master import Account, BankAccount, BankTransaction
from app.models.harness import Action
from app.models.master import Supplier
from app.models.tenancy import Business

SPEC = "prepare_vat_summary"
REMIND_DAYS = 10  # remind when the period closes within this many days
_PERIOD = re.compile(r"^(\d{4})-(?:(0[1-9]|1[0-2])|Q([1-4]))$")


class PeriodError(ValueError):
    pass


# ------------------------------------------------------------------ periods
def period_for(d: date, kind: str) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}" if kind == "quarterly" else f"{d.year}-{d.month:02d}"


def bounds(period: str) -> tuple[date, date]:
    """'2026-10' -> 1-31 Oct; '2026-Q4' -> 1 Oct - 31 Dec."""
    m = _PERIOD.match(period)
    if not m:
        raise PeriodError("period must look like 2026-10 (monthly) or 2026-Q4 (quarterly)")
    year = int(m.group(1))
    if m.group(2):
        first_month = last_month = int(m.group(2))
    else:
        first_month = (int(m.group(3)) - 1) * 3 + 1
        last_month = first_month + 2
    return date(year, first_month, 1), date(year, last_month, monthrange(year, last_month)[1])


# ------------------------------------------------------------------ summary
async def compute(business_id: uuid.UUID, period: str) -> dict[str, Any]:
    start, end = bounds(period)
    async with read_session() as s:
        b = await s.get(Business, business_id)
        assert b is not None
        accts = {a.code: a.id for a in (await s.execute(select(Account).where(Account.business_id == business_id))).scalars()}
        lines = (await s.execute(
            select(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(JournalEntry.business_id == business_id, JournalEntry.status != "quarantined",
                   JournalEntry.date >= start, JournalEntry.date <= end,
                   JournalLine.account_id.in_([accts.get(posting.VAT_IN), accts.get(posting.VAT_OUT)])))).scalars().all()
        payables = (await s.execute(select(PayableInvoice).where(
            PayableInvoice.business_id == business_id, PayableInvoice.status.in_(("posted", "paid")),
            PayableInvoice.invoice_date >= start, PayableInvoice.invoice_date <= end)
            .order_by(PayableInvoice.invoice_date))).scalars().all()
        receivables = (await s.execute(select(ReceivableInvoice).where(
            ReceivableInvoice.business_id == business_id, ReceivableInvoice.status != "void",
            ReceivableInvoice.invoice_date >= start, ReceivableInvoice.invoice_date <= end)
            .order_by(ReceivableInvoice.invoice_date))).scalars().all()
        suppliers = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == business_id))).scalars()}
    cur = b.currency
    input_minor = sum(ln.debit_minor - ln.credit_minor for ln in lines if ln.account_id == accts.get(posting.VAT_IN))
    output_minor = sum(ln.credit_minor - ln.debit_minor for ln in lines if ln.account_id == accts.get(posting.VAT_OUT))
    purchases: list[dict[str, Any]] = []
    for inv in payables:
        vat = inv.vat_amount.amount_minor
        flags = []
        if vat > 0 and not tax_id_valid(inv.supplier_vat_number, b.tax_id_pattern):
            flags.append("missing_vat_number")
        if (inv.subtotal.amount_minor + vat != inv.total.amount_minor
                or any(ln.get("vat_rate_percent") in (None, "") for ln in inv.lines)):
            flags.append("missing_breakdown")
        sp = suppliers.get(inv.supplier_id) if inv.supplier_id else None
        purchases.append({"id": inv.id, "number": inv.invoice_number, "date": inv.invoice_date,
                          "supplier_en": sp.name_en if sp else None, "supplier_ar": sp.name_ar if sp else None,
                          "vat_number": inv.supplier_vat_number, "net": inv.subtotal, "vat": inv.vat_amount,
                          "total": inv.total, "flags": flags})
    sales = [{"id": r.id, "number": r.number, "date": r.invoice_date, "customer": r.customer_name, "net": r.subtotal,
              "vat": r.vat_amount, "total": r.total} for r in receivables]
    invoiced_input = sum(p["vat"].amount_minor for p in purchases)
    return {
        "period": period, "from": start, "to": end, "vat_period": b.vat_period, "vat_rate_percent": b.vat_rate_percent,
        "input_vat": Money(input_minor, cur), "output_vat": Money(output_minor, cur),
        "net_payable": Money(output_minor - input_minor, cur),
        "purchases": purchases, "sales": sales,
        # Input VAT on the period's supplier invoices vs the ledger (they differ only if something is off).
        "invoice_input_vat": Money(invoiced_input, cur),
        "flag_counts": {f: sum(1 for p in purchases if f in p["flags"]) for f in ("missing_vat_number", "missing_breakdown")},
    }


def totals(summary: dict[str, Any]) -> dict[str, int]:
    return {k: summary[k].amount_minor if isinstance(summary[k], Money) else int(summary[k]["amount_minor"])
            for k in ("input_vat", "output_vat", "net_payable", "invoice_input_vat")}


# ------------------------------------------------------------------ prepare_vat_summary
async def _execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"period": inputs["period"], "totals": totals(await compute(ctx.business_id, inputs["period"]))}


async def _verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    again = totals(await compute(ctx.business_id, inputs["period"]))  # read back: the books have not moved
    return VerifyOutcome(again == result.get("totals"), {"read_back": again})


async def _packet(ctx: ActionContext, inputs: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    summary = await compute(ctx.business_id, inputs["period"])
    t = totals(summary)
    issues = []
    if t["invoice_input_vat"] != t["input_vat"]:
        issues.append({"field": "input_vat", "severity": "high",
                       "problem": f"supplier invoices carry {t['invoice_input_vat']} input VAT but the ledger has {t['input_vat']}"})
    if t["net_payable"] != t["output_vat"] - t["input_vat"]:
        issues.append({"field": "net_payable", "severity": "high", "problem": "net payable is not output VAT minus input VAT"})
    flagged = summary["flag_counts"]["missing_vat_number"] + summary["flag_counts"]["missing_breakdown"]
    if flagged:
        issues.append({"field": "purchases", "severity": "low",
                       "problem": f"{flagged} invoice(s) lack a valid VAT number or breakdown; their input VAT may not be claimable"})
    return build_packet(SPEC, {"period": inputs["period"],
                               "invoices": [{"number": p["number"], "vat": p["vat"], "net": p["net"], "flags": p["flags"]}
                                            for p in summary["purchases"]],
                               "sales_invoices": [{"number": r["number"], "vat": r["vat"]} for r in summary["sales"]],
                               "ledger_vat": {"input": t["input_vat"], "output": t["output_vat"]}},
                        {"input_vat": t["input_vat"], "output_vat": t["output_vat"], "net_payable": t["net_payable"]},
                        known_issues=issues)


async def _plan(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"intent": f"prepare the VAT summary for {inputs['period']}", "summary": inputs["period"]}


async def _nothing_to_undo(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    """The summary writes no records (its figures live on the action), so a rollback has nothing to undo."""


def register_spec() -> None:
    register(ActionSpec(name=SPEC, agent="accountant", risk_class="reversible", execute=_execute, plan=_plan,
                        verify=_verify, compensate=_nothing_to_undo, verifier_packet=_packet,
                        title_en="Prepare VAT summary", title_ar="إعداد ملخص ضريبة القيمة المضافة"))


async def prepared(business_id: uuid.UUID, period: str, current: dict[str, int]) -> Action | None:
    """The newest finished prepare_vat_summary run for this period, if it was made from today's figures."""
    async with read_session() as s:
        rows = (await s.execute(select(Action).where(Action.business_id == business_id, Action.type == SPEC,
                                                     Action.stage.in_(("completed", "escalated")))
                                .order_by(Action.created_at.desc()).limit(50))).scalars().all()
    newest = next((a for a in rows if a.inputs.get("period") == period), None)
    return newest if newest is not None and newest.inputs.get("totals") == current else None


async def summary_with_status(business_id: uuid.UUID, period: str) -> dict[str, Any]:
    """Compute the summary; run prepare_vat_summary when the figures are new, and report its status."""
    from app.harness.graph import run_action

    summary = await compute(business_id, period)
    current = totals(summary)
    action = await prepared(business_id, period, current)
    if action is None:  # new or changed figures: prepare and review them again
        out = await run_action(SPEC, {"period": period, "totals": current}, business_id)
        async with read_session() as s:
            action = await s.get(Action, out["action_id"])
    assert action is not None
    summary["status"] = "ready" if action.stage == "completed" else "needs_owner"
    summary["action_id"] = action.id
    summary["review"] = action.verifier_verdict
    return summary


# ------------------------------------------------------------------ reminder before the period closes
async def step_period_reminder(business_id: uuid.UUID, d: date) -> dict[str, Any] | None:
    """'VAT period ends in 9 days. 4 bank payments have no invoice.' (once per period and count)."""
    from app.approvals import service as approvals

    async with read_session() as s:
        b = await s.get(Business, business_id)
        if b is None or not b.vat_registered:
            return None
        period = period_for(d, b.vat_period)
        start, end = bounds(period)
        days_left = (end - d).days
        if days_left > REMIND_DAYS:
            return None
        banks = [a.id for a in (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id,
                                                                            BankAccount.is_cash_on_hand.is_(False)))).scalars()]
        payments = (await s.execute(select(BankTransaction).where(
            BankTransaction.business_id == business_id, BankTransaction.account_id.in_(banks),
            BankTransaction.date >= start, BankTransaction.date <= d,
            BankTransaction.match_status.in_(("unmatched", "suggested"))))).scalars().all()
    payments = [t for t in payments if t.amount.amount_minor < 0]
    if not payments:
        return None
    n = len(payments)
    when_en = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
    when_ar = "اليوم" if days_left == 0 else f"بعد {days_left} يوم"
    await approvals.post_alert(
        business_id=business_id, agent="accountant", urgency=2 if days_left <= 3 else 1,
        dedupe_key=f"vat_period:{period}:{n}", context={"vat_period": period, "unmatched_payments": n},
        text_en=f"VAT period {period} ends {when_en}. {n} bank payment{'s have' if n != 1 else ' has'} no invoice; "
                "send the invoices so their input VAT can be claimed.",
        text_ar=f"تنتهي فترة ضريبة القيمة المضافة {period} {when_ar}. {n} مدفوعات بنكية بلا فاتورة؛ "
                "أرسل الفواتير ليمكن خصم ضريبتها.")
    return {"period": period, "days_left": days_left, "unmatched_payments": n}

