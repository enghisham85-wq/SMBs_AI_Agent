"""Learned rules (FR-008): proposed from incidents, approved/edited/rejected/deactivated by the owner,
and applied by code as checks on later actions.

Kinds and how they apply:
- precondition / check: compare a field of an action's inputs; a failure holds the action and asks the owner
- parsing_hint: extraction hints; a supplier date format also sets Supplier.date_format_hint (date_sanity)
- classification: expense account for a supplier (Accountant classification)
- policy: `require_approval` turns off auto-approval for an action type
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.core.clock import clock_now
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.graphs.streaming import broker
from app.harness import rule_schemas
from app.harness.action_spec import ActionContext, ActionSpec, Check
from app.harness.audit import add_audit
from app.models.harness import LearnedRule

APPLIED_KINDS = ("precondition", "check")

# business_id -> active rules (detached snapshots); refreshed on rule.activated / rule.deactivated.
_cache: dict[uuid.UUID, list[LearnedRule]] = {}


class RuleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def invalidate(business_id: uuid.UUID | None = None) -> None:
    if business_id is None:
        _cache.clear()
    else:
        _cache.pop(business_id, None)


async def _on_rule_event(env: dict[str, Any]) -> None:
    invalidate(uuid.UUID(env["business_id"]))


# ------------------------------------------------------------------ lifecycle
async def propose(*, business_id: uuid.UUID, agent: str, kind: str, rule_text_en: str, rule_text_ar: str,
                  trigger: dict[str, Any], source_incident_id: uuid.UUID | None) -> uuid.UUID:
    """Create a `proposed` rule. The same kind + trigger already proposed or active is not proposed again."""
    rule_schemas.validate(kind, trigger)
    async with write_session() as s:
        same = (await s.execute(select(LearnedRule).where(LearnedRule.business_id == business_id, LearnedRule.kind == kind,
                                                          LearnedRule.status.in_(("proposed", "active"))))).scalars().all()
        existing = next((r for r in same if r.trigger == trigger), None)
        if existing is not None:
            return existing.id
        rule = LearnedRule(business_id=business_id, agent=agent, kind=kind, rule_text_en=rule_text_en,
                           rule_text_ar=rule_text_ar, trigger=trigger, source_incident_id=source_incident_id,
                           proposed_by=agent, status="proposed", status_changed_at=clock_now())
        s.add(rule)
        await s.flush()
        rid = rule.id
        add_audit(s, "rule_proposed", business_id=business_id, agent=agent,
                  inputs={"rule_id": rid, "kind": kind, "trigger": trigger, "text": rule_text_en})
    broker.publish("harness", {"kind": "rule.proposed", "rule_id": rid, "text": rule_text_en,
                               "business_id": str(business_id)})
    return rid


async def _get(s: Any, rule_id: uuid.UUID, business_id: uuid.UUID) -> LearnedRule:
    rule: LearnedRule | None = await s.get(LearnedRule, rule_id)
    if rule is None or rule.business_id != business_id:
        raise RuleError("not_found", "rule not found")
    return rule


async def _side_effects(s: Any, rule: LearnedRule, active: bool) -> None:
    """Rules that live as data elsewhere: a supplier's date format feeds date_sanity."""
    if rule.kind == "parsing_hint" and rule.trigger.get("date_format"):
        from app.models.master import Supplier

        try:
            sup = await s.get(Supplier, uuid.UUID(str(rule.trigger["supplier_id"])))
        except ValueError:
            sup = None
        if sup is not None:
            sup.date_format_hint = rule.trigger["date_format"] if active else None


def _event(s: Any, rule: LearnedRule, activated: bool) -> None:
    publish(s, "rule.activated" if activated else "rule.deactivated",
            {"rule_id": rule.id, "agent": rule.agent, "kind": rule.kind, "trigger": rule.trigger},
            producer="harness", business_id=rule.business_id)


async def approve(rule_id: uuid.UUID, business_id: uuid.UUID, user_id: uuid.UUID) -> LearnedRule:
    async with write_session() as s:
        rule = await _get(s, rule_id, business_id)
        if rule.status != "proposed":
            raise RuleError("invalid_state", f"only a proposed rule can be approved (this one is {rule.status})")
        rule.status, rule.approved_by, rule.status_changed_at = "active", user_id, clock_now()
        await _side_effects(s, rule, True)
        _event(s, rule, True)
        add_audit(s, "rule_approved", business_id=business_id, user_id=user_id, inputs={"rule_id": rule_id})
    invalidate(business_id)
    return rule


async def reject(rule_id: uuid.UUID, business_id: uuid.UUID, user_id: uuid.UUID) -> LearnedRule:
    async with write_session() as s:
        rule = await _get(s, rule_id, business_id)
        if rule.status != "proposed":
            raise RuleError("invalid_state", f"only a proposed rule can be rejected (this one is {rule.status})")
        rule.status, rule.status_changed_at = "rejected", clock_now()
        add_audit(s, "rule_rejected", business_id=business_id, user_id=user_id, inputs={"rule_id": rule_id})
    return rule


async def deactivate(rule_id: uuid.UUID, business_id: uuid.UUID, user_id: uuid.UUID) -> LearnedRule:
    async with write_session() as s:
        rule = await _get(s, rule_id, business_id)
        if rule.status != "active":
            raise RuleError("invalid_state", f"only an active rule can be deactivated (this one is {rule.status})")
        rule.status, rule.status_changed_at = "inactive", clock_now()
        await _side_effects(s, rule, False)
        _event(s, rule, False)
        add_audit(s, "rule_deactivated", business_id=business_id, user_id=user_id, inputs={"rule_id": rule_id})
    invalidate(business_id)
    return rule


