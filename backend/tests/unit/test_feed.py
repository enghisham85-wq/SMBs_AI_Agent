"""Demo data feed: deterministic, idempotent, seasonal, overridable."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core import clock
from app.core.country_profiles import calendar_for
from app.db.engine import read_session, write_session
from app.models.events import FeedOverride
from app.models.finance_master import BankTransaction, Sale
from app.models.master import Item
from app.models.tenancy import Business
from app.seed import feed
from app.seed.sample_cafe import seed

START = date(2026, 10, 4)


async def _seeded(db: None) -> Any:
    info = await seed(start_date=START, history_days=14)
    return info["business_id"]


async def _sales_on(bid: Any, d: date) -> list[Sale]:
    async with read_session() as s:
        return list((await s.execute(select(Sale).where(Sale.business_id == bid, Sale.date == d))).scalars())


async def test_same_date_twice_gives_identical_output(db: None) -> None:
    bid = await _seeded(db)
    async with read_session() as s:
        b = await s.get(Business, bid)
        products = list((await s.execute(select(Item).where(Item.business_id == bid, Item.is_sold.is_(True)))).scalars())
        from app.core import settings_store

        cfg = (await settings_store.get_all(bid, s))["demo_feed"]
    assert b is not None
    a1 = feed.generate_sales(b, products, cfg, START, {})
    a2 = feed.generate_sales(b, products, cfg, START, {})
    assert a1 == a2


async def test_advancing_7_days_creates_data_every_day_without_duplicates(db: None) -> None:
    bid = await _seeded(db)
    for i in range(7):
        d = START + timedelta(days=i)
        clock.set_state("simulated", d)
        await feed.feed_sales(bid, d)
        await feed.feed_bank(bid, d)
    # Same dates again: nothing new.
    for i in range(7):
        d = START + timedelta(days=i)
        assert await feed.feed_sales(bid, d) == 0
        assert await feed.feed_bank(bid, d) == 0
    for i in range(7):
        d = START + timedelta(days=i)
        assert len(await _sales_on(bid, d)) == 3  # cash, card, transfer
        async with read_session() as s:
            n = (await s.execute(select(func.count()).select_from(BankTransaction).where(
                BankTransaction.business_id == bid, BankTransaction.date == d))).scalar_one()
        assert n > 0


async def test_weekend_and_ramadan_show_uplift(db: None) -> None:
    bid = await _seeded(db)
    async with read_session() as s:
        b = await s.get(Business, bid)
    assert b is not None
    cal = calendar_for(b)
    friday = date(2026, 10, 9)
    tuesday = date(2026, 10, 6)
    assert cal.is_weekend(friday) and not cal.is_weekend(tuesday)
    assert feed.demand_factor(cal, friday, "drink") > feed.demand_factor(cal, tuesday, "drink")
    ramadan_day = date(2027, 2, 15)
    assert cal.is_ramadan(ramadan_day)
    assert feed.demand_factor(cal, ramadan_day, "drink") > feed.demand_factor(cal, ramadan_day, "food")


async def test_skip_bank_override_produces_no_bank_rows(db: None) -> None:
    bid = await _seeded(db)
    d = START + timedelta(days=2)
    async with write_session() as s:
        s.add(FeedOverride(business_id=bid, date=d, overrides={"skip_bank": True}))
    clock.set_state("simulated", d)
    await feed.feed_sales(bid, d)
    assert await feed.feed_bank(bid, d) == 0
    async with read_session() as s:
        n = (await s.execute(select(func.count()).select_from(BankTransaction).where(
            BankTransaction.business_id == bid, BankTransaction.date == d))).scalar_one()
    assert n == 0


async def test_sales_multiplier_override_raises_one_item(db: None) -> None:
    bid = await _seeded(db)
    async with read_session() as s:
        b = await s.get(Business, bid)
        latte = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Latte"))).scalar_one()
        products = list((await s.execute(select(Item).where(Item.business_id == bid, Item.is_sold.is_(True)))).scalars())
        from app.core import settings_store

        cfg = (await settings_store.get_all(bid, s))["demo_feed"]
    assert b is not None
    d = START + timedelta(days=1)

    def latte_qty(sales: list[dict[str, Any]]) -> int:
        return sum(line["qty"] for sale in sales for line in sale["lines"] if line["sold_item_id"] == str(latte.id))

    base = latte_qty(feed.generate_sales(b, products, cfg, d, {}))
    spiked = latte_qty(feed.generate_sales(b, products, cfg, d, {"sales_multiplier": {str(latte.id): 3}}))
    assert spiked >= base * 2.5
