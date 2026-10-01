"""API, event and startup hardening: upload limits, input checks, login across businesses, paging,
cheaper event dispatch, the currency-lock check, stale clock flags and the production secret guard.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core import clock, events, files
from app.db.engine import read_session, write_session
from app.db.types import Money
from tests.conftest import PASSWORD


async def _owner(api: Any) -> None:
    await api.login("owner")


# ------------------------------------------------------------------ uploads
async def test_upload_over_the_limit_is_413(api: Any, business: dict[str, Any], monkeypatch: Any) -> None:
    await _owner(api)
    monkeypatch.setattr(files, "MAX_BYTES", 10)
    r = await api.client.post("/api/v1/sales/import", files={"file": ("s.csv", b"x" * 100, "text/csv")})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "file_too_large" and r.json()["error"]["message_ar"]


async def test_oversized_content_length_is_refused_before_the_body_is_read() -> None:
    reached: list[bool] = []
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    async def receive() -> dict[str, Any]:
        raise AssertionError("the body must not be read")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/v1/documents", "headers": [
        (b"content-type", b"multipart/form-data; boundary=x"),
        (b"content-length", str(files.MAX_BYTES * 2).encode())]}
    await files.UploadLimit(app)(scope, receive, send)
    assert not reached and sent[0]["status"] == 413


async def test_invalid_sales_mapping_is_422(api: Any, business: dict[str, Any]) -> None:
    await _owner(api)
    for mapping in ("{not json", "[1, 2]"):
        r = await api.client.post("/api/v1/sales/import", data={"mapping": mapping},
                                  files={"file": ("s.csv", b"date,item,qty\n", "text/csv")})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_mapping"


# ------------------------------------------------------------------ login
async def test_login_picks_the_business_by_password_and_refuses_a_tie(api: Any, business: dict[str, Any]) -> None:
    from app.core.auth import hash_password
    from app.models.tenancy import Business, User

    other = uuid.uuid4()
    async with write_session() as s:
        s.add(Business(id=other, name="Other Cafe", country="EG", currency="EGP",
                       min_cash_buffer=Money.from_decimal("0", "EGP")))
        await s.flush()
        s.add(User(business_id=other, username="owner", password_hash=hash_password("other-pass-123"), role="owner"))
    r = await api.client.post("/api/v1/auth/login", json={"username": "owner", "password": "other-pass-123"})
    assert r.status_code == 200 and r.json()["user"]["business_id"] == str(other)
    r = await api.client.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["business_id"] == str(business["id"])
    assert "secure" not in r.headers["set-cookie"].lower()  # plain http outside production

    async with write_session() as s:
        u = (await s.execute(select(User).where(User.business_id == other))).scalar_one()
        u.password_hash = hash_password(PASSWORD)
    r = await api.client.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ambiguous_login"


def test_production_refuses_the_default_session_secret(monkeypatch: Any) -> None:
    from app.config import DEFAULT_SESSION_SECRET, InsecureSettingsError, get_settings

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("SESSION_SECRET", DEFAULT_SESSION_SECRET)
    with pytest.raises(InsecureSettingsError):
        get_settings.__wrapped__()  # type: ignore[attr-defined]
    monkeypatch.setenv("SESSION_SECRET", "a-private-random-value")
    assert get_settings.__wrapped__().APP_ENV == "prod"  # type: ignore[attr-defined]


# ------------------------------------------------------------------ paging and aggregates
async def test_reconciliation_pages_open_lines_but_counts_all(api: Any, business: dict[str, Any]) -> None:
    from app.api.v1 import books
    from app.models.finance_master import BankAccount, BankTransaction

    bid = business["id"]
    acct = uuid.uuid4()
    async with write_session() as s:
        s.add(BankAccount(id=acct, business_id=bid, name="Bank", bank="NBE", currency="EGP", ledger_account_code="1010"))
        await s.flush()
        for i, status in enumerate(["unmatched", "suggested", "unmatched", "confirmed", "auto_matched"]):
            s.add(BankTransaction(business_id=bid, account_id=acct, date=clock.today() - timedelta(days=i),
                                  amount=Money(1000 + i, "EGP"), description=f"t{i}", import_batch_id="b",
                                  match_status=status))
    summary = await books.reconciliation_summary(bid)
    assert (summary["total"], summary["matched"], summary["open_count"], summary["percent_matched"]) == (5, 2, 3, 40.0)
    await _owner(api)
    body = (await api.client.get("/api/v1/reconciliation?limit=2")).json()
    assert (body["total"], body["matched"], body["percent_matched"]) == (5, 2, 40.0)
    assert [t["description"] for t in body["open"]] == ["t0", "t1"]
    assert [t["description"] for t in (await api.client.get("/api/v1/reconciliation?limit=2&offset=2")).json()["open"]] == ["t2"]
    home = (await api.client.get("/api/v1/home")).json()
    assert home["health"]["books"]["unmatched"] == 3 and home["health"]["books"]["percent_reconciled"] == 40.0


async def test_currency_lock_follows_the_first_money_record(business: dict[str, Any]) -> None:
    from app.api.v1.business import has_financial_records
    from app.models.finance_master import Obligation

    bid = business["id"]
    assert not await has_financial_records(bid)
    async with write_session() as s:
        s.add(Obligation(business_id=bid, type="rent", description="Rent", amount=Money(100, "EGP"),
                         next_due_date=clock.today(), recurrence="monthly"))
    assert await has_financial_records(bid)


# ------------------------------------------------------------------ events
async def test_dispatch_is_inline_and_skipped_for_writes_without_events(business: dict[str, Any], monkeypatch: Any) -> None:
    from app.models.events import Event, EventDelivery

    seen: list[str] = []

    async def first(env: dict[str, Any]) -> None:
        seen.append("first")

    async def second(env: dict[str, Any]) -> None:
        seen.append("second")

    events.subscribe("budget.updated", "stock:t1", first)
    events.subscribe("budget.updated", "cashflow:t2", second)
    payload = {"week_start": date(2026, 10, 5), "amount": Money(1, "EGP"), "tightened": False, "reason": "t"}
    async with write_session() as s:
        events.publish(s, "budget.updated", payload, producer="cashflow", business_id=business["id"])
    assert seen == ["first", "second"]  # handlers ran before the publishing write returned
    async with read_session() as s:
        ev = (await s.execute(select(Event).where(Event.type == "budget.updated"))).scalar_one()
        deliveries = (await s.execute(select(EventDelivery).where(EventDelivery.event_id == ev.id))).scalars().all()
    assert ev.dispatched and sorted(d.consumer for d in deliveries) == ["cashflow:t2", "stock:t1"]

    calls: list[int] = []
    real = events.dispatch_pending

    async def counting() -> int:
        calls.append(1)
        return await real()

    monkeypatch.setattr(events, "dispatch_pending", counting)
    async with write_session() as s:
        s.add(Event(business_id=business["id"], type="noop", producer="t", payload={}, dispatched=True))
    assert calls == []  # nothing published: no outbox query
    async with write_session() as s:
        events.publish(s, "budget.updated", payload, producer="cashflow", business_id=business["id"])
    assert calls and seen == ["first", "second", "first", "second"]


# ------------------------------------------------------------------ clock
async def test_stale_advancing_flag_is_cleared_at_startup(business: dict[str, Any]) -> None:
    from app.models.clock import BusinessClock

    bid = business["id"]
    assert not await clock.clear_stale_advance(bid)
    async with write_session() as s:
        (await s.execute(select(BusinessClock).where(BusinessClock.business_id == bid))).scalar_one().advancing = True
    assert await clock.clear_stale_advance(bid)
    async with read_session() as s:
        assert not (await s.execute(select(BusinessClock).where(BusinessClock.business_id == bid))).scalar_one().advancing


async def test_huge_paging_values_are_clamped(api: Any, business: dict[str, Any]) -> None:
    await _owner(api)
    for q in ("offset=99999999999999999999", "limit=99999999999999999999", "limit=-99999999999999999999"):
        for path in ("/api/v1/reconciliation", "/api/v1/receivables", "/api/v1/harness/rules"):
            assert (await api.client.get(f"{path}?{q}")).status_code == 200, (path, q)


@pytest.mark.parametrize("pattern", [r"(\d+)+", r"(a|a*)*", r"^(\d{2,})*$", "\d" * 60])
def test_tax_id_pattern_refuses_catastrophic_or_overlong_regexes(pattern: str) -> None:
    from pydantic import ValidationError

    from app.api.v1.business import BusinessPatch

    with pytest.raises(ValidationError):
        BusinessPatch(tax_id_pattern=pattern)


@pytest.mark.parametrize("pattern", [r"^\d{9}$", r"^(\d{3}-){2}\d{3}$", r"^EG\d{9}$", r"^(\d{3})?\d{9}$"])
def test_tax_id_pattern_accepts_ordinary_formats(pattern: str) -> None:
    from app.api.v1.business import BusinessPatch

    assert BusinessPatch(tax_id_pattern=pattern).tax_id_pattern == pattern
