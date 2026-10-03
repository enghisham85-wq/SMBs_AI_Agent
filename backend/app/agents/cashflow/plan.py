"""Shortfall detection and the ranked action plan. Financing always comes last."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.agents.cashflow import payables as pay
from app.agents.cashflow.projection import Inputs, Projection, occurrences, project
from app.db.types import Money

RISK_SCORE = {"low": 1, "medium": 2, "high": 3}
MOVABLE_OBLIGATIONS = {"other": "low", "subscription": "medium", "utility": "medium"}
CHASE_DAYS = 5
DEFER_PO_DAYS = 7
PURCHASING_CUT = "0.85"
MAX_ACTIONS = 5
MOVE_AFTER_DAYS = 7


@dataclass
class Shortfall:
    gap_minor: int  # how far the lowest point is below the buffer
    first_below: date
    lowest_minor: int
    lowest_date: date
    last_below: date
    days_to_act: int


@dataclass
class Candidate:
    type: str
    target_type: str
    target_ref: str | None
    description_en: str
    description_ar: str
    risk: str
    adjustments: list[dict[str, Any]]
    impact_minor: int = 0
    simulated: Projection | None = None
    rank: int = 0


@dataclass
class Plan:
    shortfall: Shortfall
    actions: list[Candidate] = field(default_factory=list)
    combined_lowest_minor: int = 0


def detect_shortfall(proj: Projection, buffer_minor: int, today: date) -> Shortfall | None:
    first = proj.first_below
    if first is None:
        return None
    low = proj.lowest
    last = max(d.date for d in proj.days if d.below_buffer)
    return Shortfall(buffer_minor - low.closing, first.date, low.closing, low.date, last, (first.date - today).days)


def _money(minor: int, cur: str, lang: str = "en") -> str:
    return Money(minor, cur).to_display(lang)


def candidates(inp: Inputs, base: Projection, sf: Shortfall) -> list[Candidate]:
    cur = inp.currency
    out: list[Candidate] = []
    # chase receivables expected after the dip
    for r in inp.receivables:
        flows = [f for f in base.flows if f.kind == "receivable" and f.ref == r.id]
        late = sum(f.amount_minor for f in flows if f.date >= sf.first_below)
        if late <= 0 and flows:
            continue
        chase_on = max(inp.start, inp.today + timedelta(days=CHASE_DAYS))
        overdue = (inp.today - r.due_date).days
        when_en = f"{overdue} days overdue" if overdue > 0 else f"due {r.due_date.strftime('%d %b')}"
        when_ar = f"متأخرة {overdue} يوماً" if overdue > 0 else f"تستحق {r.due_date.strftime('%d/%m')}"
        out.append(Candidate(
            "chase_receivable", "receivable", r.id,
            f"Chase {r.customer} for {r.number} ({_money(r.outstanding_minor, cur)}, {when_en})",
            f"طالب {r.customer} بالفاتورة {r.number} ({_money(r.outstanding_minor, cur, 'ar')}، {when_ar})",
            "medium" if r.late_score >= 0.3 else "low",
            [{"prefix": f"receivable:{r.id}", "move_to": chase_on.isoformat()}]))
    # pay suppliers at the end of terms, never later
    for p in inp.payables:
        rec = pay.recommend(p.terms, p.outstanding_minor, inp.today, tight=inp.tight)
        if rec.pay_on >= p.terms.due_date or rec.pay_on > sf.lowest_date:
            continue
        lose = f", losing the {_money(rec.discount_minor, cur)} early-payment discount" if rec.discount_minor else ""
        lose_ar = f"، مع خسارة خصم السداد المبكر {_money(rec.discount_minor, cur, 'ar')}" if rec.discount_minor else ""
        out.append(Candidate(
            "delay_payable", "payable", p.id,
            f"Pay {p.label} on {p.terms.due_date.strftime('%d %b')}, the last day of its terms{lose}",
            f"ادفع {p.label} في {p.terms.due_date.strftime('%d/%m')}، آخر يوم في المهلة{lose_ar}",
            "medium" if rec.discount_minor else "low",
            [{"key": f"payable:{p.id}", "move_to": p.terms.due_date.isoformat()}]))
    # defer unsent, non-critical POs
    for po in inp.pos:
        if po.is_critical or po.status not in ("draft", "pending_approval", "approved"):
            continue
        f = next((x for x in base.flows if x.key == f"po:{po.id}"), None)
        if f is None or f.date > sf.lowest_date:
            continue
        out.append(Candidate(
            "defer_po", "po", po.id,
            f"Delay order {po.number} ({_money(po.total_minor, cur)}) by {DEFER_PO_DAYS} days",
            f"أجّل أمر الشراء {po.number} ({_money(po.total_minor, cur, 'ar')}) {DEFER_PO_DAYS} أيام",
            "medium", [{"key": f"po:{po.id}", "shift_days": DEFER_PO_DAYS}]))
    if any(f.kind == "planned_purchases" and f.date <= sf.lowest_date for f in base.flows):
        out.append(Candidate(
            "defer_po", "planned_purchases", None,
            f"Cut non-critical purchasing by 15% until {sf.lowest_date.strftime('%d %b')}",
            f"خفّض المشتريات غير الضرورية 15% حتى {sf.lowest_date.strftime('%d/%m')}",
            "medium", [{"prefix": "planned:", "scale": PURCHASING_CUT, "until": sf.lowest_date.isoformat()}]))
    # flexible expenses move to a week after recovery
    move_to = sf.last_below + timedelta(days=MOVE_AFTER_DAYS)
    for ob in inp.obligations:
        risk = MOVABLE_OBLIGATIONS.get(ob.type)
        if risk is None:
            continue
        for d in occurrences(ob, inp.start, sf.lowest_date):
            if d >= move_to:
                continue
            out.append(Candidate(
                "move_expense", "obligation", ob.id,
                f"Move {ob.description} ({_money(ob.amount_minor, cur)}) from {d.strftime('%d %b')} to {move_to.strftime('%d %b')}",
                f"انقل {ob.description} ({_money(ob.amount_minor, cur, 'ar')}) من {d.strftime('%d/%m')} إلى {move_to.strftime('%d/%m')}",
                risk, [{"key": f"obligation:{ob.id}:{d.isoformat()}", "move_to": move_to.isoformat()}]))
    return out


def _financing(inp: Inputs, sf: Shortfall) -> Candidate:
    unit = Money.from_decimal(1000, inp.currency).amount_minor
    amount = -(-sf.gap_minor // unit) * unit  # round up to a whole thousand
    on = max(inp.start, sf.first_below - timedelta(days=1))
    return Candidate(
        "financing", "financing", None,
        f"Last resort: short-term overdraft of {_money(amount, inp.currency)} from {on.strftime('%d %b')}",
        f"كحل أخير: سحب على المكشوف قصير الأجل بقيمة {_money(amount, inp.currency, 'ar')} من {on.strftime('%d/%m')}",
        "high", [{"add": {"date": on.isoformat(), "amount_minor": amount, "kind": "financing", "key": "adjustment:financing",
                          "label": "short-term overdraft"}}])


def build_plan(inp: Inputs, base: Projection, sf: Shortfall) -> Plan:
    """Simulate each candidate, rank by gap closed / risk, financing last."""
    scored: list[Candidate] = []
    for c in candidates(inp, base, sf):
        sim = project(inp, base.scenario, c.adjustments)
        c.simulated = sim
        c.impact_minor = min(sf.gap_minor, sim.lowest.closing - base.lowest.closing)
        if c.impact_minor > 0:
            scored.append(c)
    scored.sort(key=lambda c: (-c.impact_minor / RISK_SCORE[c.risk], c.type, c.description_en))
    chosen = scored[:MAX_ACTIONS]
    fin = _financing(inp, sf)
    fin.simulated = project(inp, base.scenario, fin.adjustments)
    fin.impact_minor = min(sf.gap_minor, fin.simulated.lowest.closing - base.lowest.closing)
    ranked = [*chosen, fin]
    for i, c in enumerate(ranked, start=1):
        c.rank = i
    combined = project(inp, base.scenario, [a for c in chosen for a in c.adjustments]) if chosen else base
    return Plan(sf, ranked, combined.lowest.closing)
