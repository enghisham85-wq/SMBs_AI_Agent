"""Expense classification and recurring-correction detection (T077, FR-039)."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.core import clock
from app.db.engine import read_session, write_session
from app.harness import rules
from app.harness.incidents import open_incident
from app.llm.client import LLMRefusalError, LLMUnavailableError, MissingFixtureError, get_llm, text_block
from app.llm.schemas import ExpenseClassification
from app.models.books import ClassificationCorrection, PayableInvoice
from app.models.finance_master import Account
from app.models.master import Supplier

_PROMPT = (Path(__file__).resolve().parents[2] / "llm" / "prompts" / "classification.md").read_text(encoding="utf-8")
RECURRING_COUNT = 3
RECURRING_WINDOW_DAYS = 90
KEYWORDS: list[tuple[tuple[str, ...], str, float]] = [
    (("cup", "packag", "box", "bag", "كوب", "تغليف", "علب"), "5120", 0.85),
    (("rent", "إيجار"), "5200", 0.9),
    (("salary", "wage", "راتب", "رواتب"), "5300", 0.9),
    (("electric", "water bill", "gas", "كهرباء", "مياه"), "5400", 0.8),
    (("subscription", "software", "internet", "pos", "اشتراك", "انترنت"), "5500", 0.85),
    (("repair", "maintenance", "fix", "إصلاح", "صيانة"), "5600", 0.85),
    (("fee", "commission", "charge", "عمولة", "رسوم"), "5700", 0.85),
]


@dataclass
class Classification:
    account_code: str
    confidence: float
    source: str  # rule | history | llm | offline
    rule_id: str | None = None
    rationale: str = ""


def _keyword_guess(text: str) -> tuple[str, float]:
    low = text.lower()
    for words, code, conf in KEYWORDS:
        if any(w in low for w in words):
            return code, conf
    return "5900", 0.45


async def classify(business_id: uuid.UUID, supplier_id: uuid.UUID | None, description: str) -> Classification:
    # 1. Active classification rules for this supplier.
    if supplier_id is not None:
        for r in await rules.active_rules(business_id, "accountant", "classification"):
            if str(r.trigger.get("supplier_id")) == str(supplier_id):
                await rules.mark_applied(r.id)
                return Classification(r.trigger["account_code"], 0.99, "rule", str(r.id), r.rule_text_en)
    async with read_session() as s:
        accts = {a.code: a for a in (await s.execute(select(Account).where(Account.business_id == business_id))).scalars()}
        # 2. Supplier history: accounts used on this supplier's posted non-stock lines.
        history: Counter[str] = Counter()
        if supplier_id is not None:
            for inv in (await s.execute(select(PayableInvoice).where(PayableInvoice.supplier_id == supplier_id,
                                                                     PayableInvoice.status.in_(("posted", "paid"))))).scalars():
                for ln in inv.lines:
                    if not ln.get("item_id") and ln.get("account_code"):
                        history[ln["account_code"]] += 1
        sup = await s.get(Supplier, supplier_id) if supplier_id else None
    if history:
        code, n = history.most_common(1)[0]
        if n >= 2 and n / sum(history.values()) >= 0.8:
            return Classification(code, 0.9, "history", rationale=f"used {n} times for this supplier")
    # 3. Model (offline: keywords).
    guess_code, guess_conf = _keyword_guess(f"{sup.name_en if sup else ''} {description}")
    chart = "\n".join(f"{a.code} {a.name_en}" for a in accts.values() if a.type == "expense")
    content = [text_block(json.dumps({"supplier": sup.name_en if sup else None, "description": description,
                                      "history": dict(history)}, ensure_ascii=False) + "\n\nChart of accounts:\n" + chart)]
    llm = get_llm()
    try:
        out = await llm.parse("classification", _PROMPT, content, ExpenseClassification,
                              offline=lambda: ExpenseClassification(account_code=guess_code, confidence=guess_conf,
                                                                    rationale="matched keywords in the description"))
    except (LLMRefusalError, LLMUnavailableError, MissingFixtureError):
        out = ExpenseClassification(account_code=guess_code, confidence=min(guess_conf, 0.5), rationale="model unavailable")
    if out.account_code not in accts:
        return Classification("5900", 0.3, "llm", rationale="suggested account does not exist")
    return Classification(out.account_code, out.confidence, "offline" if llm.mode == "offline" else "llm",
                          rationale=out.rationale)


async def record_correction(business_id: uuid.UUID, supplier_id: uuid.UUID | None, from_code: str | None, to_code: str,
                            user_id: uuid.UUID | None, source_ref: str) -> uuid.UUID | None:
    """Store an owner override; at 3 identical corrections in 90 days open a `recurring_correction` incident
    and propose a classification rule. Returns the incident id when one is opened."""
    today = clock.today()
    async with write_session() as s:
        accts = {a.code: a for a in (await s.execute(select(Account).where(Account.business_id == business_id))).scalars()}
        s.add(ClassificationCorrection(business_id=business_id, supplier_id=supplier_id,
                                       from_account_id=accts[from_code].id if from_code in accts else None,
                                       to_account_id=accts[to_code].id, corrected_by=user_id, date=today,
                                       source_ref=source_ref[:120]))
    if supplier_id is None or from_code == to_code:
        return None
    async with read_session() as s:
        count = (await s.execute(select(func.count()).select_from(ClassificationCorrection).where(
            ClassificationCorrection.supplier_id == supplier_id, ClassificationCorrection.to_account_id == accts[to_code].id,
            ClassificationCorrection.date >= today - timedelta(days=RECURRING_WINDOW_DAYS)))).scalar_one()
        sup = await s.get(Supplier, supplier_id)
    if count < RECURRING_COUNT:
        return None
    pair = {"supplier_id": str(supplier_id), "account_code": to_code}
    if await rules.recent_for_trigger(business_id, "classification", pair, ("active",)):
        return None  # a rule already covers it
    if await rules.recent_for_trigger(business_id, "classification", pair, ("proposed",)):
        return None  # a proposal is pending
    if await rules.recent_for_trigger(business_id, "classification", pair, ("rejected",), RECURRING_WINDOW_DAYS):
        return None  # the owner rejected this recently
    acct = accts[to_code]
    inc_id = await open_incident(
        business_id=business_id, agent="accountant", type="recurring_correction", detected_by="classification_corrections",
        summary=f"{sup.name_en if sup else 'Supplier'} was corrected to {acct.name_en} ({to_code}) {count} times in {RECURRING_WINDOW_DAYS} days",
        refs={**pair, "count": count, "supplier_en": sup.name_en if sup else "", "supplier_ar": sup.name_ar if sup else "",
              "account_name_en": acct.name_en, "account_name_ar": acct.name_ar},
        dedupe_key=f"recurring_correction:{supplier_id}:{to_code}", action_taken="proposing a classification rule")
    from app.harness.analysis import analyse_and_propose

    await analyse_and_propose(inc_id)
    return inc_id
