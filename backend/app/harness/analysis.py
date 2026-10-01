"""Incident analysis -> root cause and a proposed learned rule (FR-007, FR-008).

Uses Claude (IncidentAnalysis, RuleProposal) with deterministic templates offline. Per-type templates
live in TEMPLATES; every escalated action (`<action>.escalated`) has a generic one. A proposed
trigger must validate against its kind's schema (rule_schemas), else the template's is used.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from app.db.engine import read_session
from app.harness import rule_schemas, rules
from app.llm.client import LLMRefusalError, LLMUnavailableError, MissingFixtureError, get_llm, text_block
from app.llm.schemas import IncidentAnalysis, RuleProposal
from app.models.harness import Incident

PROMPTS = Path(__file__).resolve().parent.parent / "llm" / "prompts"
Template = Callable[[Incident], tuple[IncidentAnalysis, RuleProposal] | None]
TEMPLATES: dict[str, Template] = {}
_LLM_FAILURES = (LLMRefusalError, LLMUnavailableError, MissingFixtureError, AttributeError)


def template(incident_type: str) -> Callable[[Template], Template]:
    def deco(fn: Template) -> Template:
        TEMPLATES[incident_type] = fn
        return fn

    return deco


@template("recurring_correction")
def _recurring(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal]:
    r = inc.refs
    return (
        IncidentAnalysis(root_cause=f"The owner corrected {r['supplier_en']} to account {r['account_code']} "
                                    f"{r['count']} times; the automatic classification keeps suggesting another account.",
                         category="model_error"),
        RuleProposal(rule_text_en=f"Always classify {r['supplier_en']} as {r['account_name_en']} ({r['account_code']})",
                     rule_text_ar=f"صنّف دائماً {r['supplier_ar']} كـ {r['account_name_ar']} ({r['account_code']})",
                     kind="classification", trigger={"supplier_id": r["supplier_id"], "account_code": r["account_code"]},
                     expected_effect="future invoices from this supplier are classified without asking"),
    )


@template("three_way_mismatch")
def _three_way(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal] | None:
    r = inc.refs
    if not r.get("supplier_id"):
        return None
    return (
        IncidentAnalysis(root_cause=f"{r['supplier_en']} invoiced the full order before the delivery was checked; "
                                    "the delivery was short.", category="external"),
        RuleProposal(rule_text_en=f"Always wait for delivery confirmation before posting invoices from {r['supplier_en']}",
                     rule_text_ar=f"انتظر دائماً تأكيد الاستلام قبل ترحيل فواتير {r.get('supplier_ar') or r['supplier_en']}",
                     kind="precondition",
                     trigger={"action_type": "post_invoice", "field": "delivery_confirmed", "op": "eq", "value": True,
                              "when": {"supplier_id": r["supplier_id"]}},
                     expected_effect="invoices from this supplier wait until the delivery is recorded"),
    )


@template("date_format")
def _date_format(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal] | None:
    r = inc.refs
    if not r.get("supplier_id") or r.get("format") not in ("DMY", "MDY"):
        return None
    name = inc.summary.split(":", 1)[0]
    other = "MDY" if r["format"] == "DMY" else "DMY"
    pretty = {"DMY": "DD/MM", "MDY": "MM/DD"}
    return (
        IncidentAnalysis(root_cause=f"{name} writes dates as {pretty[r['format']]}; the first reading gave an impossible date.",
                         category="data_format"),
        RuleProposal(rule_text_en=f"{name} invoices use {pretty[r['format']]} format; never parse as {pretty[other]}",
                     rule_text_ar=f"فواتير {name} تستخدم صيغة {pretty[r['format']]}؛ لا تقرأها أبداً كـ {pretty[other]}",
                     kind="parsing_hint", trigger={"supplier_id": r["supplier_id"], "date_format": r["format"]},
                     expected_effect="dates from this supplier are read correctly the first time"),
    )


@template("duplicate_invoice")
def _duplicate_invoice(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal] | None:
    r = inc.refs
    if not r.get("supplier_id"):
        return None
    name, name_ar = r.get("supplier_en") or "This supplier", r.get("supplier_ar") or r.get("supplier_en") or "هذا المورد"
    return (
        IncidentAnalysis(root_cause=f"{name} sent invoice {r.get('invoice_number')} a second time (another copy of the "
                                    "same invoice); posting it would have doubled the payable and the payment.",
                         category="duplicate"),
        RuleProposal(rule_text_en=f"{name} sometimes re-sends invoices as a new copy; match the invoice number "
                                  "before reading it as new",
                     rule_text_ar=f"قد يعيد {name_ar} إرسال الفواتير كنسخة جديدة؛ طابق رقم الفاتورة قبل اعتبارها جديدة",
                     kind="parsing_hint",
                     trigger={"supplier_id": r["supplier_id"],
                              "hint": "re-sends invoices; check the invoice number against earlier invoices first"},
                     expected_effect="repeat copies from this supplier are recognised as duplicates immediately"),
    )


@template("price_sanity")
def _price_spike(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal] | None:
    r = inc.refs
    if not r.get("supplier_id"):
        return None
    name, name_ar = r.get("supplier_en") or "this supplier", r.get("supplier_ar") or r.get("supplier_en") or "هذا المورد"
    return (
        IncidentAnalysis(root_cause=f"{name} raised the price of {r.get('item_en') or 'an item'} by "
                                    f"{float(r.get('change_pct') or 0):+.0f}% compared with its usual price.",
                         category="pricing"),
        RuleProposal(rule_text_en=f"Hold orders from {name} for my review while its prices are rising",
                     rule_text_ar=f"أوقف طلبات الشراء من {name_ar} لمراجعتي ما دامت أسعاره ترتفع",
                     kind="precondition",
                     trigger={"action_type": "draft_po", "field": "supplier_id", "op": "ne", "value": r["supplier_id"]},
                     expected_effect="orders to this supplier wait for the owner instead of going ahead"),
    )


@template("forecast_accuracy")
def _demand_spike(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal]:
    names = ", ".join(i.get("name_en", "") for i in inc.refs.get("items", [])) or "several items"
    return (
        IncidentAnalysis(root_cause=f"Sales of {names} were far above the forecast, most likely an unusual event "
                                    "nearby; the usual forecasting method did not expect it.", category="external"),
        RuleProposal(rule_text_en="While demand is unusual, never send purchase orders without asking me",
                     rule_text_ar="ما دام الطلب غير معتاد، لا ترسل أوامر شراء دون سؤالي",
                     kind="policy", trigger={"action_type": "send_po", "require_approval": True},
                     expected_effect="orders are not auto-approved until the forecast is reliable again"),
    )


@template("invoice_still_unpaid")
def _paid_before_reminder(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal] | None:
    customer = inc.refs.get("customer")
    if not customer:
        return None
    return (
        IncidentAnalysis(root_cause=f"{customer} paid invoice {inc.refs.get('invoice')} shortly before its reminder "
                                    "was due; the payment was in the bank but not yet matched.", category="external"),
        RuleProposal(rule_text_en=f"Ask me before sending payment reminders to {customer}; they often pay just in time",
                     rule_text_ar=f"اسألني قبل إرسال تذكيرات الدفع إلى {customer}؛ فهم غالباً يدفعون في الوقت المناسب",
                     kind="precondition",
                     trigger={"action_type": "send_reminder", "field": "customer", "op": "ne", "value": customer},
                     expected_effect="reminders to this customer wait for the owner"),
    )


@template("bank_freshness")
def _bank_gap(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal]:
    missing = inc.refs.get("missing_dates") or []
    what = f"no bank data arrived for {', '.join(missing)}" if missing else "the bank feed stopped updating"
    return (
        IncidentAnalysis(root_cause=f"{what[0].upper()}{what[1:]}, so balances for those days are unknown; "
                                    "guessing them could hide a real payment.", category="data_gap"),
        RuleProposal(rule_text_en="When bank data is missing, ask me before changing the purchasing budget",
                     rule_text_ar="عند نقص بيانات البنك، اسألني قبل تغيير ميزانية الشراء",
                     kind="policy", trigger={"action_type": "publish_budget", "require_approval": True},
                     expected_effect="the budget is not changed on a low-confidence forecast without the owner"),
    )


@template("shortfall_predicted")
def _shortfall(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal]:
    r = inc.refs
    return (
        IncidentAnalysis(root_cause=f"Known payments due around {r.get('gap_date')} are larger than the cash expected "
                                    "by then, so the balance falls below the minimum buffer.", category="external"),
        RuleProposal(rule_text_en="While cash is heading below the buffer, ask me before ordering anything that is not critical",
                     rule_text_ar="ما دام النقد يتجه تحت الحد الأدنى، اسألني قبل طلب أي صنف غير حرج",
                     kind="precondition",
                     trigger={"action_type": "draft_po", "field": "is_critical", "op": "eq", "value": True},
                     expected_effect="non-critical orders wait for the owner; critical items are still ordered"),
    )


def _escalated(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal]:
    action = inc.type.removesuffix(".escalated")
    title = action.replace("_", " ")
    detail = inc.summary.split(":", 1)[-1].strip()
    category = "model_error" if "review disagreed" in detail or "verification failed" in detail else "other"
    return (
        IncidentAnalysis(root_cause=f"'{title}' could not be completed safely: {detail}", category=category),
        RuleProposal(rule_text_en=f"Ask me before every '{title}' until this is fixed",
                     rule_text_ar=f"اسألني قبل كل عملية '{title}' حتى تُحل هذه المشكلة",
                     kind="policy", trigger={"action_type": action, "require_approval": True},
                     expected_effect=f"'{title}' waits for the owner instead of running on its own"),
    )


def _fallback(inc: Incident) -> tuple[IncidentAnalysis, RuleProposal] | None:
    if inc.type in TEMPLATES:
        return TEMPLATES[inc.type](inc)
    if inc.type.endswith(".escalated"):
        return _escalated(inc)
    return None


def _prompt(name: str) -> str:
    p = PROMPTS / name
    return p.read_text(encoding="utf-8") if p.exists() else "Analyse the incident and propose one precise rule."


async def analyse_and_propose(incident_id: uuid.UUID, *, resolve: bool = True) -> uuid.UUID | None:
    """Fill in the root cause and propose a rule. Returns the proposed rule id, if any.

    `resolve=False` (escalations, still with the owner) keeps the incident open as `investigating`.
    """
    async with read_session() as s:
        inc = await s.get(Incident, incident_id)
    if inc is None:
        return None
    fallback = _fallback(inc)
    facts = json.dumps({"type": inc.type, "summary": inc.summary, "detected_by": inc.detected_by, "refs": inc.refs,
                        "action_taken": inc.action_taken}, ensure_ascii=False, default=str)
    llm = get_llm()
    # Both calls only read `facts`, so they run side by side; any expected failure falls back for both.
    got = await asyncio.gather(
        llm.parse("incident", _prompt("incident_analysis.md"), [text_block(facts)], IncidentAnalysis,
                  offline=lambda: fallback[0] if fallback else IncidentAnalysis(root_cause=inc.summary, category="other")),
        llm.parse("incident", _prompt("rule_proposal.md"), [text_block(facts)], RuleProposal,
                  offline=lambda: fallback[1] if fallback else None),  # type: ignore[arg-type,return-value]
        return_exceptions=True,
    )
    errors = [r for r in got if isinstance(r, BaseException)]
    unexpected = [e for e in errors if not isinstance(e, _LLM_FAILURES)]
    if unexpected:
        raise unexpected[0]
    analysis: IncidentAnalysis
    proposal: RuleProposal | None
    if not errors:
        analysis = cast(IncidentAnalysis, got[0])
        proposal = cast(RuleProposal | None, got[1])
    else:
        if fallback is None:
            await _record_cause(inc, IncidentAnalysis(root_cause=inc.summary, category="other"), resolve)
            return None
        analysis, proposal = fallback
    if proposal is not None and not rule_schemas.is_valid(proposal.kind, proposal.trigger):
        proposal = fallback[1] if fallback else None  # the model's trigger is not machine-checkable
    if proposal is None:
        await _record_cause(inc, analysis, resolve)
        return None
    # A template's trigger is authoritative for known incident types (keeps triggers machine-checkable).
    trigger: dict[str, Any] = fallback[1].trigger if fallback and fallback[1].kind == proposal.kind else proposal.trigger
    rid = await rules.propose(business_id=inc.business_id, agent=inc.agent, kind=proposal.kind,
                              rule_text_en=proposal.rule_text_en, rule_text_ar=proposal.rule_text_ar, trigger=trigger,
                              source_incident_id=inc.id)
    await _record_cause(inc, analysis, resolve, "proposed a rule", rule_id=rid)
    return rid


async def _record_cause(inc: Incident, analysis: IncidentAnalysis, resolve: bool, note: str | None = None,
                        rule_id: uuid.UUID | None = None) -> None:
    from app.db.engine import write_session
    from app.harness.incidents import resolve_incident

    taken = inc.action_taken
    if note:
        taken = f"{taken}; {note}" if taken else note
    if rule_id is not None:  # the rule may already exist from an earlier incident with the same cause
        async with write_session() as s:
            row = await s.get(Incident, inc.id)
            if row is not None:
                row.refs = {**row.refs, "rule_id": str(rule_id)}
    if resolve:
        await resolve_incident(inc.id, root_cause=analysis.root_cause, category=analysis.category, action_taken=taken)
        return
    async with write_session() as s:
        row = await s.get(Incident, inc.id)
        if row is not None:
            row.root_cause, row.category, row.action_taken = analysis.root_cause, analysis.category, taken
            if row.status == "open":
                row.status = "investigating"


async def on_escalation(spec: Any, state: Any, incident_id: uuid.UUID) -> None:
    """ESCALATION hook: every escalated action gets a root cause and, where possible, a proposed rule."""
    await analyse_and_propose(incident_id, resolve=False)


def register() -> None:
    from app.harness.graph import ESCALATION_HOOKS

    if on_escalation not in ESCALATION_HOOKS:
        ESCALATION_HOOKS.append(on_escalation)
