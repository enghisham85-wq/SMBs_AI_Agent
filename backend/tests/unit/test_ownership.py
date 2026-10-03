"""Ownership guard: each agent writes only its own tables; other agents change them
through logged events handled in that agent's handlers.py.

Two checks:
- static: outside handlers.py, an agent module never creates (`Model(...)`), deletes (`delete(Model)`)
  or bulk-updates (`update(Model)`) another agent's model. Reading another agent's records is allowed:
  the Cash-Flow forecast, for example, reads invoices and orders, so imports alone cannot be the test.
- runtime: while the milk end-to-end scenario runs, every flushed INSERT/UPDATE/DELETE on an owned
  table is made by the owning agent (harness nodes act as their action's agent, event handlers as
  their consuming agent).

Shared master data (Supplier, SupplierAlias, Account, BankAccount, users, settings) and harness,
event and audit tables are not owned by one agent.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.ownership import acting_agent
from app.models.books import (
    ClassificationCorrection,
    Document,
    Extraction,
    JournalEntry,
    JournalLine,
    PayableInvoice,
    ReceivableInvoice,
)
from app.models.cash import (
    CashForecast,
    CashForecastRun,
    PaymentPromise,
    PaymentReminder,
    PlanAction,
    PurchasingBudget,
    ShortfallPlan,
)
from app.models.finance_master import BankBalanceSnapshot, BankTransaction, Obligation, Sale
from app.models.master import Item, RecipeLine, SupplierPrice
from app.models.purchasing import Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine
from app.models.stock_ops import DemandForecast, StockCount, StockLevel, StockMovement

OWNERSHIP: dict[type, str] = {
    **{m: "stock" for m in (Item, RecipeLine, SupplierPrice, StockLevel, StockMovement, StockCount, DemandForecast,
                            PurchaseOrder, PurchaseOrderLine, Delivery, DeliveryLine, Sale)},
    **{m: "accountant" for m in (Document, Extraction, PayableInvoice, ReceivableInvoice, JournalEntry, JournalLine,
                                 ClassificationCorrection, BankTransaction, BankBalanceSnapshot)},
    **{m: "cashflow" for m in (CashForecastRun, CashForecast, ShortfallPlan, PlanAction, PurchasingBudget, PaymentReminder,
                               PaymentPromise, Obligation)},
}
BY_NAME = {m.__name__: a for m, a in OWNERSHIP.items()}
AGENTS_DIR = Path(__file__).resolve().parents[2] / "app" / "agents"
# Sample-data setup run by `seed` before any agent acts; not an agent write path.
SEED_FUNCTIONS = {"seed_books", "seed_opening_stock", "seed_cash_story"}


def _writes(tree: ast.AST) -> list[tuple[str, int, str]]:
    """(model name, line, how) for each create/delete/update of a model class, outside seed functions."""
    out: list[tuple[str, int, str]] = []

    class V(ast.NodeVisitor):
        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node.name not in SEED_FUNCTIONS:
                self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node.name not in SEED_FUNCTIONS:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            name = f.id if isinstance(f, ast.Name) else None
            if name in BY_NAME:
                out.append((name, node.lineno, "create"))
            if name in ("delete", "update") and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in BY_NAME:
                out.append((node.args[0].id, node.lineno, name))
            self.generic_visit(node)

    V().visit(tree)
    return out


def test_agents_only_create_or_delete_their_own_records() -> None:
    problems = []
    checked = 0
    for agent_dir in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")):
        agent = agent_dir.name
        for module in sorted(agent_dir.glob("*.py")):
            if module.name == "handlers.py":
                continue
            checked += 1
            for model, line, how in _writes(ast.parse(module.read_text(encoding="utf-8"))):
                if BY_NAME[model] != agent:
                    problems.append(f"{agent}/{module.name}:{line} {how}s {model} (owned by {BY_NAME[model]})")
    assert checked > 20
    assert not problems, "\n".join(problems)


@pytest.fixture
def flush_log() -> Any:
    log: list[dict[str, Any]] = []

    def before_flush(session: Session, flush_context: Any, instances: Any) -> None:
        agent = acting_agent()
        for kind, objs in (("insert", session.new), ("update", session.dirty), ("delete", session.deleted)):
            for obj in objs:
                owner = OWNERSHIP.get(type(obj))
                if owner is None:
                    continue
                log.append({"agent": agent, "owner": owner, "table": type(obj).__name__, "kind": kind})

    event.listen(Session, "before_flush", before_flush)
    yield log
    event.remove(Session, "before_flush", before_flush)


async def test_runtime_writes_stay_with_the_owning_agent(flush_log: list[dict[str, Any]], tmp_path: Path,
                                                         monkeypatch: Any, db: None) -> None:
    from tests.integration import test_milk_e2e as milk

    cafe = await milk.cafe.__wrapped__(db, tmp_path, monkeypatch)  # type: ignore[attr-defined]
    flush_log.clear()  # sample-data setup is not an agent write
    await milk.test_milk_end_to_end_across_three_agents(cafe)
    acted = [w for w in flush_log if w["agent"] is not None]
    assert {w["agent"] for w in acted} == {"stock", "cashflow", "accountant"}  # the scenario exercised every agent
    wrong = [w for w in acted if w["agent"] != w["owner"]]
    assert not wrong, wrong
