"""Safety guard: no assistant can move money or hold bank credentials, and only
actions classed `irreversible_external` may reach outside the system.

Static checks over the source tree and the model metadata, so a future change that adds a payment
call, a bank login field or an unguarded outbound send fails here.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import app.models as models
from app.harness.action_spec import all_specs

APP = Path(__file__).resolve().parents[2] / "app"
SOURCES = {p: p.read_text(encoding="utf-8") for p in APP.rglob("*.py")}

# Network clients, and where each is allowed: Claude (LLM client) and the owner's own Telegram chat.
NETWORK_MODULES = {"httpx", "requests", "aiohttp", "urllib.request", "smtplib", "socket", "anthropic", "telegram",
                   "telegram.ext"}
ALLOWED_NETWORK = {"anthropic": {"app/llm/client.py"}, "telegram": {"app/approvals/telegram_bot.py"},
                   "telegram.ext": {"app/approvals/telegram_bot.py"}}
PAYMENT_WORDS = re.compile(
    r"\b(stripe|paypal|paymob|fawry|braintree|adyen|initiate_payment|create_payment|transfer_funds|payout|"
    r"execute_payment|make_payment|send_money|wire_transfer)\b", re.IGNORECASE)
CREDENTIAL_COLUMN = re.compile(r"(password|passcode|\bpin\b|secret|credential|api_key|access_token|refresh_token|"
                               r"card_number|cvv|online_banking|bank_login|bank_username)", re.IGNORECASE)
ALLOWED_CREDENTIAL_COLUMNS = {("user", "password_hash")}  # the dashboard login, hashed


def _rel(p: Path) -> str:
    return p.relative_to(APP.parent).as_posix()


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_no_module_calls_a_payment_or_bank_api() -> None:
    offenders = []
    for path, text in SOURCES.items():
        for mod in _imports(ast.parse(text)):
            root = mod if mod in NETWORK_MODULES else mod.split(".")[0]
            if root in NETWORK_MODULES and _rel(path) not in ALLOWED_NETWORK.get(root, set()):
                offenders.append(f"{_rel(path)} imports {mod}")
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())  # comments may say "no payout"
        for m in PAYMENT_WORDS.finditer(code):
            offenders.append(f"{_rel(path)} mentions {m.group(0)}")
    assert offenders == []


def test_no_model_stores_bank_credentials() -> None:
    found = [(t.name, c.name) for t in models.metadata.sorted_tables for c in t.columns
             if CREDENTIAL_COLUMN.search(c.name) and (t.name, c.name) not in ALLOWED_CREDENTIAL_COLUMNS]
    assert found == []
    bank = models.metadata.tables["bank_account"]
    assert {c.name for c in bank.columns} <= {"id", "business_id", "created_at", "updated_at", "name", "bank", "currency",
                                              "is_cash_on_hand", "ledger_account_code"}


def test_only_irreversible_external_actions_send_outside() -> None:
    from app import wiring

    wiring.register_all()
    specs = all_specs()
    assert {"send_po", "send_reminder"} <= set(specs)
    for name, spec in specs.items():
        source = inspect.getsource(spec.execute)
        sends = name.startswith("send_") or "sent_at" in source or "send_message" in source
        if sends:
            assert spec.risk_class == "irreversible_external", f"{name} sends outside but is {spec.risk_class}"
    # The Telegram send itself lives only in the owner-facing notifier (the business's own users).
    senders = [_rel(p) for p, text in SOURCES.items() if ".send_message(" in text]
    assert senders == ["app/approvals/telegram_bot.py"]


def test_assistants_only_recommend_payments() -> None:
    """Payment plans and schedules are data for the owner; nothing marks a bill as paid by itself.

    The one place a record becomes "paid" is a customer invoice whose payment already arrived in the
    bank (money the customer moved, matched by the Accountant).
    """
    marks = {_rel(p) for p, text in SOURCES.items() if re.search(r"\.status\s*=\s*\"paid\"", text)}
    assert marks <= {"app/agents/accountant/receivables.py"}
