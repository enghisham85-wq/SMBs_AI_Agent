"""Machine-checkable triggers for learned rules, one JSON schema per kind.

A rule proposed by the model is only stored when its trigger validates, so every active rule can be
applied by code rather than by re-reading its text.
"""

from __future__ import annotations

import re
from typing import Any

OPS = ("lt", "le", "gt", "ge", "eq", "ne")

# Precondition/check: compare one field of an action's inputs (dotted path) with a value.
_COMPARE = {
    "type": "object",
    "required": ["action_type", "field", "op", "value"],
    "properties": {
        "action_type": {"type": "string"},
        "field": {"type": "string"},
        "op": {"enum": list(OPS)},
        "value": {"type": ["number", "string", "boolean"]},
        "when": {"type": "object"},  # optional: only when these input fields equal these values
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "precondition": _COMPARE,
    "check": _COMPARE,
    "parsing_hint": {
        "type": "object",
        "required": ["supplier_id"],
        "properties": {"supplier_id": {"type": "string"}, "date_format": {"enum": ["DMY", "MDY"]},
                       "hint": {"type": "string"}},
        "any_of_required": [["date_format"], ["hint"]],
    },
    "classification": {
        "type": "object",
        "required": ["supplier_id", "account_code"],
        "properties": {"supplier_id": {"type": "string"}, "account_code": {"type": "string"}},
    },
    # Either turn auto-approval off for an action type (require_approval), or let routine orders from one
    # supplier up to a limit go ahead without asking (auto_approve_up_to_minor).
    "policy": {
        "type": "object",
        "required": ["action_type"],
        "properties": {"action_type": {"type": "string"}, "require_approval": {"type": "boolean"},
                       "auto_approve_up_to_minor": {"type": "number"}, "supplier_id": {"type": "string"}},
        "any_of_required": [["require_approval"], ["auto_approve_up_to_minor", "supplier_id"]],
    },
}

_TYPES = {"string": str, "number": (int, float), "boolean": bool, "object": dict}


class InvalidTriggerError(ValueError):
    pass


def _type_ok(value: Any, expected: str | list[str]) -> bool:
    names = [expected] if isinstance(expected, str) else expected
    for n in names:
        t = _TYPES[n]
        if n == "number" and isinstance(value, bool):
            continue
        if isinstance(value, t):  # type: ignore[arg-type]
            return True
    return False


def validate(kind: str, trigger: Any) -> None:
    """Raise InvalidTriggerError unless `trigger` matches the schema for `kind`."""
    schema = SCHEMAS.get(kind)
    if schema is None:
        raise InvalidTriggerError(f"unknown rule kind {kind!r}")
    if not isinstance(trigger, dict):
        raise InvalidTriggerError("trigger must be an object")
    missing = [k for k in schema["required"] if k not in trigger]
    if missing:
        raise InvalidTriggerError(f"{kind} trigger is missing {missing}")
    props = schema["properties"]
    unknown = set(trigger) - set(props)
    if unknown:
        raise InvalidTriggerError(f"{kind} trigger has unknown fields {sorted(unknown)}")
    for k, v in trigger.items():
        spec = props[k]
        if "enum" in spec and v not in spec["enum"]:
            raise InvalidTriggerError(f"{k} must be one of {spec['enum']}")
        if "type" in spec and not _type_ok(v, spec["type"]):
            raise InvalidTriggerError(f"{k} has the wrong type")
    if "any_of_required" in schema and not any(all(k in trigger for k in group) for group in schema["any_of_required"]):
        raise InvalidTriggerError(f"{kind} trigger needs one of {schema['any_of_required']}")
    if kind in ("precondition", "check") and not re.fullmatch(r"[A-Za-z_][\w.]*", trigger["field"]):
        raise InvalidTriggerError("field must be a dotted path")


def is_valid(kind: str, trigger: Any) -> bool:
    try:
        validate(kind, trigger)
    except InvalidTriggerError:
        return False
    return True