async def edit(rule_id: uuid.UUID, business_id: uuid.UUID, user_id: uuid.UUID, *, rule_text_en: str | None = None,
               rule_text_ar: str | None = None, trigger: dict[str, Any] | None = None) -> LearnedRule:
    """The owner's edit is a new, active version; the previous version becomes inactive."""
    async with write_session() as s:
        old = await _get(s, rule_id, business_id)
        if old.status not in ("proposed", "active"):
            raise RuleError("invalid_state", f"a {old.status} rule cannot be edited")
        new_trigger = trigger if trigger is not None else old.trigger
        try:
            rule_schemas.validate(old.kind, new_trigger)
        except rule_schemas.InvalidTriggerError as exc:
            raise RuleError("invalid_trigger", str(exc)) from exc
        was_active = old.status == "active"
        if was_active:
            await _side_effects(s, old, False)
            _event(s, old, False)
        old.status, old.status_changed_at = "inactive", clock_now()
        new = LearnedRule(business_id=business_id, agent=old.agent, kind=old.kind,
                          rule_text_en=rule_text_en or old.rule_text_en, rule_text_ar=rule_text_ar or old.rule_text_ar,
                          trigger=new_trigger, source_incident_id=old.source_incident_id, proposed_by=old.proposed_by,
                          approved_by=user_id, status="active", version=old.version + 1, previous_version_id=old.id,
                          status_changed_at=clock_now())
        s.add(new)
        await s.flush()
        await _side_effects(s, new, True)
        _event(s, new, True)
        add_audit(s, "rule_edited", business_id=business_id, user_id=user_id,
                  inputs={"rule_id": rule_id, "new_rule_id": new.id, "trigger": new_trigger})
    invalidate(business_id)
    return new


# ------------------------------------------------------------------ lookup
async def active_rules(business_id: uuid.UUID, agent: str | None = None, kind: str | None = None) -> list[LearnedRule]:
    if business_id not in _cache:
        async with read_session() as s:
            _cache[business_id] = list((await s.execute(select(LearnedRule).where(
                LearnedRule.business_id == business_id, LearnedRule.status == "active"))).scalars())
    return [r for r in _cache[business_id] if (agent is None or r.agent == agent) and (kind is None or r.kind == kind)]


async def recent_for_trigger(business_id: uuid.UUID, kind: str, match: dict[str, Any], statuses: tuple[str, ...],
                             within_days: int | None = None) -> list[LearnedRule]:
    """Rules of a kind whose trigger contains all key/values of `match`, optionally changed recently."""
    async with read_session() as s:
        rows = (await s.execute(select(LearnedRule).where(LearnedRule.business_id == business_id, LearnedRule.kind == kind,
                                                          LearnedRule.status.in_(statuses)))).scalars().all()
    cutoff = clock_now() - timedelta(days=within_days) if within_days else None
    return [r for r in rows if all(str(r.trigger.get(k)) == str(v) for k, v in match.items())
            and (cutoff is None or (r.status_changed_at or r.created_at) >= cutoff)]


async def mark_applied(rule_id: uuid.UUID) -> None:
    async with write_session() as s:
        r = await s.get(LearnedRule, rule_id)
        if r is not None:
            r.times_applied += 1


async def mark_overridden(rule_id: uuid.UUID) -> None:
    async with write_session() as s:
        r = await s.get(LearnedRule, rule_id)
        if r is not None:
            r.times_overridden += 1


# ------------------------------------------------------------------ application
def _lookup(inputs: dict[str, Any], path: str) -> Any:
    cur: Any = inputs
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def compare(actual: Any, op: str, expected: Any) -> bool:
    a, b = _num(actual), _num(expected)
    if a is not None and b is not None:
        left, right = a, b
    else:
        left, right = str(actual), str(expected)  # type: ignore[assignment]
    return {"lt": left < right, "le": left <= right, "gt": left > right, "ge": left >= right,
            "eq": left == right, "ne": left != right}[op]


async def apply_preconditions(spec: ActionSpec, ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    """PRECHECK hook: evaluate the active precondition/check rules for this action type."""
    out: list[Check] = []
    for rule in await active_rules(ctx.business_id):
        if rule.kind not in APPLIED_KINDS or rule.trigger.get("action_type") != spec.name:
            continue
        when = rule.trigger.get("when") or {}
        if any(str(_lookup(inputs, k)) != str(v) for k, v in when.items()):
            continue
        actual = _lookup(inputs, rule.trigger["field"])
        if actual is None:
            continue
        ok = compare(actual, rule.trigger["op"], rule.trigger["value"])
        await mark_applied(rule.id)
        out.append(Check("learned_rule", ok, {"rule_id": str(rule.id), "field": rule.trigger["field"], "value": actual},
                         ask=True, reason_en="" if ok else f"learned rule: {rule.rule_text_en}",
                         reason_ar="" if ok else f"قاعدة مُتعلَّمة: {rule.rule_text_ar or rule.rule_text_en}",
                         learned_rule_id=str(rule.id)))
    return out


async def requires_approval(business_id: uuid.UUID, action_type: str) -> uuid.UUID | None:
    """A policy rule that turns off auto-approval for this action type (its id), else None."""
    for rule in await active_rules(business_id, kind="policy"):
        if rule.trigger.get("action_type") == action_type and rule.trigger.get("require_approval"):
            return rule.id
    return None


def register() -> None:
    from app.core.events import subscribe
    from app.harness.graph import PRECHECK_HOOKS

    if apply_preconditions not in PRECHECK_HOOKS:
        PRECHECK_HOOKS.append(apply_preconditions)
    subscribe("rule.activated", "harness_rule_cache", _on_rule_event)
    subscribe("rule.deactivated", "harness_rule_cache", _on_rule_event)
