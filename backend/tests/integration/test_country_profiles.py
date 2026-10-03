"""Configurable country profiles, default Egypt."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.core.country_profiles import calendar_for
from app.db.engine import read_session
from app.models.harness import AuditLogEntry
from app.models.master import Item
from app.models.tenancy import Business
from app.seed import feed
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)


async def test_default_seed_is_egypt_egp_vat_14(api: Any) -> None:
    await seed(start_date=START, history_days=3)
    await api.login("owner", PASSWORDS["owner"])
    r = await api.client.get("/api/v1/business")
    body = r.json()
    assert r.status_code == 200
    assert body["country"] == "EG" and body["currency"] == "EGP" and body["decimals"] == 2
    assert Decimal(body["vat_rate_percent"]) == Decimal("14")
    assert body["weekend_days"] == [5, 6]
    names = {h["name_en"] for h in body["holidays"]}
    assert "Armed Forces Day" in names and "Sham El-Nessim" in names
    assert body["min_cash_buffer"]["display"] == "EGP 50,000.00"


async def test_vat_config_override_applies_to_new_business(db: None, monkeypatch: Any) -> None:
    monkeypatch.setattr(get_settings(), "DEFAULT_VAT_RATE_PERCENT", Decimal("10"))
    info = await seed(start_date=START, history_days=1)
    async with read_session() as s:
        b = await s.get(Business, info["business_id"])
    assert b is not None and b.vat_rate_percent == Decimal("10")


async def test_oman_profile_uses_omr_three_decimals_and_scaled_prices(db: None) -> None:
    info = await seed(country="OM", start_date=START, history_days=1)
    assert info["currency"] == "OMR"
    async with read_session() as s:
        latte = (await s.execute(select(Item).where(Item.business_id == info["business_id"], Item.name_en == "Latte"))).scalar_one()
        b = await s.get(Business, info["business_id"])
    assert latte.sale_price.currency == "OMR" and latte.sale_price.decimals == 3
    assert latte.sale_price == latte.sale_price.from_decimal(Decimal("75") * Decimal("0.0077"), "OMR")
    assert b is not None and b.vat_rate_percent == Decimal("5")


async def test_currency_change_refused_once_financial_records_exist(api: Any) -> None:
    await seed(start_date=START, history_days=2)
    await api.login("owner", PASSWORDS["owner"])
    r = await api.client.patch("/api/v1/business", json={"currency": "USD"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "currency_locked"


async def test_vat_rate_validation_and_audit(api: Any) -> None:
    info = await seed(start_date=START, history_days=1)
    await api.login("owner", PASSWORDS["owner"])
    assert (await api.client.patch("/api/v1/business", json={"vat_rate_percent": 101})).status_code == 422
    r = await api.client.patch("/api/v1/business", json={"vat_rate_percent": "10.5"})
    assert r.status_code == 200 and Decimal(r.json()["vat_rate_percent"]) == Decimal("10.5")
    async with read_session() as s:
        ev = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.business_id == info["business_id"],
                                                          AuditLogEntry.event == "setting_changed"))).scalars().all()
    assert ev


async def test_manager_cannot_change_business(api: Any) -> None:
    await seed(start_date=START, history_days=1)
    await api.login("manager", PASSWORDS["manager"])
    assert (await api.client.patch("/api/v1/business", json={"vat_rate_percent": 10})).status_code == 403


async def test_coptic_christmas_gets_holiday_uplift(db: None) -> None:
    info = await seed(start_date=START, history_days=1)
    async with read_session() as s:
        b = await s.get(Business, info["business_id"])
    assert b is not None
    cal = calendar_for(b)
    xmas, normal = date(2027, 1, 7), date(2027, 1, 5)  # both Thursday/Tuesday, not weekends
    assert cal.is_holiday(xmas) and not cal.is_holiday(normal)
    assert feed.demand_factor(cal, xmas, "food") > feed.demand_factor(cal, normal, "food")
