"""harness_graph routes (T048): read-only, rollback/retry/escalate, approvals, auto-approve, verifier."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.approvals import service as approvals
from app.core.i18n import option
from app.db.engine import read_session
from app.harness.action_spec import ActionSpec, Check, OwnerAsk, VerifyOutcome, register
from app.harness.graph import run_action
from app.models.harness import Action, ApprovalRequest, AuditLogEntry, Incident

calls: dict[str, int] = {}


def _count(key: str) -> None:
    calls[key] = calls.get(key, 0) + 1


async def _execute(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    _count(f"execute:{inputs.get('tag')}")
    return {"value": inputs.get("value", 1)}


async def _verify_value_ok(ctx: Any, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    return VerifyOutcome(ok=result.get("value") == 1, corrections={"value": 1})


async def _verify_always_fail(ctx: Any, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    return VerifyOutcome(ok=False, details={"why": "read-back mismatch"}, corrections={"value": 1})


async def _compensate(ctx: Any, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    _count(f"compensate:{inputs.get('tag')}")


async def _approval(ctx: Any, inputs: dict[str, Any], plan: dict[str, Any]) -> OwnerAsk:
    _count(f"approval_request:{inputs.get('tag')}")  # in real specs this words the request with the model
    return OwnerAsk(kind="approval", text_en="Send it?", text_ar="هل أرسل؟",
                    options=[option("approve", "opt_approve", "approve"), option("reject", "opt_reject", "reject")])


async def _auto_yes(ctx: Any, inputs: dict[str, Any], settings: dict[str, Any]) -> bool:
    return bool(inputs.get("small"))


async def _packet_with_issue(ctx: Any, inputs: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    from app.harness.verifier import build_packet

    return build_packet("t_send", {"x": 1}, {"y": 2},
                        known_issues=[{"field": "qty", "problem": "unit mismatch", "severity": "high"}])


async def _fail_check(ctx: Any, inputs: dict[str, Any]) -> list[Check]:
    return [Check(name="price_sanity", passed=False, ask=True, reason_en="price up 25%", reason_ar="السعر ارتفع 25%")]


register(ActionSpec(name="t_read", agent="stock", risk_class="read_only", execute=_execute))
register(ActionSpec(name="t_rev", agent="stock", risk_class="reversible", execute=_execute,
                    verify=_verify_value_ok, compensate=_compensate))
register(ActionSpec(name="t_rev_bad", agent="stock", risk_class="reversible", execute=_execute,
                    verify=_verify_always_fail, compensate=_compensate))
register(ActionSpec(name="t_send", agent="stock", risk_class="irreversible_external", execute=_execute,
                    approval_request=_approval, auto_approve=_auto_yes))
register(ActionSpec(name="t_send_verified", agent="stock", risk_class="irreversible_external", execute=_execute,
                    approval_request=_approval, verifier_packet=_packet_with_issue))
register(ActionSpec(name="t_hold", agent="stock", risk_class="reversible", execute=_execute,
                    preconditions=_fail_check))


async def _stage(action_id: uuid.UUID) -> str:
    async with read_session() as s:
        return (await s.get(Action, action_id)).stage  # type: ignore[union-attr]


async def _pending(action_id: uuid.UUID) -> ApprovalRequest:
    async with read_session() as s:
        return (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == action_id,
                                                              ApprovalRequest.status == "pending"))).scalar_one()


async def test_read_only_completes_without_interrupt(business: dict[str, Any]) -> None:
    out = await run_action("t_read", {"tag": "r"}, business["id"])
    assert out["interrupted"] is False
    assert out["outcome"] == "completed"
    assert await _stage(out["action_id"]) == "completed"


async def test_reversible_retry_with_corrected_input_succeeds(business: dict[str, Any]) -> None:
    calls.clear()
    # value=2 fails read-back; the verify step suggests the correction value=1 -> retry succeeds.
    out = await run_action("t_rev", {"tag": "a", "value": 2}, business["id"])
    assert out["outcome"] == "completed"
    assert calls["execute:a"] == 2
    assert calls["compensate:a"] == 1
    assert await _stage(out["action_id"]) == "completed"


async def test_reversible_failing_verify_rolls_back_retries_once_then_escalates(business: dict[str, Any]) -> None:
    calls.clear()
    out = await run_action("t_rev_bad", {"tag": "b"}, business["id"])
    assert out["outcome"] == "escalated"
    assert calls["execute:b"] == 2  # original + one retry
    assert calls["compensate:b"] == 2
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.action_id == out["action_id"]))).scalar_one()
        alert = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.kind == "alert"))).scalar_one()
    assert inc.type == "t_rev_bad.escalated"
    assert alert.status == "pending"
    assert await _stage(out["action_id"]) == "escalated"


async def test_irreversible_pauses_then_approve_executes(business: dict[str, Any], actor: Any) -> None:
    calls.clear()
    out = await run_action("t_send", {"tag": "s"}, business["id"])
    assert out["interrupted"] is True
    assert "execute:s" not in calls
    assert await _stage(out["action_id"]) == "awaiting_approval"
    req = await _pending(out["action_id"])
    res = await approvals.resolve(str(req.id), "approve", actor("manager"), "dashboard")
    assert res.status == "resolved"
    assert calls["execute:s"] == 1
    assert await _stage(out["action_id"]) == "completed"


async def test_irreversible_reject_never_executes(business: dict[str, Any], actor: Any) -> None:
    calls.clear()
    out = await run_action("t_send", {"tag": "x"}, business["id"])
    req = await _pending(out["action_id"])
    await approvals.resolve(req.request_token, "reject", actor("owner"), "telegram")
    assert "execute:x" not in calls
    assert await _stage(out["action_id"]) == "rejected"


async def test_auto_approve_skips_interrupt_and_is_audited(business: dict[str, Any]) -> None:
    calls.clear()
    out = await run_action("t_send", {"tag": "auto", "small": True}, business["id"])
    assert out["interrupted"] is False and out["outcome"] == "completed"
    async with read_session() as s:
        ev = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.action_id == out["action_id"],
                                                          AuditLogEntry.event == "auto_approved"))).scalar_one_or_none()
    assert ev is not None


async def test_verifier_disagreement_escalates_without_retry(business: dict[str, Any]) -> None:
    calls.clear()
    out = await run_action("t_send_verified", {"tag": "v"}, business["id"])
    assert out["outcome"] == "escalated"
    assert "execute:v" not in calls
    async with read_session() as s:
        action = await s.get(Action, out["action_id"])
    assert action is not None and action.verifier_verdict is not None
    assert action.verifier_verdict["agrees"] is False


async def test_precheck_hold_asks_owner_and_continue_executes(business: dict[str, Any], actor: Any) -> None:
    calls.clear()
    out = await run_action("t_hold", {"tag": "h"}, business["id"])
    assert out["interrupted"] is True
    req = await _pending(out["action_id"])
    assert req.kind == "question" and "price up 25%" in req.text_en
    await approvals.resolve(str(req.id), "continue", actor("manager"), "dashboard")
    assert calls["execute:h"] == 1


async def _events(action_id: uuid.UUID) -> list[str]:
    async with read_session() as s:
        return [e.event for e in (await s.execute(select(AuditLogEntry).where(AuditLogEntry.action_id == action_id))).scalars()]


async def test_owner_questions_are_prepared_once_across_pause_and_resume(business: dict[str, Any], actor: Any) -> None:
    """Resuming re-runs the paused node, so the wording (a model call) and the stage audit live before it."""
    calls.clear()
    out = await run_action("t_send", {"tag": "once"}, business["id"])
    await approvals.resolve(str((await _pending(out["action_id"])).id), "approve", actor("manager"), "dashboard")
    assert calls["approval_request:once"] == 1 and calls["execute:once"] == 1
    assert (await _events(out["action_id"])).count("stage:approval_gate") == 1

    held = await run_action("t_hold", {"tag": "held_once"}, business["id"])
    await approvals.resolve(str((await _pending(held["action_id"])).id), "continue", actor("manager"), "dashboard")
    assert calls["execute:held_once"] == 1
    assert (await _events(held["action_id"])).count("stage:hold") == 1


async def test_every_node_writes_audit_and_stage(business: dict[str, Any]) -> None:
    out = await run_action("t_read", {"tag": "audit"}, business["id"])
    async with read_session() as s:
        events = [e.event for e in (await s.execute(select(AuditLogEntry).where(
            AuditLogEntry.action_id == out["action_id"]))).scalars()]
    for expected in ("stage:plan", "stage:precheck", "stage:classify_risk", "stage:execute", "stage:post_verify",
                     "action_finalized"):
        assert expected in events


@pytest.mark.parametrize(
    "options,allow_text,ok",
    [
        (1, False, False),
        (2, False, True),
        (4, False, True),
        (5, False, False),
        (0, True, True),
    ],
)
def test_sc005_requests_are_one_tap_or_short_reply(options: int, allow_text: bool, ok: bool) -> None:
    ask = OwnerAsk(kind="question", text_en="Q?", text_ar="س؟", allow_text=allow_text,
                   options=[option(f"o{i}", "opt_ok", "ack") for i in range(options)])
    if ok:
        approvals.validate_ask(ask)
    else:
        with pytest.raises(approvals.InvalidRequestError):
            approvals.validate_ask(ask)
