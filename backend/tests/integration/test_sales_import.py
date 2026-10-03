"""Sales CSV import and manual entry, and Stock endpoints."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core import clock, scheduler
from app.db.engine import read_session, write_session
from app.models.events import FeedOverride
from app.models.finance_master import Sale
from app.models.harness import Incident
from app.models.master import Item
from app.models.stock_ops import StockLevel
from app.seed import feed
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(api: Any) -> dict[str, Any]:
    info = await seed(start_date=START, history_days=42)
    await api.login("owner", PASSWORDS["owner"])
    return info


def _csv(rows: list[str]) -> bytes:
    return ("date,item,qty,amount,payment_method\n" + "\n".join(rows) + "\n").encode("utf-8")


async def test_valid_csv_with_arabic_names_and_digits_imports_and_deducts_stock(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    # Process one day so the imported date is "already run" and stock must be deducted now.
    day = START
    async with write_session() as s:
        s.add(FeedOverride(business_id=bid, date=day, overrides={"sales_multiplier": {}}))
    await scheduler.advance(bid, days=1)
    async with read_session() as s:
        milk = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Milk"))).scalar_one()
        before = (await s.execute(select(StockLevel.quantity).where(StockLevel.item_id == milk.id))).scalar_one()
    body = _csv(["03/10/2026,لاتيه,١٠,٧٥٠,cash", "03/10/2026,Espresso,4,180,card"])
    # 03/10/2026 already has seeded sales, use a fresh processed date.
    body = _csv([f"{day.strftime('%d/%m/%Y')},لاتيه,١٠,٧٥٠,cash"])
    async with write_session() as s:  # make the day empty so the import is its only data
        for sale in (await s.execute(select(Sale).where(Sale.business_id == bid, Sale.date == day))).scalars():
            await s.delete(sale)
    r = await api.client.post("/api/v1/sales/import", files={"file": ("sales.csv", body, "text/csv")})
    assert r.status_code == 201, r.text
    assert r.json()["imported"] == 1 and r.json()["errors"] == []
    async with read_session() as s:
        after = (await s.execute(select(StockLevel.quantity).where(StockLevel.item_id == milk.id))).scalar_one()
    assert before - after == 2  # 10 lattes x 0.2 L


async def test_bad_rows_are_reported_and_not_imported(api: Any, cafe: dict[str, Any]) -> None:
    today = clock.today()
    body = _csv([
        f"{today.strftime('%d/%m/%Y')},Unknown drink,1,10,cash",
        f"{(today + timedelta(days=5)).strftime('%d/%m/%Y')},Latte,1,75,cash",
        f"{today.strftime('%d/%m/%Y')},Latte,0,75,cash",
        f"{today.strftime('%d/%m/%Y')},Latte,1,75,bitcoin",
        f"{today.strftime('%d/%m/%Y')},Latte,2,150,card",
    ])
    r = await api.client.post("/api/v1/sales/import", files={"file": ("s.csv", body, "text/csv")})
    data = r.json()
    assert r.status_code == 201
    assert data["imported"] == 1
    reasons = [e["reason"] for e in data["errors"]]
    assert len(reasons) == 4
    assert any("unknown item" in x for x in reasons) and any("future" in x for x in reasons)
    assert any("greater than 0" in x for x in reasons) and any("payment method" in x for x in reasons)


async def test_same_file_twice_is_rejected(api: Any, cafe: dict[str, Any]) -> None:
    body = _csv([f"{clock.today().strftime('%d/%m/%Y')},Latte,3,225,cash"])
    assert (await api.client.post("/api/v1/sales/import", files={"file": ("a.csv", body, "text/csv")})).status_code == 201
    r = await api.client.post("/api/v1/sales/import", files={"file": ("a.csv", body, "text/csv")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "file_already_imported"


async def test_manual_entry_for_gap_day_resolves_sales_gap_incident(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    gap_day = START
    # No POS data for START.
    async with write_session() as s:
        s.add(Sale(business_id=bid, date=gap_day - timedelta(days=400), lines=[], amount_total=__import__(
            "app.db.types", fromlist=["Money"]).Money(0, "EGP"), payment_method="cash", source="manual"))
    from unittest.mock import patch

    async def no_sales(*a: Any, **k: Any) -> int:
        return 0

    with patch.object(feed, "feed_sales", no_sales):
        from app.graphs import daily_run

        daily_run.clear_steps()
        from app import wiring

        wiring.register_all()
        # Re-register the import step with the patched function.
        daily_run._steps["import_sales"] = [("patched", no_sales)]
        await scheduler.advance(bid, days=1)
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.business_id == bid,
                                                      Incident.type == "sales_data_gap"))).scalars().all()
        latte = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Latte"))).scalar_one()
    assert inc and inc[0].status == "open"
    r = await api.client.post("/api/v1/sales/manual", json={"date": gap_day.isoformat(), "payment_method": "cash",
                                                            "lines": [{"item_id": str(latte.id), "qty": 30, "amount": 2250}]})
    assert r.status_code == 201, r.text
    async with read_session() as s:
        inc = await s.get(Incident, inc[0].id)
    assert inc.status == "resolved"


async def test_demo_feed_skips_a_date_with_imported_sales(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    day = clock.today()
    async with write_session() as s:  # start from a day with no sales yet
        for sale in (await s.execute(select(Sale).where(Sale.business_id == bid, Sale.date == day))).scalars():
            await s.delete(sale)
    body = _csv([f"{day.strftime('%d/%m/%Y')},Latte,5,375,cash"])
    await api.client.post("/api/v1/sales/import", files={"file": ("f.csv", body, "text/csv")})
    assert await feed.feed_sales(bid, day) == 0
    async with read_session() as s:
        sources = {r.source for r in (await s.execute(select(Sale).where(Sale.business_id == bid, Sale.date == day))).scalars()}
    assert sources == {"csv_upload"}


async def test_stock_items_hide_costs_from_staff(api: Any, cafe: dict[str, Any]) -> None:
    r = await api.client.get("/api/v1/stock/items")
    assert r.status_code == 200 and "unit_cost" in r.json()["items"][0]
    assert "data_as_of" in r.json()
    await api.client.post("/api/v1/auth/logout")
    await api.login("staff", PASSWORDS["staff"])
    r = await api.client.get("/api/v1/stock/items")
    assert r.status_code == 200 and "unit_cost" not in r.json()["items"][0]
    assert (await api.client.get("/api/v1/sales?date=2026-10-01")).status_code == 403
