"""Cash-Flow checks (FR-030, FR-031). Plain functions returning harness `Check`s."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from app.agents.cashflow.position import Freshness
from app.agents.cashflow.projection import Inputs, ObligationIn, Projection, occurrences
from app.db.types import Money
from app.harness.action_spec import Check

# How bank lines and projected flows are grouped when finding which line was wrong.
TXN_CATEGORY = {
    "cash_sales": "sales", "card_settlement": "sales", "transfer_sales": "sales", "settlement": "sales",
    "sales_cash": "sales", "card_fee": "card_fee", "customer_payment": "receivables",
    "supplier_payment": "suppliers", "supplier_invoice": "suppliers", "obligation": "obligations",
    "transfer": "internal", "cash_deposit_out": "internal", "cash_deposit_in": "internal",
}
FLOW_CATEGORY = {
    "sales": "sales", "card_fee": "card_fee", "receivable": "receivables", "payable": "suppliers", "po": "suppliers",
    "planned_purchases": "suppliers", "obligation": "obligations", "financing": "financing",
}
CATEGORY_NAMES = {
    "sales": ("sales takings", "إيرادات المبيعات"), "card_fee": ("card fees", "رسوم البطاقات"),
    "receivables": ("customer payments", "مدفوعات العملاء"), "suppliers": ("supplier payments", "مدفوعات الموردين"),
    "obligations": ("rent, salaries and bills", "الإيجار والرواتب والفواتير"),
    "unplanned": ("an unplanned payment", "دفعة غير مخطط لها"), "financing": ("financing", "التمويل"),
}


def txn_category(kind: str, amount_minor: int) -> str:
    cat = TXN_CATEGORY.get(kind)
    if cat:
        return cat
    return "unplanned" if amount_minor < 0 else "sales"


def forecast_vs_actual(day: date, projected_closing: int, actual_closing: int, projected_flows: list[dict[str, Any]],
                       actual_txns: list[tuple[str, int]], threshold_pct: float, currency: str) -> Check:
    """Compare a day's projected and actual closing balance; name the line that explains the gap."""
    variance = actual_closing - projected_closing
    base = max(abs(projected_closing), 1)
    pct = abs(variance) / base * 100
    lines: dict[str, dict[str, int]] = defaultdict(lambda: {"projected": 0, "actual": 0})
    for f in projected_flows:
        lines[FLOW_CATEGORY.get(f["kind"], "unplanned")]["projected"] += int(f["amount_minor"])
    for kind, amount in actual_txns:
        cat = txn_category(kind, amount)
        if cat != "internal":
            lines[cat]["actual"] += amount
    for v in lines.values():
        v["diff"] = v["actual"] - v["projected"]
    wrong = max(lines.items(), key=lambda kv: abs(kv[1]["diff"]))[0] if lines else None
    passed = pct <= threshold_pct
    details: dict[str, Any] = {"date": day.isoformat(), "projected_closing_minor": projected_closing,
                               "actual_closing_minor": actual_closing, "variance_minor": variance,
                               "variance_pct": round(pct, 1), "lines": dict(lines), "wrong_line": wrong}
    reason_en = reason_ar = ""
    if not passed and wrong:
        diff = lines[wrong]["diff"]
        en, ar = CATEGORY_NAMES.get(wrong, (wrong, wrong))
        more = diff > 0
        amt = Money(abs(diff), currency)
        reason_en = (f"Yesterday's closing balance was {Money(abs(variance), currency).to_display()} "
                     f"{'above' if variance > 0 else 'below'} my forecast, mostly because {en} were "
                     f"{amt.to_display()} {'higher' if more else 'lower'} than planned")
        reason_ar = (f"جاء رصيد الإغلاق أمس {'أعلى' if variance > 0 else 'أقل'} من توقعي بمقدار "
                     f"{Money(abs(variance), currency).to_display('ar')}، والسبب الأساسي أن {ar} "
                     f"{'زادت' if more else 'قلّت'} بمقدار {amt.to_display('ar')}")
    return Check("forecast_vs_actual", passed, details, reason_en=reason_en, reason_ar=reason_ar)


def bank_freshness(fresh: Freshness) -> Check:
    passed = not fresh.low_confidence
    details = {"bank_data_as_of": fresh.bank_data_as_of, "last_bank_date": fresh.last_bank_date,
               "age_business_days": fresh.age_business_days, "stale": fresh.stale,
               "missing_dates": [d.isoformat() for d in fresh.missing_dates]}
    if passed:
        return Check("bank_freshness", True, details)
    if fresh.missing_dates:
        days = ", ".join(d.strftime("%d %b") for d in fresh.missing_dates)
        en = f"Bank data is missing for {days}, so the cash forecast is low confidence. Please upload a statement for those days"
        ar = f"بيانات البنك ناقصة ليوم {days}، لذا التوقع النقدي منخفض الثقة. يرجى رفع كشف حساب لتلك الأيام"
    elif fresh.last_bank_date is None:
        en = "I have no bank data yet. Please upload a bank statement"
        ar = "لا توجد بيانات بنكية بعد. يرجى رفع كشف حساب"
    else:
        when = fresh.last_bank_date.strftime("%d %b")
        en = f"The latest bank data is from {when}, so the cash forecast is low confidence. Please upload a fresh statement"
        ar = f"آخر بيانات بنكية بتاريخ {when}، لذا التوقع النقدي منخفض الثقة. يرجى رفع كشف حساب حديث"
    return Check("bank_freshness", False, details, ask=True, reason_en=en, reason_ar=ar)


_DIGITS = re.compile(r"[\d/.\-:]+")


