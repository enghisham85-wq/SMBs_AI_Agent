"""US4 acceptance (T097): audit, approvals, verifier, rollback/retry/escalate, learned rules, confidence
bands, self-calibration and approval timeouts."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.approvals import service as approvals
from app.core import clock
from app.core.i18n import option
from app.db.engine import read_session, write_session
from app.harness import calibration, confidence, digest, rules
from app.harness.action_spec import ActionSpec, OwnerAsk, VerifyOutcome, register
from app.harness.graph import run_action
from app.models.harness import (
    Action,
    AgentCalibration,
    ApprovalRequest,
    AuditLogEntry,
    Incident,
    LearnedRule,
)
from app.models.master import Supplier

calls: dict[str, int] = {}


def _count(key: str) -> None:
    calls[key] = calls.get(key, 0) + 1


async def _execute(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    _count(f"execute:{inputs.get('tag')}")
    return {"value": inputs.get("value", 1)}


async def _verify_ok(ctx: Any, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    return VerifyOutcome(ok=True, details={"read_back": result.get("value")})


async def _verify_fail(ctx: Any, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    return VerifyOutcome(ok=False, details={"why": "read-back mismatch"}, corrections={"value": 2})


async def _compensate(ctx: Any, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    _count(f"compensate:{inputs.get('tag')}")


async def _plan(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"intent": "test action", "reason": "acceptance test"}


async def _approval(ctx: Any, inputs: dict[str, Any], plan: dict[str, Any]) -> OwnerAsk:
    return OwnerAsk(kind="approval", text_en="Send it?", text_ar="هل أرسل؟",
                    options=[option("approve", "opt_approve", "approve"), option("reject", "opt_reject", "reject")])


async def _auto_small(ctx: Any, inputs: dict[str, Any], settings: dict[str, Any]) -> bool:
    return bool(inputs.get("small"))


async def _disagree(ctx: Any, inputs: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    from app.harness.verifier import build_packet

    return build_packet("t4_send_checked", {"stock": 5}, {"qty": 500},
                        known_issues=[{"field": "qty", "problem": "quantity exceeds storage capacity", "severity": "high"}],
                        irreversible=True)


register(ActionSpec(name="t4_rev", agent="stock", risk_class="reversible", execute=_execute, plan=_plan,
                    verify=_verify_ok, compensate=_compensate, title_en="Test update"))
register(ActionSpec(name="t4_bad", agent="accountant", risk_class="reversible", execute=_execute, plan=_plan,
                    verify=_verify_fail, compensate=_compensate, title_en="Test posting"))
register(ActionSpec(name="t4_cf_bad", agent="cashflow", risk_class="reversible", execute=_execute,
                    verify=_verify_fail, compensate=_compensate))
register(ActionSpec(name="t4_send", agent="stock", risk_class="irreversible_external", execute=_execute,
                    approval_request=_approval, auto_approve=_auto_small, verify=_verify_ok))
register(ActionSpec(name="t4_send_checked", agent="stock", risk_class="irreversible_external", execute=_execute,
                    approval_request=_approval, verifier_packet=_disagree))


@pytest.fixture
async def owner(api: Any, business: dict[str, Any]) -> dict[str, Any]:
    await api.login("owner")
    return business


async def _action(aid: uuid.UUID) -> Action:
    async with read_session() as s:
        a = await s.get(Action, aid)
    assert a is not None
    return a


async def _pending(bid: uuid.UUID, action_id: uuid.UUID | None = None) -> list[ApprovalRequest]:
    async with read_session() as s:
        q = select(ApprovalRequest).where(ApprovalRequest.business_id == bid, ApprovalRequest.status == "pending",
                                          ApprovalRequest.kind != "alert")
        if action_id is not None:
            q = q.where(ApprovalRequest.action_id == action_id)
        return list((await s.execute(q.order_by(ApprovalRequest.created_at))).scalars())


class _Actor:
    def __init__(self, uid: uuid.UUID, role: str) -> None:
        self.id, self.role, self.username = uid, role, role


# ------------------------------------------------------------------ scenario 1
async def test_audit_records_plan_inputs_risk_result_and_verification(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    out = await run_action("t4_rev", {"tag": "audit", "value": 1}, bid)
    assert out["outcome"] == "completed"
    a = await _action(out["action_id"])
    assert a.plan["intent"] == "test action" and a.inputs["tag"] == "audit" and a.risk_class == "reversible"
    assert a.result == {"value": 1}
    async with read_session() as s:
        events = [e.event for e in (await s.execute(select(AuditLogEntry).where(
            AuditLogEntry.action_id == out["action_id"]).order_by(AuditLogEntry.business_time))).scalars()]
    for ev in ("stage:plan", "stage:precheck", "stage:classify_risk", "stage:execute", "stage:post_verify", "action_finalized"):
        assert ev in events
    detail = (await api.client.get(f"/api/v1/harness/actions/{out['action_id']}")).json()
    assert detail["action"]["plan"]["intent"] == "test action"
    assert any(e["event"] == "stage:post_verify" and e["verification"] == "passed" for e in detail["audit"])
    listed = (await api.client.get("/api/v1/harness/actions")).json()["actions"]
    assert any(x["id"] == str(out["action_id"]) and x["stage"] == "completed" for x in listed)
    log = (await api.client.get(f"/api/v1/audit-log?action_id={out['action_id']}")).json()["entries"]
    assert len(log) >= 6


# ------------------------------------------------------------------ scenario 2
async def test_irreversible_action_waits_without_auto_approve_rule(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    calls.clear()
    out = await run_action("t4_send", {"tag": "wait"}, bid)
    assert out["interrupted"] and calls.get("execute:wait") is None
    assert (await _action(out["action_id"])).stage == "awaiting_approval"
    auto = await run_action("t4_send", {"tag": "auto", "small": True}, bid)
    assert auto["outcome"] == "completed" and calls["execute:auto"] == 1


# ------------------------------------------------------------------ scenario 3
async def test_verifier_disagreement_escalates_not_retried(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    calls.clear()
    out = await run_action("t4_send_checked", {"tag": "v"}, bid)
    assert out["outcome"] == "escalated" and calls.get("execute:v") is None
    a = await _action(out["action_id"])
    assert a.verifier_verdict and a.verifier_verdict["agrees"] is False
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.action_id == out["action_id"]))).scalar_one()
    assert inc.detected_by == "verifier" and "storage capacity" in inc.summary


# ------------------------------------------------------------------ scenario 4 and 5
async def test_rollback_retry_escalate_then_rule_changes_next_run(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    calls.clear()
    out = await run_action("t4_bad", {"tag": "rb", "value": 1}, bid)
    assert out["outcome"] == "escalated"
    assert calls["execute:rb"] == 2 and calls["compensate:rb"] == 2  # retried once with corrected input
    a = await _action(out["action_id"])
    assert a.inputs["value"] == 2  # the verifier's correction was used on the retry
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.action_id == out["action_id"]))).scalar_one()
        rule = (await s.execute(select(LearnedRule).where(LearnedRule.source_incident_id == inc.id))).scalar_one()
    assert inc.root_cause and inc.status == "investigating"
    assert rule.status == "proposed" and rule.kind == "policy" and rule.trigger["action_type"] == "t4_bad"
    incidents = (await api.client.get("/api/v1/harness/incidents")).json()["incidents"]
    assert any(i["id"] == str(inc.id) and i["rules"] for i in incidents)

    r = await api.client.post(f"/api/v1/harness/rules/{rule.id}/approve")
    assert r.status_code == 200 and r.json()["status"] == "active"
    calls.clear()
    nxt = await run_action("t4_bad", {"tag": "next", "value": 1}, bid)
    assert nxt["interrupted"] and calls.get("execute:next") is None  # the rule now makes it wait for the owner
    req = (await _pending(bid, nxt["action_id"]))[0]
    assert req.context["learned_rule_id"] == str(rule.id)
    async with read_session() as s:
        assert (await s.get(LearnedRule, rule.id)).times_applied == 1  # type: ignore[union-attr]


async def test_rule_approve_reject_edit_deactivate(api: Any, owner: dict[str, Any], actor: Any) -> None:
    bid = owner["id"]
    trig = {"action_type": "t4_rev", "field": "value", "op": "le", "value": 10}
    rid = await rules.propose(business_id=bid, agent="stock", kind="precondition", rule_text_en="Never update above 10",
                              rule_text_ar="لا تحدّث فوق 10", trigger=trig, source_incident_id=None)
    assert await rules.propose(business_id=bid, agent="stock", kind="precondition", rule_text_en="dup",
                               rule_text_ar="dup", trigger=trig, source_incident_id=None) == rid  # no duplicates
    # manager cannot approve rules
    await api.login("manager")
    assert (await api.client.post(f"/api/v1/harness/rules/{rid}/approve")).status_code == 403
    await api.login("owner")
    assert (await api.client.post(f"/api/v1/harness/rules/{rid}/approve")).status_code == 200
    ok = await run_action("t4_rev", {"tag": "small", "value": 3}, bid)
    assert ok["outcome"] == "completed"
    held = await run_action("t4_rev", {"tag": "big", "value": 50}, bid)
    assert held["interrupted"]
    q = (await _pending(bid, held["action_id"]))[0]
    assert "Never update above 10" in q.text_en
    res = await approvals.resolve(str(q.id), "continue", actor("owner"), "dashboard")
    assert res.status == "resolved"
    assert (await _action(held["action_id"])).stage == "completed"
    async with read_session() as s:
        row = await s.get(LearnedRule, rid)
    assert row is not None and row.times_applied == 2 and row.times_overridden == 1

    # edit -> new active version, old inactive
    r = await api.client.patch(f"/api/v1/harness/rules/{rid}", json={"trigger": {**trig, "value": 100}})
    assert r.status_code == 200 and r.json()["version"] == 2
    new_id = r.json()["id"]
    assert (await run_action("t4_rev", {"tag": "mid", "value": 50}, bid))["outcome"] == "completed"
    bad = await api.client.patch(f"/api/v1/harness/rules/{new_id}", json={"trigger": {"action_type": "t4_rev"}})
    assert bad.status_code == 422
    # deactivate -> no longer applied
    assert (await api.client.post(f"/api/v1/harness/rules/{new_id}/deactivate")).json()["status"] == "inactive"
    assert (await run_action("t4_rev", {"tag": "huge", "value": 500}, bid))["outcome"] == "completed"
    # reject a proposal; an invalid transition is refused
    other = await rules.propose(business_id=bid, agent="stock", kind="policy", rule_text_en="Ask first", rule_text_ar="اسأل",
                                trigger={"action_type": "t4_rev", "require_approval": True}, source_incident_id=None)
    assert (await api.client.post(f"/api/v1/harness/rules/{other}/reject")).json()["status"] == "rejected"
    assert (await api.client.post(f"/api/v1/harness/rules/{other}/approve")).status_code == 409
    listed = {r["id"]: r["status"] for r in (await api.client.get("/api/v1/harness/rules")).json()["rules"]}
    assert listed[str(rid)] == "inactive" and listed[new_id] == "inactive" and listed[str(other)] == "rejected"


async def test_parsing_hint_rule_sets_supplier_date_format(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    async with write_session() as s:
        sup = Supplier(business_id=bid, name_en="Delta Dairy", name_ar="دلتا", stated_lead_time_days=1, payment_terms_days=14)
        s.add(sup)
        await s.flush()
        sid = sup.id
    rid = await rules.propose(business_id=bid, agent="accountant", kind="parsing_hint",
                              rule_text_en="Delta Dairy invoices use DD/MM format; never parse as MM/DD", rule_text_ar="-",
                              trigger={"supplier_id": str(sid), "date_format": "DMY"}, source_incident_id=None)
    await api.client.post(f"/api/v1/harness/rules/{rid}/approve")
    async with read_session() as s:
        assert (await s.get(Supplier, sid)).date_format_hint == "DMY"  # type: ignore[union-attr]
    assert [r.id for r in await rules.active_rules(bid, "accountant", "parsing_hint")] == [rid]
    await api.client.post(f"/api/v1/harness/rules/{rid}/deactivate")
    async with read_session() as s:
        assert (await s.get(Supplier, sid)).date_format_hint is None  # type: ignore[union-attr]
    assert await rules.active_rules(bid, "accountant", "parsing_hint") == []


# ------------------------------------------------------------------ scenario 6
async def test_three_confidence_bands_and_digest(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    assert await confidence.route(bid, "accountant", 0.95, "a", "a") == "act"
    assert await confidence.route(bid, "accountant", 0.75, "Recorded X as rent", "سُجّل", {"line": 1}) == "act_flag"
    assert await confidence.route(bid, "accountant", 0.40, "b", "b") == "ask"
    dg = await digest.send(bid, clock.today())
    assert [f["text_en"] for f in dg["act_flag"]] == ["Recorded X as rent"]
    async with read_session() as s:
        alert = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                               ApprovalRequest.kind == "alert"))).scalars().all()
    assert any("Recorded X as rent" in a.text_en for a in alert)
    # the owner's settings move the bands
    r = await api.client.patch("/api/v1/settings", json={"values": {"confidence_high": 0.7}})
    assert r.status_code == 200
    assert await confidence.band(bid, "accountant", 0.75) == "act"
    assert (await api.client.patch("/api/v1/settings", json={"values": {"confidence_low": 0.8}})).status_code == 422
    async with read_session() as s:
        changed = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.event == "setting_changed"))).scalars().all()
    assert changed and changed[0].outputs["previous"]["confidence_high"] == 0.9


# ------------------------------------------------------------------ scenario 7
async def test_calibration_degrades_and_restores_in_four_healthy_days(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    d = clock.today()
    st = await calibration.record(bid, "cashflow", "forecast_variance", 0.30, d, 0.10, safe_method="pessimistic")
    assert st["state"] == "degraded" and st["auto_approve_factor"] == 0.0 and st["method_override"] == "pessimistic"
    assert await calibration.auto_approve_factor(bid, "cashflow") == 0.0
    high, low = await confidence.thresholds(bid, "cashflow")
    assert high > 0.9 and low > 0.6
    assert await confidence.band(bid, "cashflow", 0.93) == "act_flag"  # would have been "act"
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.type == "calibration_degraded"))).scalar_one()
    assert "Suspected cause" in inc.summary
    # a second value on the same day does not count as a recovery step
    assert (await calibration.record(bid, "cashflow", "forecast_variance", 0.05, d, 0.10))["state"] == "degraded"
    factors = []
    for i in range(1, 3):
        st = await calibration.record(bid, "cashflow", "forecast_variance", 0.05, d + timedelta(days=i), 0.10)
        factors.append(st["auto_approve_factor"])
    assert factors == [0.25, 0.5] and st["state"] == "recovering"
    # an unhealthy day restarts the count from the current, partly restored values
    st = await calibration.record(bid, "cashflow", "forecast_variance", 0.30, d + timedelta(days=3), 0.10)
    assert st["state"] == "still_degraded" and st["auto_approve_factor"] == 0.5
    states = []
    for i in range(4, 6):
        states.append((await calibration.record(bid, "cashflow", "forecast_variance", 0.05, d + timedelta(days=i), 0.10))["state"])
    assert states == ["recovering", "restored"]
    assert await calibration.auto_approve_factor(bid, "cashflow") == 1.0
    assert await confidence.band(bid, "cashflow", 0.93) == "act"
    body = (await api.client.get("/api/v1/harness/calibration")).json()
    cf = next(a for a in body["agents"] if a["agent"] == "cashflow")
    assert cf["degraded"] is False and len([h for h in body["history"] if h["agent"] == "cashflow"]) == 6


async def test_failure_rate_degrades_agent_the_same_day(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    for i in range(calibration.MIN_ACTIONS):
        await run_action("t4_cf_bad", {"tag": f"cf{i}"}, bid)
    async with read_session() as s:
        row = (await s.execute(select(AgentCalibration).where(AgentCalibration.business_id == bid, AgentCalibration.agent == "cashflow",
                                                              AgentCalibration.metric == "action_failure_rate"))).scalar_one()
    assert row.degraded and row.auto_approve_factor == 0.0


# ------------------------------------------------------------------ scenario 8
async def test_timeout_reasks_with_higher_urgency_and_never_executes(api: Any, owner: dict[str, Any]) -> None:
    bid = owner["id"]
    calls.clear()
    out = await run_action("t4_send", {"tag": "late"}, bid)
    first = (await _pending(bid, out["action_id"]))[0]
    assert first.urgency == 1 and first.reask_count == 0
    standalone = await approvals.post_question(
        business_id=bid, agent="cashflow", key="obligation:test",
        ask=OwnerAsk(kind="question", text_en="Is rent still due?", text_ar="هل الإيجار مستحق؟", safe_default=None,
                     options=[{"key": "yes", "label_en": "Yes", "label_ar": "نعم", "effect": "ack"},
                              {"key": "no", "label_en": "No", "label_ar": "لا", "effect": "ack"}]))
    clock.set_state("simulated", clock.today() + timedelta(days=1))
    expired = await digest.step_approval_timeouts(bid, clock.today())
    assert expired == 2
    async with read_session() as s:
        old = await s.get(ApprovalRequest, first.id)
        old_q = await s.get(ApprovalRequest, standalone)
    assert old is not None and old.status == "timed_out" and old_q is not None and old_q.status == "timed_out"
    again = await _pending(bid, out["action_id"])
    assert len(again) == 1 and again[0].urgency == 2 and again[0].reask_count == 1
    copies = [r for r in await _pending(bid) if r.text_en == "Is rent still due?"]
    assert len(copies) == 1 and copies[0].urgency == 2 and copies[0].reask_count == 1
    assert calls.get("execute:late") is None and (await _action(out["action_id"])).stage == "awaiting_approval"
    # a safe default can never approve
    with pytest.raises(approvals.InvalidRequestError):
        approvals.validate_ask(OwnerAsk(kind="approval", text_en="x", text_ar="x", safe_default="approve",
                                        options=[option("approve", "opt_approve", "approve"),
                                                 option("reject", "opt_reject", "reject")]))


async def test_audit_log_is_owner_only(api: Any, owner: dict[str, Any]) -> None:
    await api.login("manager")
    assert (await api.client.get("/api/v1/audit-log")).status_code == 403
    assert (await api.client.get("/api/v1/harness/rules")).status_code == 200
    assert (await api.client.patch("/api/v1/settings", json={"values": {"price_change_pct": 20}})).status_code == 403
