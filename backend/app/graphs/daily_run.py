"""daily_run_graph: the fixed, ordered work for one business date (FR-012a, research R10/R17).

Agents register callables into named steps; empty steps are no-ops. The clock runs this graph
once per date, in date order, so jumping ahead never skips a day's checks.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.harness.audit import audit

log = logging.getLogger(__name__)
GRAPH = "daily_run"

STEPS: tuple[str, ...] = (
    "import_sales",
    "stock_update",
    "forecast_check",
    "reorder",
    "bank_import",
    "reconcile",
    "balance_compare",
    "cash_forecast",
    "trial_balance",
    "reminders",
    "approval_timeouts",
    "digest",
)

StepFn = Callable[[uuid.UUID, date], Awaitable[Any]]
_steps: dict[str, list[tuple[str, StepFn]]] = {s: [] for s in STEPS}


class DayState(TypedDict, total=False):
    business_id: str
    date: str
    done: list[str]
    failures: list[dict[str, str]]


def register_step(step: str, name: str, fn: StepFn) -> None:
    if step not in _steps:
        raise KeyError(f"unknown daily step {step!r}")
    if not any(n == name for n, _ in _steps[step]):
        _steps[step].append((name, fn))


def clear_steps() -> None:
    for s in _steps:
        _steps[s].clear()


def registered() -> dict[str, list[str]]:
    return {s: [n for n, _ in fns] for s, fns in _steps.items()}


def _node(step: str) -> Callable[[DayState], Awaitable[dict[str, Any]]]:
    async def run(state: DayState) -> dict[str, Any]:
        bid = uuid.UUID(state["business_id"])
        d = date.fromisoformat(state["date"])
        failures = list(state.get("failures", []))
        for name, fn in _steps[step]:
            try:
                await fn(bid, d)
            except Exception as exc:  # one failing step must not stop the rest of the day
                log.exception("daily step %s/%s failed on %s", step, name, d)
                failures.append({"step": step, "name": name, "error": repr(exc)})
                await audit("daily_step_failed", business_id=bid, inputs={"step": step, "name": name, "date": d},
                            outputs={"error": repr(exc)})
        return {"done": [*state.get("done", []), step], "failures": failures}

    return run


def build() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    prev = START
    for step in STEPS:
        g.add_node(step, _node(step))
        g.add_edge(prev, step)
        prev = step
    g.add_edge(prev, END)
    return g


async def run_day(business_id: uuid.UUID, d: date) -> dict[str, Any]:
    from app.graphs import runtime

    result = await runtime.start(
        GRAPH, {"business_id": str(business_id), "date": d.isoformat(), "done": [], "failures": []},
        f"daily:{business_id}:{d.isoformat()}:{uuid.uuid4().hex[:8]}",
    )
    await audit("daily_run_completed", business_id=business_id,
                inputs={"date": d}, outputs={"failures": result["values"].get("failures", [])})
    return result["values"]


def register() -> None:
    from app.graphs import runtime

    runtime.register(GRAPH, build)
