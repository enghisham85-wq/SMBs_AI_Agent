"""Shared fixtures: in-memory database, in-memory checkpointer, offline LLM, simulated clock."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
import pytest_asyncio

os.environ.setdefault("LLM_MODE", "offline")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("DEMO_MODE", "true")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.approvals import service as approvals  # noqa: E402
from app.core import clock, events  # noqa: E402
from app.core.auth import hash_password  # noqa: E402
from app.db.engine import get_engine, init_engine, write_session  # noqa: E402
from app.db.types import Money  # noqa: E402
from app.graphs import runtime  # noqa: E402
from app.llm.client import LLMClient, set_llm  # noqa: E402

TEST_DATE = date(2026, 10, 5)  # a Monday
PASSWORD = "demo-pass-123"


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[None]:
    import app.models as models

    # A real SQLite file, not :memory: — a shared in-memory connection lets one session's
    # rollback undo another's uncommitted write, which hides real concurrency behaviour.
    init_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with get_engine().begin() as conn:
        await conn.run_sync(models.metadata.create_all)
    events.clear_subscribers()
    approvals.clear_notifiers()
    set_llm(LLMClient(mode="offline"))
    await runtime.init(InMemorySaver())
    from app.harness import graph as harness_graph

    harness_graph.register()
    yield
    await get_engine().dispose()


@pytest.fixture
def clock_date() -> date:
    return TEST_DATE


@pytest_asyncio.fixture
async def business(db: None, clock_date: date) -> dict[str, Any]:
    """A business (Egypt defaults), three users and a simulated clock."""
    from app.models.clock import BusinessClock
    from app.models.tenancy import Business, User

    bid = uuid.uuid4()
    users: dict[str, uuid.UUID] = {}
    async with write_session() as s:
        s.add(Business(id=bid, name="Test Cafe", country="EG", currency="EGP",
                       min_cash_buffer=Money.from_decimal("50000", "EGP")))
        await s.flush()
        for role in ("owner", "manager", "staff"):
            uid = uuid.uuid4()
            users[role] = uid
            s.add(User(id=uid, business_id=bid, username=role, password_hash=hash_password(PASSWORD), role=role))
        s.add(BusinessClock(business_id=bid, mode="simulated", current_date=clock_date, last_run_date=clock_date))
    clock.set_state("simulated", clock_date)
    return {"id": bid, "users": users}


class Api:
    """httpx client against the ASGI app (no lifespan: fixtures already set up DB and runtime)."""

    def __init__(self) -> None:
        import httpx

        from app.main import create_app

        self.app = create_app()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def login(self, username: str, password: str = PASSWORD) -> Any:
        r = await self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        self.client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return r.json()

    async def close(self) -> None:
        await self.client.aclose()


@pytest_asyncio.fixture
async def api(db: None) -> AsyncIterator[Api]:
    from app import wiring

    wiring.register_all()
    a = Api()
    yield a
    await a.close()


class FakeActor:
    def __init__(self, uid: uuid.UUID, role: str) -> None:
        self.id = uid
        self.role = role
        self.username = role


@pytest.fixture
def actor(business: dict[str, Any]) -> Any:
    def _make(role: str) -> FakeActor:
        return FakeActor(business["users"][role], role)

    return _make
