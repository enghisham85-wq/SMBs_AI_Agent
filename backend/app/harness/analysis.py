"""Incident analysis -> root cause and a proposed learned rule (FR-007, FR-008).

Uses Claude (IncidentAnalysis, RuleProposal) with deterministic templates offline. Per-type templates
live in TEMPLATES; every escalated action (`<action>.escalated`) has a generic one. A proposed
trigger must validate against its kind's schema (rule_schemas), else the template's is used.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.db.engine import read_session
from app.harness import rule_schemas, rules
from app.llm.client import LLMRefusalError, LLMUnavailableError, MissingFixtureError, get_llm, text_block
from app.llm.schemas import IncidentAnalysis, RuleProposal
from app.models.harness import Incident

PROMPTS = Path(__file__).resolve().parent.parent / "llm" / "prompts"
Template = Callable[[Incident], tuple[IncidentAnalysis, RuleProposal] | None]
TEMPLATES: dict[str, Template] = {}


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
    try:
        analysis = await llm.parse("incident", _prompt("incident_analysis.md"), [text_block(facts)], IncidentAnalysis,
                                   offline=lambda: fallback[0] if fallback else IncidentAnalysis(root_cause=inc.summary, category="other"))
        proposal: RuleProposal | None = await llm.parse("incident", _prompt("rule_proposal.md"), [text_block(facts)], RuleProposal,
                                   offline=lambda: fallback[1] if fallback else None)  # type: ignore[arg-type,return-value]
    except (LLMRefusalError, LLMUnavailableError, MissingFixtureError, AttributeError):
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
    await _record_cause(inc, analysis, resolve, "proposed a rule")
    return rid


async def _record_cause(inc: Incident, analysis: IncidentAnalysis, resolve: bool, note: str | None = None) -> None:
    from app.db.engine import write_session
    from app.harness.incidents import resolve_incident

    taken = inc.action_taken
    if note:
        taken = f"{taken}; {note}" if taken else note
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
