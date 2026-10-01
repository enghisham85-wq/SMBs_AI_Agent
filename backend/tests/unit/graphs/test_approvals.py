"""ApprovalService (T049): first answer wins, role refusal, restart survival, concurrent writers."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.approvals import service as approvals
from app.db.engine import get_engine, init_engine, read_session, write_session
from app.graphs import runtime
from app.harness.graph import run_action
from app.models.harness import ApprovalRequest, AuditLogEntry
from tests.unit.graphs import test_harness_graph as specs  # registers the fake specs


async def _pending(action_id: uuid.UUID) -> ApprovalRequest:
    async with read_session() as s:
        return (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == action_id,
                                                              ApprovalRequest.status == "pending"))).scalar_one()


async def test_dashboard_and_telegram_answer_at_once_first_wins(business: dict[str, Any], actor: Any) -> None:
    specs.calls.clear()
    out = await run_action("t_send", {"tag": "race"}, business["id"])
    req = await _pending(out["action_id"])
    r1, r2 = await asyncio.gather(
        approvals.resolve(str(req.id), "approve", actor("owner"), "dashboard"),
        approvals.resolve(req.request_token, "approve", actor("manager"), "telegram"),
    )
    statuses = sorted([r1.status, r2.status])
    assert statuses == ["already_resolved", "resolved"]
    assert specs.calls["execute:race"] == 1  # the graph resumed exactly once
    loser = r1 if r1.status == "already_resolved" else r2
    assert loser.request is not None and loser.request["resolved_via"] in ("dashboard", "telegram")


async def test_staff_cannot_answer_manager_request(business: dict[str, Any], actor: Any) -> None:
    out = await run_action("t_send", {"tag": "role"}, business["id"])
    req = await _pending(out["action_id"])
    res = await approvals.resolve(str(req.id), "approve", actor("staff"), "telegram")
    assert res.status == "permission_denied"
    async with read_session() as s:
        denied = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.event == "permission_denied"))).scalars().all()
    assert len(denied) == 1
    assert (await _pending(out["action_id"])).status == "pending"


async def test_checkpoint_survives_runtime_restart(business: dict[str, Any], actor: Any, tmp_path: Path,
                                                   monkeypatch: Any) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CHECKPOINT_DB_PATH", str(tmp_path / "cp.db"))
    await runtime.init()  # file-based AsyncSqliteSaver
    from app.harness import graph as harness_graph

    harness_graph.register()
    specs.calls.clear()
    out = await run_action("t_send", {"tag": "restart"}, business["id"])
    assert out["interrupted"]
    await runtime.close()

    await runtime.init()  # "restart": new saver on the same file
    harness_graph.register()
    req = await _pending(out["action_id"])
    res = await approvals.resolve(str(req.id), "approve", actor("owner"), "dashboard")
    assert res.status == "resolved"
    assert specs.calls["execute:restart"] == 1
    await runtime.close()


async def test_timeout_applies_safe_default_and_reasks_with_higher_urgency(business: dict[str, Any]) -> None:
    from datetime import timedelta

    from app.core import clock

    specs.calls.clear()
    out = await run_action("t_send", {"tag": "timeout"}, business["id"])
    first = await _pending(out["action_id"])
    clock.set_state("simulated", clock.today() + timedelta(days=1))
    expired = await approvals.expire_due(business["id"])
    assert expired == 1
    second = await _pending(out["action_id"])
    assert second.id != first.id
    assert second.urgency == first.urgency + 1
    assert "execute:timeout" not in specs.calls  # nothing irreversible happened


async def test_concurrent_writers_on_file_sqlite_never_lock(tmp_path: Path, business: dict[str, Any],
                                                             actor: Any) -> None:
    """20 parallel writers (resolves, audits, event-like inserts) on a real SQLite file."""
    import app.models as models
    from app.harness.audit import add_audit
    from app.models.clock import BusinessClock
    from app.models.tenancy import Business, User

    # Move the whole test onto a file database.
    old = get_engine()
    init_engine(f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}")
    async with get_engine().begin() as conn:
        await conn.run_sync(models.metadata.create_all)
    async with write_session() as s:
        s.add(Business(id=business["id"], name="F", min_cash_buffer=__import__("app.db.types", fromlist=["Money"]).Money(0, "EGP")))
        await s.flush()
        for role, uid in business["users"].items():
            s.add(User(id=uid, business_id=business["id"], username=role, password_hash="x", role=role))
        from app.core import clock

        s.add(BusinessClock(business_id=business["id"], mode="simulated", current_date=clock.today()))

    specs.calls.clear()
    actions = [await run_action("t_send", {"tag": f"c{i}"}, business["id"]) for i in range(5)]
    reqs = [await _pending(a["action_id"]) for a in actions]

    async def audit_writer(i: int) -> None:
        async with write_session() as s:
            add_audit(s, "load_test", business_id=business["id"], inputs={"i": i})

    jobs: list[Any] = []
    for r in reqs:
        jobs.append(approvals.resolve(str(r.id), "approve", actor("owner"), "dashboard"))
        jobs.append(approvals.resolve(r.request_token, "approve", actor("manager"), "telegram"))
    jobs += [audit_writer(i) for i in range(10)]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == []
    async with read_session() as s:
        n = (await s.execute(select(func.count()).select_from(AuditLogEntry).where(AuditLogEntry.event == "load_test"))).scalar_one()
    assert n == 10  # no lost update
    assert sum(specs.calls.get(f"execute:c{i}", 0) for i in range(5)) == 5
    await get_engine().dispose()
    _ = old
