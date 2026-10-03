"""Event contract: docs/events.md vs the code."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core import events
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.models.events import Event, EventDelivery

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "docs" / "events.md"
APP = ROOT / "backend" / "app"
ENVELOPE = {"event_id", "type", "version", "business_id", "producer", "action_id", "occurred_at", "payload"}


def contract_rows() -> dict[str, set[str]]:
    """{event type: required fields} from the contract table, minus optional `x?` ones."""
    out: dict[str, set[str]] = {}
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].startswith("`"):
            continue
        types = re.findall(r"`([a-z_.]+)`", cells[0])
        fields_text = re.sub(r"\([^)]*\)", "", cells[3])
        fields = {f.strip() for f in fields_text.split(",") if f.strip() and not f.strip().endswith("?")}
        for t in types:
            out[t] = fields
    return out


def test_contract_and_code_agree_on_required_fields() -> None:
    rows = contract_rows()
    assert len(rows) >= 17
    for kind, fields in rows.items():
        assert kind in events.REQUIRED_FIELDS, f"{kind} is in the contract but not in REQUIRED_FIELDS"
        assert set(events.REQUIRED_FIELDS[kind]) == fields, kind
    assert set(events.REQUIRED_FIELDS) <= set(rows), "an event type is missing from docs/events.md"


def test_every_contracted_event_is_published_in_code() -> None:
    source = "\n".join(p.read_text(encoding="utf-8") for p in APP.rglob("*.py"))
    for kind in contract_rows():
        assert re.search(rf"""["']{re.escape(kind)}["']""", source), f"{kind} is never published"
        if kind.startswith(("rule.", "incident.")):
            continue  # published through a helper that picks the type
        assert re.search(rf"""publish\(\s*\w+,\s*["']{re.escape(kind)}["']""", source), f"no publish() call for {kind}"


def _payload(kind: str) -> dict[str, Any]:
    return {f: (Money(100, "EGP") if f in ("total", "amount") else f"v-{f}") for f in events.REQUIRED_FIELDS[kind]}


async def test_missing_field_is_refused_and_envelope_is_complete(business: dict[str, Any]) -> None:
    bid = business["id"]
    received: list[dict[str, Any]] = []

    async def handler(env: dict[str, Any]) -> None:
        received.append(env)

    for kind in events.REQUIRED_FIELDS:
        events.subscribe(kind, "contract_test", handler)
        full = _payload(kind)
        missing = dict(full)
        missing.pop(next(iter(full)))
        async with write_session() as s:
            with pytest.raises(events.EventPayloadError):
                events.publish(s, kind, missing, producer="harness", business_id=bid)
    for kind in events.REQUIRED_FIELDS:
        async with write_session() as s:
            events.publish(s, kind, _payload(kind), producer="harness", business_id=bid)
    await events.dispatch_pending()
    assert {e["type"] for e in received} == set(events.REQUIRED_FIELDS)
    for env in received:
        assert set(env) == ENVELOPE
        assert env["version"] == 1 and env["business_id"] == str(bid)
        assert set(events.REQUIRED_FIELDS[env["type"]]) <= set(env["payload"])
        if "total" in env["payload"]:
            assert env["payload"]["total"]["decimals"] == 2


async def test_redispatch_runs_each_handler_once(business: dict[str, Any]) -> None:
    bid = business["id"]
    calls: list[str] = []

    async def handler(env: dict[str, Any]) -> None:
        calls.append(env["event_id"])

    events.subscribe("price.changed", "contract_idem", handler)
    async with write_session() as s:
        events.publish(s, "price.changed", _payload("price.changed"), producer="stock", business_id=bid)
    await events.dispatch_pending()
    async with write_session() as s:  # simulate a crash before the event was marked dispatched
        for ev in (await s.execute(select(Event).where(Event.type == "price.changed"))).scalars():
            ev.dispatched = False
    await events.dispatch_pending()
    assert len(calls) == 1
    async with read_session() as s:
        deliveries = (await s.execute(select(EventDelivery).where(EventDelivery.consumer == "contract_idem"))).scalars().all()
    assert len(deliveries) == 1 and deliveries[0].status == "handled"


async def test_real_handler_is_idempotent_on_redispatch(api: Any, business: dict[str, Any]) -> None:
    """invoice.held only lowers reliability once."""
    from app.models.master import Supplier

    bid = business["id"]
    async with write_session() as s:
        sup = Supplier(business_id=bid, name_en="Delta Dairy", name_ar="دلتا", stated_lead_time_days=1, payment_terms_days=14,
                       reliability_score=0.9)
        s.add(sup)
        await s.flush()
        sid = sup.id
        events.publish(s, "invoice.held", {"invoice_id": uuid.uuid4(), "reason": ["three_way"], "supplier_id": sid},
                       producer="accountant", business_id=bid)
    async with write_session() as s:
        for ev in (await s.execute(select(Event).where(Event.type == "invoice.held"))).scalars():
            ev.dispatched = False
    await events.dispatch_pending()
    async with read_session() as s:
        assert (await s.get(Supplier, sid)).reliability_score == pytest.approx(0.85)  # type: ignore[union-attr]
