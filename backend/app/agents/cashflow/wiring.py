"""Registers the Cash-Flow Agent: action specs, graphs, daily steps, events, budget provider and sample data."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.agents.cashflow import action_specs, graphs, projection
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.models.finance_master import BankAccount, BankBalanceSnapshot
from app.models.tenancy import Business

# The sample cafe is slowly losing cash: this month's rent + salaries week is set to dip this far below the buffer.
SAMPLE_GAP_SHARE_OF_BUFFER = 0.3


async def seed_cash_story(business_id: uuid.UUID, start: date, ids: dict[str, Any]) -> None:
    """Set the sample cafe's bank balance so the coming rent + salaries week falls below the minimum buffer.

    Every historic bank snapshot moves by the same amount (as if the cafe had opened with a different
    balance), and the books' opening entry gets the matching owner's-equity adjustment.
    """
    from app.agents.accountant import posting

    today = start - timedelta(days=1)
    async with read_session() as s:
        b = await s.get(Business, business_id)
        assert b is not None
        inp, _ = await projection.load_inputs(s, business_id, today)
        bank = (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id,
                                                          BankAccount.is_cash_on_hand.is_(False)).limit(1))).scalar_one_or_none()
    if bank is None or inp.buffer_minor <= 0:
        return
    low = projection.project(inp, "expected").lowest.closing
    target = int(inp.buffer_minor * (1 - SAMPLE_GAP_SHARE_OF_BUFFER))
    delta = target - low
    if delta == 0:
        return
    async with write_session() as s:
        for snap in (await s.execute(select(BankBalanceSnapshot).where(BankBalanceSnapshot.account_id == bank.id))).scalars():
            snap.balance = Money(snap.balance.amount_minor + delta, snap.balance.currency)
        from app.models.books import JournalEntry

        opening = (await s.execute(select(JournalEntry.id).where(JournalEntry.business_id == business_id,
                                                                 JournalEntry.reference_type == "opening_balance").limit(1))).first()
        if opening is not None:
            draft = posting.Draft(today, "opening_balance", f"{today.isoformat()}:adjustment", "Opening bank balance (sample data)")
            draft.dr(bank.ledger_account_code, delta).cr(posting.EQUITY, delta, "owner's equity")
            await posting.post(s, business_id, draft, b.currency, created_by="seed")


async def _budget_provider(business_id: uuid.UUID, d: date) -> int | None:
    return await action_specs.budget_remaining(business_id, d)


def register() -> None:
    from app.agents.stock.graphs import BUDGET_PROVIDER
    from app.approvals.service import register_question_handler
    from app.core.events import subscribe
    from app.seed.sample_cafe import register_extension

    action_specs.register_specs()
    graphs.register_graphs()
    subscribe("customer_payment.received", "cashflow", graphs.on_customer_payment)
    register_question_handler("obligation", graphs.on_obligation_answer)
    if _budget_provider not in BUDGET_PROVIDER:
        BUDGET_PROVIDER.append(_budget_provider)
    register_extension(seed_cash_story)
