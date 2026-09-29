"""harness_graph: the lifecycle every agent action goes through (FR-001 ... FR-009, research R12/R17).

plan -> precheck -> [hold] -> classify_risk -> premortem_verify -> approval_gate -> execute
     -> post_verify -> (finalize | rollback -> retry -> execute ... -> escalate) -> finalize

Every node updates Action.stage and writes an audit entry. Irreversible/external actions pause at
`approval_gate` (LangGraph interrupt) unless the spec's own auto_approve rule allows them.
Reversible writes are undone by the spec's `compensate` step when read-back verification fails.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.approvals import service as approvals
from app.core import settings_store
from app.core.i18n import AGENT_NAMES, both, option
from app.db.engine import write_session
from app.graphs.streaming import broker
from app.harness.action_spec import ActionContext, ActionSpec, Check, OwnerAsk, VerifyOutcome, get_spec
from app.harness.audit import add_audit, jsonable
from app.harness.incidents import open_incident
from app.harness.state import ActionState
from app.harness.verifier import verify_independently
from app.models.harness import Action, CheckResult

log = logging.getLogger(__name__)
GRAPH = "harness"

# Extension points filled by US4 (learned rules, calibration, incident analysis).
PrecheckHook = Callable[[ActionSpec, ActionContext, dict[str, Any]], Awaitable[list[Check]]]
StateHook = Callable[[ActionSpec, ActionState], Awaitable[None]]
PRECHECK_HOOKS: list[PrecheckHook] = []
FINALIZE_HOOKS: list[StateHook] = []
ESCALATION_HOOKS: list[Callable[[ActionSpec, ActionState, uuid.UUID], Awaitable[None]]] = []


def _ctx(state: ActionState) -> ActionContext:
    return ActionContext(
        business_id=uuid.UUID(state["business_id"]),
        action_id=uuid.UUID(state["action_id"]),
        attempt=state.get("attempt", 1),
        dry_run=state.get("dry_run", False),
    )


def _spec(state: ActionState) -> ActionSpec:
    return get_spec(state["spec_name"])


async def _stage(state: ActionState, stage: str, event: str, outputs: dict[str, Any] | None = None,
                 verification: str | None = None, **fields: Any) -> None:
    spec = _spec(state)
    async with write_session() as s:
        action = await s.get(Action, uuid.UUID(state["action_id"]))
        if action is not None:
            action.stage = stage
            for k, v in fields.items():
                setattr(action, k, v if isinstance(v, uuid.UUID | int | str) else jsonable(v))
        add_audit(s, event, business_id=uuid.UUID(state["business_id"]), agent=spec.agent,
                  action_id=uuid.UUID(state["action_id"]), inputs={"spec": spec.name, "attempt": state.get("attempt", 1)},
                  outputs=outputs or {}, verification_result=verification)
    # Live pipeline view (Harness page): one message per stage.
    broker.publish("harness", {"kind": "stage", "business_id": state["business_id"], "action_id": state["action_id"],
                               "agent": spec.agent, "type": spec.name, "stage": stage, "event": event,
                               "attempt": state.get("attempt", 1), "verification": verification})


def _title(spec: ActionSpec) -> tuple[str, str]:
    return spec.title_en or spec.name.replace("_", " "), spec.title_ar or spec.title_en or spec.name


# ---------------------------------------------------------------- nodes
async def plan_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    plan = await spec.plan(_ctx(state), state["inputs"]) if spec.plan else {"intent": spec.name}
    await _stage(state, "planned", "stage:plan", {"plan": plan}, plan=plan)
    return {"plan": jsonable(plan), "attempt": state.get("attempt", 1), "reask": state.get("reask", 0)}


async def precheck_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    ctx = _ctx(state)
    checks: list[Check] = list(await spec.preconditions(ctx, state["inputs"])) if spec.preconditions else []
    for hook in PRECHECK_HOOKS:
        checks.extend(await hook(spec, ctx, state["inputs"]))
    results = [
        {"name": c.name, "passed": c.passed, "details": jsonable(c.details), "ask": c.ask, "cancel": c.cancel,
         "reason_en": c.reason_en, "reason_ar": c.reason_ar, "learned_rule_id": c.learned_rule_id}
        for c in checks
    ]
    async with write_session() as s:
        for c in checks:
            s.add(CheckResult(business_id=ctx.business_id, action_id=ctx.action_id, check_name=c.name,
                              passed=c.passed, details=jsonable(c.details),
                              learned_rule_id=uuid.UUID(c.learned_rule_id) if c.learned_rule_id else None))
    failed = [r for r in results if not r["passed"]]
    if not failed:
        route = "classify_risk"
    elif any(r["cancel"] for r in failed):
        route = "finalize"
    elif all(r["ask"] for r in failed):
        route = "hold"
    else:
        route = "escalate"
    await _stage(state, "prechecked", "stage:precheck", {"checks": results, "route": route},
                 verification="passed" if not failed else "failed")
    reason = "; ".join(r["reason_en"] or r["name"] for r in failed) or None
    if route == "finalize":
        return {"precheck_results": results, "route": route, "reason": reason, "outcome": "cancelled"}
    return {"precheck_results": results, "route": route, "reason": reason}


async def hold_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    ctx = _ctx(state)
    failed = [r for r in state.get("precheck_results", []) if not r["passed"]]
    custom: OwnerAsk | None = await spec.on_hold(ctx, state["inputs"], failed) if spec.on_hold else None
    title_en, title_ar = _title(spec)
    reason_en = "; ".join(r["reason_en"] or r["name"] for r in failed)
    reason_ar = "؛ ".join(r["reason_ar"] or r["reason_en"] or r["name"] for r in failed)
    ask = custom or OwnerAsk(
        kind="question",
        text_en=both("precheck_hold", action=title_en, reason=reason_en)[0],
        text_ar=both("precheck_hold", action=title_ar, reason=reason_ar)[1],
        options=[option("continue", "opt_continue", "override"), option("cancel", "opt_cancel", "cancel")],
    )
    reask = state.get("reask", 0)
    ask.urgency = min(3, ask.urgency + reask)
    ask.reask = reask
    await _stage(state, "awaiting_approval", "stage:hold", {"failed": failed})
    answer = await approvals.ask_owner(
        business_id=ctx.business_id, graph_name=GRAPH, thread_id=state["action_id"],
        gate_key=f"hold:{state.get('attempt', 1)}:{reask}", ask=ask, action_id=ctx.action_id, agent=spec.agent,
    )
    effect = answer.get("effect")
    if effect == "reask":
        return {"route": "hold", "reask": reask + 1, "approval": answer}
    if effect in ("override", "approve", "continue"):
        from app.harness import rules

        for r in failed:
            if r.get("learned_rule_id"):
                await rules.mark_overridden(uuid.UUID(r["learned_rule_id"]))
        return {"route": "classify_risk", "approval": answer}
    if effect == "edit" and spec.on_option:
        out = await spec.on_option(ctx, state["inputs"], answer.get("option_key"), answer.get("edits") or {})
        return {"route": out.get("next", "precheck"), "inputs": out.get("inputs", state["inputs"]), "approval": answer}
    if spec.on_option and effect not in ("cancel", "reject"):
        out = await spec.on_option(ctx, state["inputs"], answer.get("option_key"), answer.get("edits") or {})
        return {"route": out.get("next", "finalize"), "inputs": out.get("inputs", state["inputs"]),
                "approval": answer, "outcome": out.get("outcome", "cancelled")}
    return {"route": "finalize", "outcome": "cancelled", "approval": answer}


async def classify_risk_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    route = "execute" if spec.risk_class == "read_only" else "premortem_verify"
    await _stage(state, "prechecked", "stage:classify_risk", {"risk_class": spec.risk_class})
    return {"risk_class": spec.risk_class, "route": route}


async def premortem_verify_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    if spec.verifier_packet is None:
        return {"route": "approval_gate", "verifier_verdict": None}
    packet = await spec.verifier_packet(_ctx(state), state["inputs"], state.get("plan", {}))
    if not packet:
        return {"route": "approval_gate", "verifier_verdict": None}
    verdict = (await verify_independently(packet)).model_dump(mode="json")
    high = any(i["severity"] == "high" for i in verdict["issues"])
    route = "approval_gate" if verdict["agrees"] and not high else "escalate"
    await _stage(state, "prechecked", "stage:premortem_verify", {"verdict": verdict},
                 verification="agreed" if route == "approval_gate" else "disagreed", verifier_verdict=verdict)
    reason = None
    if route == "escalate":
        reason = "independent review disagreed: " + "; ".join(i["problem"] for i in verdict["issues"])
    return {"verifier_verdict": verdict, "route": route, "reason": reason}


async def approval_gate_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    ctx = _ctx(state)
    from app.harness import rules

    policy = await rules.requires_approval(ctx.business_id, spec.name)  # an owner-approved learned rule
    if spec.risk_class != "irreversible_external" and policy is None:
        return {"route": "execute"}
    if policy is not None:
        await rules.mark_applied(policy)
    settings = await settings_store.get_all(ctx.business_id)
    if policy is None and spec.auto_approve is not None and await spec.auto_approve(ctx, state["inputs"], settings):
        await _stage(state, "executing", "auto_approved", {"rule": f"{spec.name}.auto_approve"})
        return {"route": "execute", "approval": {"effect": "approve", "via": "auto_approve"}}

    if spec.approval_request is not None:
        ask: OwnerAsk = await spec.approval_request(ctx, state["inputs"], state.get("plan", {}))
    else:
        title_en, title_ar = _title(spec)
        summary = str(state.get("plan", {}).get("summary", ""))
        ask = OwnerAsk(
            kind="approval",
            text_en=both("approval_generic", action=title_en, summary=summary)[0],
            text_ar=both("approval_generic", action=title_ar, summary=summary)[1],
            options=[option("approve", "opt_approve", "approve"), option("reject", "opt_reject", "reject")],
        )
    reask = state.get("reask", 0)
    ask.urgency = min(3, ask.urgency + reask)
    ask.reask = reask
    if policy is not None:
        ask.context = {**ask.context, "learned_rule_id": str(policy)}
    await _stage(state, "awaiting_approval", "stage:approval_gate", {"reask": reask, "learned_rule_id": policy})
    answer = await approvals.ask_owner(
        business_id=ctx.business_id, graph_name=GRAPH, thread_id=state["action_id"],
        gate_key=f"approval:{state.get('attempt', 1)}:{reask}", ask=ask, action_id=ctx.action_id, agent=spec.agent,
    )
    effect = answer.get("effect")
    if effect == "approve":
        return {"route": "execute", "approval": answer}
    if effect == "reask":
        return {"route": "approval_gate", "reask": reask + 1, "approval": answer}
    if effect == "reject":
        return {"route": "finalize", "outcome": "rejected", "approval": answer}
    if spec.on_option is not None:
        out = await spec.on_option(ctx, state["inputs"], answer.get("option_key"), answer.get("edits") or {})
        return {"route": out.get("next", "precheck"), "inputs": out.get("inputs", state["inputs"]),
                "approval": answer, "outcome": out.get("outcome")}
    return {"route": "finalize", "outcome": "rejected", "approval": answer}


async def execute_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    await _stage(state, "executing", "stage:execute", {"attempt": state.get("attempt", 1)})
    try:
        result = await spec.execute(_ctx(state), state["inputs"])
    except Exception as exc:  # execution errors are handled like failed verification
        log.exception("action %s failed", spec.name)
        result = {"error": repr(exc)}
    return {"execution_result": jsonable(result or {})}


async def post_verify_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    result = state.get("execution_result") or {}
    if "error" in result:
        outcome = VerifyOutcome(ok=False, details={"error": result["error"]})
    elif spec.verify is not None:
        outcome = await spec.verify(_ctx(state), state["inputs"], result)
    else:
        outcome = VerifyOutcome(ok=True)
    details = jsonable(outcome.details)
    await _stage(state, "verifying", "stage:post_verify", {"ok": outcome.ok, "details": details},
                 verification="passed" if outcome.ok else "failed", result=result)
    return {
        "verification_result": {"ok": outcome.ok, "details": details},
        "corrections": jsonable(outcome.corrections) if outcome.corrections else None,
        "route": "finalize" if outcome.ok else "rollback",
        "outcome": "completed" if outcome.ok else None,
        "reason": None if outcome.ok else f"verification failed: {details}",
    }


async def rollback_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    if spec.risk_class == "irreversible_external":
        await _stage(state, "failed", "stage:rollback", {"note": "irreversible action; cannot be undone"})
        return {"route": "escalate"}
    if spec.compensate is not None:
        try:
            await spec.compensate(_ctx(state), state["inputs"], state.get("execution_result") or {})
        except Exception:
            log.exception("compensate failed for %s", spec.name)
    await _stage(state, "rolled_back", "stage:rollback", {"attempt": state.get("attempt", 1)})
    return {"route": "retry" if state.get("attempt", 1) < 2 else "escalate"}


async def retry_node(state: ActionState) -> dict[str, Any]:
    inputs = dict(state["inputs"])
    inputs.update(state.get("corrections") or {})
    await _stage(state, "retrying", "stage:retry", {"corrections": state.get("corrections")}, attempt=2, inputs=inputs)
    return {"attempt": 2, "inputs": inputs}


async def escalate_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    ctx = _ctx(state)
    title_en, title_ar = _title(spec)
    reason = state.get("reason") or "unknown failure"
    incident_id = await open_incident(
        business_id=ctx.business_id, agent=spec.agent, type=f"{spec.name}.escalated",
        detected_by="verifier" if state.get("verifier_verdict") and not state["verifier_verdict"].get("agrees") else "harness",
        summary=f"{title_en}: {reason}", refs={"action_id": state["action_id"], "inputs": state["inputs"]},
        action_id=ctx.action_id, action_taken="stopped and escalated to the owner",
    )
    text_en = both("escalated", agent=AGENT_NAMES[spec.agent]["en"], action=title_en, reason=reason)[0]
    text_ar = both("escalated", agent=AGENT_NAMES[spec.agent]["ar"], action=title_ar, reason=reason)[1]
    await approvals.post_alert(business_id=ctx.business_id, agent=spec.agent, text_en=text_en[:1000],
                               text_ar=text_ar[:1000], action_id=ctx.action_id, urgency=2,
                               context={"incident_id": str(incident_id)})
    await _stage(state, "escalated", "stage:escalate", {"reason": reason, "incident_id": incident_id},
                 incident_id=incident_id)
    for hook in ESCALATION_HOOKS:
        try:
            await hook(spec, state, incident_id)
        except Exception:
            log.exception("escalation hook failed")
    return {"outcome": "escalated"}


async def finalize_node(state: ActionState) -> dict[str, Any]:
    spec = _spec(state)
    outcome = state.get("outcome") or "completed"
    stage = {"completed": "completed", "rejected": "rejected", "cancelled": "rejected", "escalated": "escalated"}.get(
        outcome, "completed"
    )
    await _stage(state, stage, "action_finalized", {"outcome": outcome})
    if spec.on_finalize is not None:
        try:
            await spec.on_finalize(_ctx(state), state["inputs"], outcome, state.get("execution_result") or {})
        except Exception:
            log.exception("on_finalize failed for %s", spec.name)
    for hook in FINALIZE_HOOKS:
        try:
            await hook(spec, {**state, "outcome": outcome})  # type: ignore[typeddict-item]
        except Exception:
            log.exception("finalize hook failed")
    return {"outcome": outcome}


def _route(state: ActionState) -> str:
    return state["route"]


def build() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(ActionState)
    g.add_node("plan", plan_node)
    g.add_node("precheck", precheck_node)
    g.add_node("hold", hold_node)
    g.add_node("classify_risk", classify_risk_node)
    g.add_node("premortem_verify", premortem_verify_node)
    g.add_node("approval_gate", approval_gate_node)
    g.add_node("execute", execute_node)
    g.add_node("post_verify", post_verify_node)
    g.add_node("rollback", rollback_node)
    g.add_node("retry", retry_node)
    g.add_node("escalate", escalate_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "precheck")
    g.add_conditional_edges("precheck", _route, ["hold", "escalate", "classify_risk", "finalize"])
    g.add_conditional_edges("hold", _route, ["hold", "classify_risk", "precheck", "execute", "finalize"])
    g.add_conditional_edges("classify_risk", _route, ["premortem_verify", "execute"])
    g.add_conditional_edges("premortem_verify", _route, ["approval_gate", "escalate"])
    g.add_conditional_edges("approval_gate", _route, ["execute", "approval_gate", "finalize", "precheck"])
    g.add_edge("execute", "post_verify")
    g.add_conditional_edges("post_verify", _route, ["finalize", "rollback"])
    g.add_conditional_edges("rollback", _route, ["retry", "escalate"])
    g.add_edge("retry", "execute")
    g.add_edge("escalate", "finalize")
    g.add_edge("finalize", END)
    return g


async def run_action(
    spec_name: str,
    inputs: dict[str, Any],
    business_id: uuid.UUID,
    *,
    parent_action_id: uuid.UUID | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create the Action record and run it through harness_graph.

    Returns {"action_id", "outcome", "interrupted", "result"}; `interrupted` means it is waiting
    for the owner (approval or hold question).
    """
    from app.graphs import runtime

    spec = get_spec(spec_name)
    action_id = uuid.uuid4()
    async with write_session() as s:
        s.add(Action(id=action_id, business_id=business_id, agent=spec.agent, type=spec.name, graph_name=GRAPH,
                     graph_thread_id=str(action_id), inputs=jsonable(inputs), risk_class=spec.risk_class,
                     parent_action_id=parent_action_id, dry_run=dry_run))
    state: ActionState = {
        "action_id": str(action_id), "business_id": str(business_id), "spec_name": spec_name,
        "inputs": jsonable(inputs), "attempt": 1, "reask": 0, "dry_run": dry_run,
    }
    run = await runtime.start(GRAPH, dict(state), str(action_id))
    values = run["values"]
    return {
        "action_id": action_id,
        "outcome": None if run["interrupted"] else values.get("outcome"),
        "interrupted": run["interrupted"],
        "result": values.get("execution_result"),
        "state": values,
    }


def register() -> None:
    from app.graphs import runtime

    runtime.register(GRAPH, build)
