"""Incident analysis -> root cause and a proposed learned rule (FR-007, FR-008).

Uses Claude (IncidentAnalysis, RuleProposal) with deterministic templates offline. Per-type templates
live in TEMPLATES; US4 extends coverage to every escalation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.db.engine import read_session
from app.harness import rules
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


def _prompt(name: str) -> str:
    p = PROMPTS / name
    return p.read_text(encoding="utf-8") if p.exists() else "Analyse the incident and propose one precise rule."


async def analyse_and_propose(incident_id: uuid.UUID) -> uuid.UUID | None:
    """Fill in the root cause and propose a rule. Returns the proposed rule id, if any."""
    from app.harness.incidents import resolve_incident

    async with read_session() as s:
        inc = await s.get(Incident, incident_id)
    if inc is None:
        return None
    fallback = TEMPLATES.get(inc.type, lambda _i: None)(inc)
    facts = json.dumps({"type": inc.type, "summary": inc.summary, "detected_by": inc.detected_by, "refs": inc.refs,
                        "action_taken": inc.action_taken}, ensure_ascii=False, default=str)
    llm = get_llm()
    try:
        analysis = await llm.parse("incident", _prompt("incident_analysis.md"), [text_block(facts)], IncidentAnalysis,
                                   offline=lambda: fallback[0] if fallback else IncidentAnalysis(root_cause=inc.summary, category="other"))
        proposal = await llm.parse("incident", _prompt("rule_proposal.md"), [text_block(facts)], RuleProposal,
                                   offline=lambda: fallback[1] if fallback else None)  # type: ignore[arg-type,return-value]
    except (LLMRefusalError, LLMUnavailableError, MissingFixtureError, AttributeError):
        if fallback is None:
            return None
        analysis, proposal = fallback
    if proposal is None:
        return None
    # A template's trigger is authoritative for known incident types (keeps triggers machine-checkable).
    trigger: dict[str, Any] = fallback[1].trigger if fallback else proposal.trigger
    rid = await rules.propose(business_id=inc.business_id, agent=inc.agent, kind=proposal.kind,
                              rule_text_en=proposal.rule_text_en, rule_text_ar=proposal.rule_text_ar, trigger=trigger,
                              source_incident_id=inc.id)
    await resolve_incident(inc.id, root_cause=analysis.root_cause, category=analysis.category,
                           action_taken=(inc.action_taken + "; " if inc.action_taken else "") + "proposed a rule")
    return rid