def _norm_desc(text: str) -> str:
    return re.sub(r"\s+", " ", _DIGITS.sub(" ", text.lower())).strip()


def missing_recurring_obligation(obligations: list[ObligationIn], txns: list[dict[str, Any]], today: date,
                                 currency: str) -> list[Check]:
    """Obligations due but not seen in the bank, and monthly bank payments that are not in the obligations.

    `txns`: recent bank lines [{date, amount_minor, description, kind, obligation_id?}].
    """
    out: list[Check] = []
    outflows = [t for t in txns if t["amount_minor"] < 0]
    last_bank = max((t["date"] for t in txns), default=None)
    for ob in obligations:
        if ob.recurrence == "once" or last_bank is None:
            continue
        for due in occurrences(ob, today - timedelta(days=10), min(today, last_bank) - timedelta(days=3)):
            seen = any(abs((t["date"] - due).days) <= 3 and (t.get("obligation_id") == ob.id or -t["amount_minor"] == ob.amount_minor)
                       for t in outflows)
            if not seen:
                amt = Money(ob.amount_minor, currency)
                out.append(Check("missing_recurring_obligation", False,
                                 {"obligation_id": ob.id, "due": due.isoformat(), "kind": "not_seen_in_bank",
                                  "amount_minor": ob.amount_minor}, ask=True,
                                 reason_en=f"{ob.description} ({amt.to_display()}) was due on {due.strftime('%d %b')} "
                                           f"but I don't see it in the bank. Was it paid another way, or is it still due?",
                                 reason_ar=f"كان {ob.description} ({amt.to_display('ar')}) مستحقاً في {due.strftime('%d/%m')} "
                                           f"لكني لا أراه في البنك. هل دُفع بطريقة أخرى أم ما زال مستحقاً؟"))
    known = {o.amount_minor for o in obligations}
    skip = {"supplier_payment", "supplier_invoice", "card_fee", "transfer", "cash_deposit_out", "cash_deposit_in",
            "obligation"}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in outflows:
        if t.get("kind") in skip or t.get("obligation_id"):
            continue
        groups[_norm_desc(t["description"])].append(t)
    for desc, rows in groups.items():
        months = {(t["date"].year, t["date"].month) for t in rows}
        if len(months) < 2 or len(rows) > len(months) * 1.5 or not desc:
            continue
        amounts = [-t["amount_minor"] for t in rows]
        typical = sorted(amounts)[len(amounts) // 2]
        if max(amounts) - min(amounts) > typical * 0.05:
            continue
        if any(abs(k - typical) <= typical * 0.02 for k in known):
            continue
        amt = Money(typical, currency)
        out.append(Check("missing_recurring_obligation", False,
                         {"kind": "not_in_forecast", "description": rows[-1]["description"], "amount_minor": typical,
                          "day_of_month": rows[-1]["date"].day, "months": len(months)}, ask=True,
                         reason_en=f"I see '{rows[-1]['description']}' of about {amt.to_display()} every month, "
                                   f"but it is not in your recurring payments. Should I add it to the forecast?",
                         reason_ar=f"أرى '{rows[-1]['description']}' بحوالي {amt.to_display('ar')} كل شهر، "
                                   f"لكنه ليس ضمن المدفوعات المتكررة. هل أضيفه إلى التوقع؟"))
    return out


def double_counting(proj: Projection) -> Check:
    """Each payable once: a PO with an invoice is not counted again, and no flow appears twice."""
    keys: dict[str, int] = defaultdict(int)
    for f in proj.flows:
        keys[f.key] += 1
    dup_keys = [k for k, n in keys.items() if n > 1 and not k.startswith("adjustment")]
    po_refs = {f.ref for f in proj.flows if f.kind == "po" and f.ref}
    invoiced = {d["id"] for d in proj.deduped if d["kind"] == "po"}
    both = sorted(str(x) for x in po_refs & invoiced)
    passed = not dup_keys and not both
    return Check("double_counting", passed, {"duplicate_keys": dup_keys, "po_and_invoice": both, "deduped": proj.deduped},
                 reason_en="" if passed else "a payment is counted twice in the forecast",
                 reason_ar="" if passed else "دفعة محسوبة مرتين في التوقع")


def unrealistic_inflow(inp: Inputs, tolerance: float = 1.10) -> Check:
    """Expected daily sales above the historical P95 make the pessimistic scenario the primary one."""
    hist: list[int] = sorted(v for v in inp.history_daily_sales if v > 0)
    if len(hist) < 14:
        return Check("unrealistic_inflow", True, {"note": "not enough history", "days": len(hist)})
    p95 = hist[min(len(hist) - 1, int(round(0.95 * (len(hist) - 1))))]
    limit = int(p95 * tolerance)
    high = [{"date": d.isoformat(), "expected_minor": v[1]} for d, v in sorted(inp.sales.items()) if v[1] > limit]
    passed = not high
    en = ar = ""
    if not passed:
        en = (f"My sales forecast for {len(high)} day(s) is above anything seen recently "
              f"(top 5% of days: {Money(p95, inp.currency).to_display()}), so I am planning on the pessimistic case")
        ar = (f"توقع المبيعات لـ {len(high)} يوم أعلى مما تحقق مؤخراً "
              f"(أعلى 5% من الأيام: {Money(p95, inp.currency).to_display('ar')})، لذا أخطط على السيناريو المتشائم")
    return Check("unrealistic_inflow", passed, {"p95_minor": p95, "limit_minor": limit, "days": high[:10]},
                 reason_en=en, reason_ar=ar)
