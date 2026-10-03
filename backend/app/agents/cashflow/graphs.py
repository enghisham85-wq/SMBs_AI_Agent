"""Cash-Flow Agent workflows as LangGraph graphs. State-changing steps run through harness_graph.

- cash_forecast_graph: freshness check -> project -> checks -> save -> weekly purchasing budget
- shortfall_plan_graph: detect -> plan -> save -> publish shortfall.predicted -> ask the owner (interrupt) -> apply
- balance_compare_graph: yesterday's projected closing balance vs the actual one
- reminders_graph: schedule escalating reminders -> send the ones due (each through send_reminder)
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.agents.cashflow import budget as budget_mod
from app.agents.cashflow import checks, plan, projection, reminders
from app.agents.cashflow.action_specs import inform_paid, paid_status
from app.approvals import service as approvals
from app.core import clock, settings_store
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness import calibration
from app.harness.action_spec import Check, OwnerAsk
from app.harness.audit import jsonable
from app.harness.graph import run_action
from app.harness.incidents import open_incident, resolve_incident
from app.models.books import ReceivableInvoice
from app.models.cash import (
    CashForecastRun,
    PaymentPromise,
    PaymentReminder,
    PlanAction,
    PurchasingBudget,
    ShortfallPlan,
)
from app.models.finance_master import BankTransaction
from app.models.harness import ApprovalRequest, CheckResult, Incident
from app.models.tenancy import Business

PLAN_GRAPH = "shortfall_plan"
REASK_GAP_CHANGE = 0.2  # re-present a plan when the gap grows by more than 20% ...
SAME_SHORTFALL_DAYS = 3  # ... or its first day below the buffer moves by more than this


class DayState(TypedDict, total=False):
    business_id: str
    date: str
    notes: dict[str, Any]


def _ids(state: DayState) -> tuple[uuid.UUID, date]:
    return uuid.UUID(state["business_id"]), date.fromisoformat(state["date"])


def _check_json(c: Check) -> dict[str, Any]:
    return {"name": c.name, "passed": c.passed, "details": jsonable(c.details), "reason_en": c.reason_en,
            "reason_ar": c.reason_ar}


async def _store_checks(bid: uuid.UUID, found: list[Check], action_id: uuid.UUID | None = None) -> None:
    async with write_session() as s:
        for c in found:
            s.add(CheckResult(business_id=bid, action_id=action_id, check_name=c.name, passed=c.passed,
                              details=jsonable(c.details)))


def _days_json(p: projection.Projection) -> list[dict[str, Any]]:
    return [{"date": d.date.isoformat(), "opening": d.opening, "inflows": d.inflows, "outflows": d.outflows,
             "closing": d.closing, "below_buffer": d.below_buffer, "confidence": d.confidence} for d in p.days]


async def _business(bid: uuid.UUID) -> Business:
    async with read_session() as s:
        b = await s.get(Business, bid)
    assert b is not None
    return b


async def _resolve_open(bid: uuid.UUID, prefix: str, root_cause: str, action_taken: str) -> None:
    async with read_session() as s:
        incs = (await s.execute(select(Incident).where(Incident.business_id == bid,
                                                       Incident.status.in_(("open", "investigating")),
                                                       Incident.dedupe_key.like(f"{prefix}%")))).scalars().all()
    for inc in incs:
        await resolve_incident(inc.id, root_cause=root_cause, category="data_gap", action_taken=action_taken)


# ============================================================= cash_forecast_graph
async def cf_freshness(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    b = await _business(bid)
    from app.core.country_profiles import calendar_for

    async with read_session() as s:
        stale_days = int(await settings_store.get(bid, "stale_bank_days", s))
        from app.agents.cashflow import position

        fresh = await position.freshness(s, bid, d, calendar_for(b), stale_days)
    c = checks.bank_freshness(fresh)
    await _store_checks(bid, [c])
    if not c.passed:
        # One incident per gap: a missed day stays in the check window for a week as later days arrive.
        key = (f"bank_freshness:missing:{fresh.missing_dates[0].isoformat()}" if fresh.missing_dates else
               f"bank_freshness:{fresh.last_bank_date.isoformat() if fresh.last_bank_date else 'none'}")
        inc = await open_incident(business_id=bid, agent="cashflow", type="bank_freshness", detected_by="bank_freshness",
                                  summary=c.reason_en, refs=jsonable(c.details), dedupe_key=key,
                                  action_taken="marked the forecast low confidence and asked for a statement")
        from app.harness.analysis import analyse_and_propose

        await analyse_and_propose(inc, resolve=False)  # stays open until the statement arrives
        await approvals.post_alert(business_id=bid, agent="cashflow", urgency=2, dedupe_key=key,
                                   text_en=c.reason_en + ".", text_ar=c.reason_ar + ".",
                                   context={"incident_id": str(inc), "upload": "bank_statement"})
    else:
        await _resolve_open(bid, "bank_freshness:", "bank data was missing or late", "fresh bank data arrived")
    return {"notes": {"freshness": _check_json(c), "low_confidence_reason": fresh.reason_en() if fresh.low_confidence else None,
                      "bank_data_as_of": fresh.bank_data_as_of.isoformat() if fresh.bank_data_as_of else None}}


async def _project(bid: uuid.UUID, d: date) -> tuple[projection.Inputs, dict[str, projection.Projection], Check]:
    async with read_session() as s:
        inp, _ = await projection.load_inputs(s, bid, d)
    unrealistic = checks.unrealistic_inflow(inp)
    return inp, projection.project_all(inp), unrealistic


async def cf_project(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    inp, projs, unrealistic = await _project(bid, d)
    primary = "expected" if unrealistic.passed else "pessimistic"
    notes = dict(state.get("notes", {}))
    notes.update({
        "primary_scenario": primary, "opening_minor": inp.opening_minor, "buffer_minor": inp.buffer_minor,
        "scenarios": {sc: _days_json(p) for sc, p in projs.items()},
        "flows": [f.to_json() for f in projs["expected"].flows],
        "checks": [_check_json(unrealistic)], "tight": inp.tight, "sales_source": inp.sales_source,
        "pessimistic_gap_minor": max(0, inp.buffer_minor - projs["pessimistic"].lowest.closing),
        "pessimistic_low_date": projs["pessimistic"].lowest.date.isoformat(),
        "regular_weekly_minor": sum(inp.regular_spend.values()),
    })
    notes["_double"] = _check_json(checks.double_counting(projs["expected"]))
    if not unrealistic.passed:
        await _store_checks(bid, [unrealistic])
    return {"notes": notes}


async def cf_checks(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    notes = dict(state.get("notes", {}))
    b = await _business(bid)
    double = notes.pop("_double")
    found: list[Check] = [Check(double["name"], double["passed"], double["details"], reason_en=double["reason_en"])]
    async with read_session() as s:
        inp, _ = await projection.load_inputs(s, bid, d)
        txns = (await s.execute(select(BankTransaction).where(BankTransaction.business_id == bid,
                                                              BankTransaction.date > d - timedelta(days=70),
                                                              BankTransaction.date <= d))).scalars().all()
    rows = [{"date": t.date, "amount_minor": t.amount.amount_minor, "description": t.description,
             "kind": projection._txn_kind(t),
             "obligation_id": (str(t.matched_id) if t.matched_type == "obligation" else (t.meta or {}).get("obligation_id"))}
            for t in txns]
    missing = checks.missing_recurring_obligation(inp.obligations, rows, d, b.currency)
    found.extend(missing)
    await _store_checks(bid, found)
    for c in missing:
        tag = hashlib.sha256(f"{c.details.get('kind')}:{c.details.get('obligation_id') or c.details.get('description')}"
                             f":{c.details.get('due', '')}".encode()).hexdigest()[:12]
        if c.details.get("kind") == "not_in_forecast":
            opts = [{"key": "add", "label_en": "Yes, add it", "label_ar": "نعم، أضفه", "effect": "add"},
                    {"key": "no", "label_en": "No, it's not recurring", "label_ar": "لا، ليس متكرراً", "effect": "ack"}]
        else:
            opts = [{"key": "paid_other", "label_en": "Paid another way", "label_ar": "دُفع بطريقة أخرى", "effect": "ack"},
                    {"key": "still_due", "label_en": "Still due", "label_ar": "ما زال مستحقاً", "effect": "ack"}]
        await approvals.post_question(business_id=bid, agent="cashflow", key=f"obligation:{tag}",
                                      ask=OwnerAsk(kind="question", text_en=c.reason_en, text_ar=c.reason_ar, options=opts,
                                                   safe_default=opts[-1]["key"], deadline_hours=48,
                                                   context=jsonable(c.details)))
    notes["checks"] = [*notes.get("checks", []), *[_check_json(c) for c in found]]
    return {"notes": notes}


async def cf_save(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    notes = dict(state.get("notes", {}))
    payload = {"generated_on": d.isoformat(), "bank_data_as_of": notes.get("bank_data_as_of"),
               "low_confidence_reason": notes.get("low_confidence_reason"), "primary_scenario": notes["primary_scenario"],
               "opening_minor": notes["opening_minor"], "buffer_minor": notes["buffer_minor"], "horizon": 30,
               "flows": notes["flows"], "checks": [notes["freshness"], *notes.get("checks", [])],
               "scenarios": notes["scenarios"]}
    out = await run_action("save_forecast_run", payload, bid)
    notes["run_id"] = (out.get("result") or {}).get("run_id") if out["outcome"] == "completed" else None
    return {"notes": notes}


async def cf_budget(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    notes = state.get("notes", {})
    b = await _business(bid)
    low = date.fromisoformat(notes["pessimistic_low_date"])
    bud = budget_mod.compute(d + timedelta(days=1), int(notes["regular_weekly_minor"]), int(notes["pessimistic_gap_minor"]),
                             max(1.0, (low - d).days / 7), b.currency)
    if bud.amount_minor <= 0:
        return {}
    await run_action("publish_budget", {"week_start": bud.week_start.isoformat(), "amount_minor": bud.amount_minor,
                                        "tightened": bud.tightened, "reason_en": bud.reason_en, "reason_ar": bud.reason_ar,
                                        "forecast_run_id": notes.get("run_id")}, bid)
    return {}


def build_cash_forecast() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    for name, fn in (("freshness", cf_freshness), ("project", cf_project), ("checks", cf_checks), ("save", cf_save),
                     ("budget", cf_budget)):
        g.add_node(name, fn)
    g.add_edge(START, "freshness")
    g.add_edge("freshness", "project")
    g.add_edge("project", "checks")
    g.add_edge("checks", "save")
    g.add_edge("save", "budget")
    g.add_edge("budget", END)
    return g


# ============================================================= shortfall_plan_graph
class PlanState(TypedDict, total=False):
    business_id: str
    date: str
    thread_id: str
    run_id: str | None
    shortfall: dict[str, Any] | None
    plan_id: str | None
    present: bool
    answer: dict[str, Any] | None


async def latest_run(bid: uuid.UUID, on_or_before: date | None = None) -> CashForecastRun | None:
    async with read_session() as s:
        q = select(CashForecastRun).where(CashForecastRun.business_id == bid)
        if on_or_before is not None:
            q = q.where(CashForecastRun.generated_on <= on_or_before)
        return (await s.execute(q.order_by(CashForecastRun.generated_on.desc(), CashForecastRun.created_at.desc())
                                .limit(1))).scalars().first()


async def _base(bid: uuid.UUID, d: date, scenario: str) -> tuple[projection.Inputs, projection.Projection]:
    async with read_session() as s:
        inp, _ = await projection.load_inputs(s, bid, d)
    projs = projection.project_all(inp)  # sets inp.tight the same way the saved run did
    return inp, projs[scenario]


async def sp_detect(state: PlanState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    d = date.fromisoformat(state["date"])
    run = await latest_run(bid, d)
    if run is None or run.generated_on != d:
        return {"shortfall": None, "run_id": None}
    inp, base = await _base(bid, d, run.primary_scenario)
    sf = plan.detect_shortfall(base, inp.buffer_minor, d)
    if sf is None:
        async with write_session() as s:
            for p in (await s.execute(select(ShortfallPlan).where(ShortfallPlan.business_id == bid,
                                                                  ShortfallPlan.status.in_(("proposed", "presented"))))).scalars():
                p.status = "superseded"
        return {"shortfall": None, "run_id": str(run.id)}
    return {"run_id": str(run.id), "shortfall": {"gap_minor": sf.gap_minor, "first_below": sf.first_below.isoformat(),
                                                 "lowest_minor": sf.lowest_minor, "lowest_date": sf.lowest_date.isoformat(),
                                                 "last_below": sf.last_below.isoformat(), "days_to_act": sf.days_to_act}}


async def sp_plan(state: PlanState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    d = date.fromisoformat(state["date"])
    run = await latest_run(bid, d)
    assert run is not None
    inp, base = await _base(bid, d, run.primary_scenario)
    sf = plan.detect_shortfall(base, inp.buffer_minor, d)
    assert sf is not None
    built = plan.build_plan(inp, base, sf)
    async with read_session() as s:
        # The last plan the owner saw, whether still open, acted on or dismissed.
        current = (await s.execute(select(ShortfallPlan).where(ShortfallPlan.business_id == bid,
                                                               ShortfallPlan.status.in_(("presented", "accepted", "dismissed")))
                                   .order_by(ShortfallPlan.created_at.desc()).limit(1))).scalars().first()
    # Ask again only about a different shortfall (its date moved by more than a few days: while cash stays
    # below the buffer, each day's forecast says "first below tomorrow") or one that got materially worse.
    present = (current is None or abs((sf.first_below - current.gap_date).days) > SAME_SHORTFALL_DAYS
               or sf.gap_minor > (1 + REASK_GAP_CHANGE) * max(1, current.gap_amount.amount_minor))
    actions = []
    for c in built.actions:
        assert c.simulated is not None
        actions.append({"type": c.type, "target_type": c.target_type, "target_ref": c.target_ref,
                        "description_en": c.description_en, "description_ar": c.description_ar,
                        "impact_minor": c.impact_minor, "risk": c.risk, "rank": c.rank,
                        "simulated_lowest_minor": c.simulated.lowest.closing,
                        "simulated_lowest_date": c.simulated.lowest.date.isoformat(),
                        "adjustments": c.adjustments, "series": c.simulated.series()})
    out = await run_action("save_shortfall_plan", {"run_id": str(run.id), "shortfall": state["shortfall"],
                                                   "combined_lowest_minor": built.combined_lowest_minor,
                                                   "actions": actions}, bid)
    plan_id = (out.get("result") or {}).get("plan_id") if out["outcome"] == "completed" else None
    if plan_id and not present and current is not None and current.status == "presented":
        async with write_session() as s:  # the owner already has this shortfall in front of them
            row = await s.get(ShortfallPlan, uuid.UUID(plan_id))
            if row is not None:
                row.status = "presented"
                row.graph_thread_id = current.graph_thread_id
    return {"plan_id": plan_id, "present": bool(plan_id) and present}


async def sp_publish(state: PlanState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    sf = state["shortfall"] or {}
    cur = (await _business(bid)).currency
    async with write_session() as s:
        publish(s, "shortfall.predicted", {"run_id": state["run_id"], "gap_amount": Money(int(sf["gap_minor"]), cur),
                                           "gap_date": sf["first_below"], "days_to_act": sf["days_to_act"],
                                           "plan_id": state["plan_id"], "lowest_date": sf["lowest_date"],
                                           "lowest_balance": Money(int(sf["lowest_minor"]), cur)},
                producer="cashflow", business_id=bid)
        row = await s.get(ShortfallPlan, uuid.UUID(state["plan_id"])) if state.get("plan_id") else None
        if row is not None:
            row.status = "presented"
            row.graph_thread_id = state["thread_id"]
    gap = Money(int(sf["gap_minor"]), cur)
    async with read_session() as s:
        latest = (await s.execute(select(PurchasingBudget).where(PurchasingBudget.business_id == bid)
                                  .order_by(PurchasingBudget.created_at.desc()).limit(1))).scalars().first()
    tightened = latest is not None and latest.tightened
    inc = await open_incident(
        business_id=bid, agent="cashflow", type="shortfall_predicted", detected_by="shortfall_detection",
        summary=f"Cash is projected to fall {gap.to_display()} below the buffer on {sf['first_below']}, "
                f"{sf['days_to_act']} days from now",
        refs={"gap_minor": sf["gap_minor"], "gap_date": sf["first_below"], "days_to_act": sf["days_to_act"],
              "plan_id": state.get("plan_id")},
        dedupe_key=f"shortfall:{sf['first_below']}",
        action_taken=("tightened the purchasing budget and presented a ranked action plan" if tightened
                      else "presented a ranked action plan"))
    from app.harness.analysis import analyse_and_propose

    await analyse_and_propose(inc)
    return {}


async def sp_ask(state: PlanState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    sf = state["shortfall"] or {}
    b = await _business(bid)
    cur = b.currency
    async with read_session() as s:
        acts = (await s.execute(select(PlanAction).where(PlanAction.plan_id == uuid.UUID(state["plan_id"]))
                                .order_by(PlanAction.rank))).scalars().all()
    low = Money(int(sf["lowest_minor"]), cur)
    first = date.fromisoformat(sf["first_below"])
    buf = b.min_cash_buffer
    top = [a for a in acts if a.type != "financing"][:2]
    opts_en = " ".join(f"{i}) {a.description_en} (+{a.impact.to_display()})." for i, a in enumerate(top, start=1))
    opts_ar = " ".join(f"{i}) {a.description_ar} (+{a.impact.to_display('ar')})." for i, a in enumerate(top, start=1))
    low_date = date.fromisoformat(sf["lowest_date"])
    text_en = (f"Heads up: cash drops to {low.to_display()} on {low_date.strftime('%d %b')} (buffer is {buf.to_display()}); "
               f"it first goes below the buffer on {first.strftime('%d %b')}, {sf['days_to_act']} days from now. "
               + (f"Best options: {opts_en} " if top else "") + "Financing only as a last resort.")
    text_ar = (f"تنبيه: ينخفض النقد إلى {low.to_display('ar')} في {low_date.strftime('%d/%m')} (الحد الأدنى {buf.to_display('ar')})؛ "
               f"ويقل عن الحد الأدنى أول مرة في {first.strftime('%d/%m')}، بعد {sf['days_to_act']} يوماً. "
               + (f"أفضل الخيارات: {opts_ar} " if top else "") + "التمويل كحل أخير فقط.")
    options = [{"key": f"do:{a.id}", "label_en": f"Do option {i}", "label_ar": f"نفّذ الخيار {i}", "effect": "accept"}
               for i, a in enumerate(top, start=1)]
    options.append({"key": "review", "label_en": "I'll review the plan", "label_ar": "سأراجع الخطة", "effect": "ack"})
    options.append({"key": "dismiss", "label_en": "Dismiss", "label_ar": "تجاهل", "effect": "dismiss"})
    ans = await approvals.ask_owner(
        business_id=bid, graph_name=PLAN_GRAPH, thread_id=state["thread_id"], gate_key="plan",
        ask=OwnerAsk(kind="question", text_en=text_en[:1000], text_ar=text_ar[:1000], options=options[:4],
                     required_role="owner", safe_default="review", deadline_hours=24, urgency=2,
                     context={"plan_id": state["plan_id"], "gap_minor": sf["gap_minor"], "gap_date": sf["first_below"]}),
        agent="cashflow")
    return {"answer": ans}


async def sp_apply(state: PlanState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    ans = state.get("answer") or {}
    effect, key = ans.get("effect"), ans.get("option_key") or ""
    async with write_session() as s:
        p = await s.get(ShortfallPlan, uuid.UUID(state["plan_id"]))
        if p is None:
            return {}
        if effect == "dismiss":
            p.status = "dismissed"
            return {}
        if effect != "accept" or not key.startswith("do:"):
            return {}
        act = await s.get(PlanAction, uuid.UUID(key.split(":", 1)[1]))
        if act is None:
            return {}
        act.status = "accepted"
        p.status = "accepted"
        chase = act.target_ref if act.type == "chase_receivable" else None
    if chase:
        await chase_now(bid, uuid.UUID(chase), date.fromisoformat(state["date"]))
    return {}


def _plan_route(state: PlanState) -> str:
    return "plan" if state.get("shortfall") else "end"


def _present_route(state: PlanState) -> str:
    return "publish" if state.get("present") else "end"


def build_shortfall_plan() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(PlanState)
    g.add_node("detect", sp_detect)
    g.add_node("plan", sp_plan)
    g.add_node("publish", sp_publish)
    g.add_node("ask", sp_ask)
    g.add_node("apply", sp_apply)
    g.add_edge(START, "detect")
    g.add_conditional_edges("detect", _plan_route, {"plan": "plan", "end": END})
    g.add_conditional_edges("plan", _present_route, {"publish": "publish", "end": END})
    g.add_edge("publish", "ask")
    g.add_edge("ask", "apply")
    g.add_edge("apply", END)
    return g


# ============================================================= balance_compare_graph
async def bc_compare(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    b = await _business(bid)
    async with read_session() as s:
        prev = (await s.execute(select(CashForecastRun).where(CashForecastRun.business_id == bid,
                                                              CashForecastRun.generated_on < d)
                                .order_by(CashForecastRun.generated_on.desc(), CashForecastRun.created_at.desc())
                                .limit(1))).scalars().first()
        from app.agents.cashflow import position
        from app.models.cash import CashForecast

        balances = await position.account_balances(s, bid, d)
        has_today = any(a.last_date == d for a in balances if not a.is_cash_on_hand)
        row = None
        if prev is not None:
            row = (await s.execute(select(CashForecast).where(CashForecast.run_id == prev.id, CashForecast.scenario == "expected",
                                                              CashForecast.date == d))).scalar_one_or_none()
        txns = (await s.execute(select(BankTransaction).where(BankTransaction.business_id == bid,
                                                              BankTransaction.date == d))).scalars().all()
        threshold = float(await settings_store.get(bid, "cash_variance_pct", s))
    if prev is None or row is None or not has_today:
        return {"notes": {"skipped": "no forecast for today or no bank data yet"}}
    actual = sum(a.balance_minor for a in balances)
    flows = [f for f in prev.flows if f["date"] == d.isoformat()]
    c = checks.forecast_vs_actual(d, row.closing.amount_minor, actual, flows,
                                  [(projection._txn_kind(t), t.amount.amount_minor) for t in txns], threshold, b.currency)
    await _store_checks(bid, [c])
    await calibration.record(bid, "cashflow", "forecast_variance", c.details["variance_pct"] / 100, d, threshold / 100)
    if not c.passed:
        key = f"cash_variance:{d.isoformat()}"
        inc = await open_incident(business_id=bid, agent="cashflow", type="forecast_vs_actual", detected_by="forecast_vs_actual",
                                  summary=c.reason_en, refs=jsonable(c.details), dedupe_key=key,
                                  action_taken="re-based today's forecast on the actual balance")
        await approvals.post_alert(business_id=bid, agent="cashflow", dedupe_key=key, context={"incident_id": str(inc)},
                                   text_en=c.reason_en + ". I re-based today's forecast on the actual balance.",
                                   text_ar=c.reason_ar + ". أعدت بناء توقع اليوم على الرصيد الفعلي.")
    return {"notes": {"check": _check_json(c)}}


def build_balance_compare() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("compare", bc_compare)
    g.add_edge(START, "compare")
    g.add_edge("compare", END)
    return g


# ============================================================= reminders_graph
async def _promise(s: Any, inv_id: uuid.UUID, today: date, paid: bool) -> reminders.PromiseState | None:
    p = (await s.execute(select(PaymentPromise).where(PaymentPromise.receivable_invoice_id == inv_id)
                         .order_by(PaymentPromise.created_at.desc()).limit(1))).scalars().first()
    return _promise_state(p, today, paid)


def _promise_state(p: PaymentPromise | None, today: date, paid: bool) -> reminders.PromiseState | None:
    """Settle a promise whose date has passed (or that was kept) and return its state."""
    if p is None:
        return None
    if p.outcome is None and (paid or p.promised_date < today):
        p.outcome = "kept" if paid else "broken"
    return reminders.PromiseState(p.promised_date, None if p.outcome is None else p.outcome == "kept")


async def rm_schedule(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    b = await _business(bid)
    created = []
    async with write_session() as s:
        invs = (await s.execute(select(ReceivableInvoice).where(ReceivableInvoice.business_id == bid,
                                                                ReceivableInvoice.status.in_(("open", "partially_paid"))))).scalars().all()
        # One query each for every open invoice's reminders, latest promise and the customers' paid history,
        # instead of three per invoice while holding the write lock.
        ids = [inv.id for inv in invs]
        by_inv: dict[uuid.UUID, list[PaymentReminder]] = defaultdict(list)
        latest: dict[uuid.UUID, PaymentPromise] = {}
        paid_by_customer: dict[str, list[ReceivableInvoice]] = defaultdict(list)
        if ids:
            for r in (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id.in_(ids)))).scalars():
                by_inv[r.receivable_invoice_id].append(r)
            for pr in (await s.execute(select(PaymentPromise).where(PaymentPromise.receivable_invoice_id.in_(ids))
                                       .order_by(PaymentPromise.created_at.desc()))).scalars():
                latest.setdefault(pr.receivable_invoice_id, pr)
            for paid in (await s.execute(select(ReceivableInvoice).where(
                    ReceivableInvoice.business_id == bid, ReceivableInvoice.status == "paid",
                    ReceivableInvoice.customer_name.in_(sorted({inv.customer_name for inv in invs}))))).scalars():
                paid_by_customer[paid.customer_name].append(paid)
        for inv in invs:
            rows = by_inv.get(inv.id, [])
            if any(r.status in ("scheduled", "pending_approval") for r in rows):
                continue
            promise = _promise_state(latest.get(inv.id), d, False)
            sent = {r.level: (r.sent_at.date() if r.sent_at else r.scheduled_for) for r in rows if r.status == "sent"}
            score = projection.late_score(paid_by_customer.get(inv.customer_name, []), inv.late_payment_history_score)
            nxt = reminders.next_reminder(inv.due_date, score, sent, d, promise)
            if nxt is None or any(r.level == nxt.level for r in rows):
                continue  # all sent, paused by a promise, or this level was cancelled by the owner
            owed = Money(inv.total.amount_minor - inv.amount_paid_minor, inv.total.currency)
            en, ar = reminders.texts(nxt.level, inv.customer_name, inv.number, owed, inv.due_date, b.name)
            s.add(PaymentReminder(business_id=bid, receivable_invoice_id=inv.id, level=nxt.level,
                                  scheduled_for=nxt.scheduled_for, status="scheduled", text_en=en, text_ar=ar))
            created.append({"invoice": inv.number, "level": nxt.level, "on": nxt.scheduled_for.isoformat()})
    return {"notes": {"scheduled": created}}


async def rm_send(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    async with read_session() as s:
        due = (await s.execute(select(PaymentReminder).where(PaymentReminder.business_id == bid,
                                                             PaymentReminder.status == "scheduled",
                                                             PaymentReminder.scheduled_for <= d)
                               .order_by(PaymentReminder.scheduled_for))).scalars().all()
        customers = {i.id: i.customer_name for i in (await s.execute(select(ReceivableInvoice).where(
            ReceivableInvoice.id.in_([r.receivable_invoice_id for r in due])))).scalars()}
    results = []
    for rem in due:
        # The customer is part of the inputs so learned rules about a customer can apply.
        out = await run_action("send_reminder", {"reminder_id": str(rem.id),
                                                 "customer": customers.get(rem.receivable_invoice_id)}, bid)
        results.append({"reminder_id": str(rem.id), "outcome": "awaiting_approval" if out["interrupted"] else out["outcome"]})
    return {"notes": {**state.get("notes", {}), "sent": results}}


def build_reminders() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("schedule", rm_schedule)
    g.add_node("send", rm_send)
    g.add_edge(START, "schedule")
    g.add_edge("schedule", "send")
    g.add_edge("send", END)
    return g


async def chase_now(bid: uuid.UUID, invoice_id: uuid.UUID, d: date) -> dict[str, Any] | None:
    """Schedule the next reminder level for today and run it (used when the owner picks 'chase')."""
    b = await _business(bid)
    async with write_session() as s:
        inv = await s.get(ReceivableInvoice, invoice_id)
        if inv is None or inv.status not in ("open", "partially_paid"):
            return None
        rows = (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id == inv.id))).scalars().all()
        waiting = next((r for r in rows if r.status in ("scheduled", "pending_approval")), None)
        if waiting is not None:
            if waiting.status == "pending_approval":
                return {"reminder_id": str(waiting.id), "outcome": "awaiting_approval"}
            waiting.scheduled_for = d
            rid = waiting.id
        else:
            level = min(3, max((r.level for r in rows), default=0) + 1)
            if any(r.level == level for r in rows):
                return None
            owed = Money(inv.total.amount_minor - inv.amount_paid_minor, inv.total.currency)
            en, ar = reminders.texts(level, inv.customer_name, inv.number, owed, inv.due_date, b.name)
            rem = PaymentReminder(business_id=bid, receivable_invoice_id=inv.id, level=level, scheduled_for=d,
                                  status="scheduled", text_en=en, text_ar=ar)
            s.add(rem)
            await s.flush()
            rid = rem.id
        customer = inv.customer_name
    out = await run_action("send_reminder", {"reminder_id": str(rid), "customer": customer}, bid)
    return {"reminder_id": str(rid), "outcome": "awaiting_approval" if out["interrupted"] else out["outcome"]}


# ============================================================= events
async def on_customer_payment(env: dict[str, Any]) -> None:
    """customer_payment.received: cancel reminders for a now-paid invoice and tell the owner."""
    bid = uuid.UUID(env["business_id"])
    inv_id = uuid.UUID(env["payload"]["receivable_invoice_id"])
    async with write_session() as s:
        inv = await s.get(ReceivableInvoice, inv_id)
        if inv is None:
            return
        paid = await paid_status(s, inv)
        await _promise(s, inv.id, clock.today(), paid is not None)
        if paid is None:
            return
        rems = (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id == inv_id,
                                                              PaymentReminder.status.in_(("scheduled", "pending_approval"))))).scalars().all()
        waiting = [str(r.id) for r in rems if r.status == "pending_approval"]
        for r in rems:
            if r.status == "scheduled":
                r.status, r.cancel_reason = "cancelled_paid", f"paid ({paid['via']}) on {paid['paid_on']}"
        scheduled = [r.id for r in rems if r.status == "cancelled_paid"]
    for rid in scheduled:
        await inform_paid(bid, inv, paid["paid_on"], rid)
    if waiting:
        async with read_session() as s:
            reqs = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                                  ApprovalRequest.status == "pending",
                                                                  ApprovalRequest.agent == "cashflow"))).scalars().all()
        for req in reqs:
            if (req.context or {}).get("reminder_id") in waiting:
                await approvals.withdraw(str(req.id), f"{inv.customer_name} paid {inv.number}")


async def on_obligation_answer(req: dict[str, Any]) -> None:
    """Owner confirmed a monthly bank payment should be a recurring obligation."""
    if req.get("effect") != "add":
        return
    ctx = req.get("context") or {}
    bid = uuid.UUID(req["business_id"])
    today = clock.today()
    day = int(ctx.get("day_of_month") or today.day)
    nxt = date(today.year, today.month, min(day, 28))
    if nxt <= today:
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, min(day, 28))
    await run_action("save_obligation", {"fields": {"type": "other", "description": str(ctx.get("description", ""))[:200],
                                                    "amount_minor": int(ctx["amount_minor"]), "next_due_date": nxt.isoformat(),
                                                    "recurrence": "monthly", "is_confirmed": True}}, bid)


# ============================================================= daily steps
async def _run(graph: str, bid: uuid.UUID, d: date, extra: dict[str, Any] | None = None) -> Any:
    from app.graphs import runtime

    return await runtime.start(graph, {"business_id": str(bid), "date": d.isoformat(), "notes": {}, **(extra or {})},
                               f"{graph}:{bid}:{d.isoformat()}:{uuid.uuid4().hex[:6]}")


async def step_balance_compare(bid: uuid.UUID, d: date) -> Any:
    return await _run("balance_compare", bid, d)


async def step_cash_forecast(bid: uuid.UUID, d: date) -> Any:
    return await _run("cash_forecast", bid, d)


async def step_shortfall_plan(bid: uuid.UUID, d: date) -> Any:
    from app.graphs import runtime

    thread = f"{PLAN_GRAPH}:{bid}:{d.isoformat()}:{uuid.uuid4().hex[:6]}"
    return await runtime.start(PLAN_GRAPH, {"business_id": str(bid), "date": d.isoformat(), "thread_id": thread}, thread)


async def step_reminders(bid: uuid.UUID, d: date) -> Any:
    return await _run("reminders", bid, d)


async def refresh(bid: uuid.UUID) -> None:
    """Re-run today's forecast and plan (after a bank upload or an obligation change)."""
    d = clock.today()
    await step_cash_forecast(bid, d)
    await step_shortfall_plan(bid, d)


def register_graphs() -> None:
    from app.graphs import runtime
    from app.graphs.daily_run import register_step

    runtime.register("cash_forecast", build_cash_forecast)
    runtime.register(PLAN_GRAPH, build_shortfall_plan)
    runtime.register("balance_compare", build_balance_compare)
    runtime.register("reminders", build_reminders)
    register_step("balance_compare", "balance_compare_graph", step_balance_compare)
    register_step("cash_forecast", "cash_forecast_graph", step_cash_forecast)
    register_step("cash_forecast", "shortfall_plan_graph", step_shortfall_plan)
    register_step("reminders", "reminders_graph", step_reminders)
