"""Business settings and country profiles (FR-051, contracts/rest-api.md "Business and country")."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.common import J, with_freshness
from app.core import clock, country_profiles
from app.core.auth import CurrentUser, RequireManager, RequireOwner
from app.core.errors import AppError
from app.db.currencies import exponent
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.audit import add_audit
from app.models.tenancy import Business

router = APIRouter(tags=["business"])


def _business_out(b: Business) -> dict[str, Any]:
    year = clock.today().year
    return {
        "id": b.id,
        "name": b.name,
        "country": b.country,
        "currency": b.currency,
        "decimals": exponent(b.currency),
        "vat_registered": b.vat_registered,
        "vat_rate_percent": b.vat_rate_percent,
        "vat_period": b.vat_period,
        "weekend_days": b.weekend_days,
        "tax_id_pattern": b.tax_id_pattern,
        "default_date_format": b.default_date_format,
        "min_cash_buffer": b.min_cash_buffer,
        "demo_mode": b.demo_mode,
        "holidays": [{"date": d, "name_en": n[0], "name_ar": n[1]}
                     for d, n in country_profiles.holidays(b.country, year).items()],
    }


async def has_financial_records(business_id: Any) -> bool:
    """Any Money-bearing record exists -> the currency is locked (no FX conversion in the MVP)."""
    from app.db.types import Base

    money_tables = [t for t in Base.metadata.sorted_tables
                    if t.name not in ("business", "setting", "item", "supplier_price")
                    and any(c.name.endswith("_currency") for c in t.columns) and "business_id" in t.columns]
    async with read_session() as s:
        for t in money_tables:
            n = (await s.execute(select(func.count()).select_from(t).where(t.c.business_id == business_id))).scalar_one()
            if n:
                return True
    return False


@router.get("/business")
async def get_business(user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        b = await s.get(Business, user.business_id)
    assert b is not None
    return J(with_freshness({**_business_out(b), "currency_locked": await has_financial_records(user.business_id)},
                            {"settings": b.created_at}))


@router.get("/countries")
async def countries(user: CurrentUser = RequireManager) -> Response:
    return J({"countries": [
        {"code": p.code, "name_en": p.name_en, "name_ar": p.name_ar, "currency": p.currency,
         "vat_rate_percent": p.vat_rate_percent, "vat_period": p.vat_period, "weekend_days": p.weekend_days}
        for p in country_profiles.available()
    ]})


class BusinessPatch(BaseModel):
    country: str | None = None
    currency: str | None = None
    vat_rate_percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    vat_period: Literal["monthly", "quarterly"] | None = None
    weekend_days: list[int] | None = None
    tax_id_pattern: str | None = None
    min_cash_buffer: Decimal | None = Field(default=None, ge=0)

    @field_validator("weekend_days")
    @classmethod
    def _days(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and (not v or any(d < 1 or d > 7 for d in v)):
            raise ValueError("weekend_days are ISO weekday numbers 1-7")
        return v

    @field_validator("tax_id_pattern")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is not None:
            exponent(v)
        return v.upper() if v else v


@router.patch("/business")
async def patch_business(body: BusinessPatch, user: CurrentUser = RequireOwner) -> Response:
    changes = body.model_dump(exclude_none=True)
    if "currency" in changes:
        async with read_session() as s:
            current = (await s.get(Business, user.business_id))
        assert current is not None
        if changes["currency"] != current.currency and await has_financial_records(user.business_id):
            raise AppError(409, "currency_locked")
    async with write_session() as s:
        b = await s.get(Business, user.business_id)
        assert b is not None
        if "country" in changes:
            try:
                profile = country_profiles.load(changes["country"])
            except KeyError as exc:
                raise AppError(422, "unknown_country", message_en=str(exc), message_ar="دولة غير معروفة.") from exc
            country_profiles.apply(b, profile, creating=False)
        if "currency" in changes and changes["currency"] != b.currency:
            # Allowed only while no financial record exists, so only the buffer needs re-expressing.
            buffer_value = b.min_cash_buffer.to_decimal()
            b.currency = changes["currency"]
            b.min_cash_buffer = Money.from_decimal(buffer_value, b.currency)
        for field in ("vat_rate_percent", "vat_period", "weekend_days", "tax_id_pattern"):
            if field in changes:
                setattr(b, field, changes[field])
        if "min_cash_buffer" in changes:
            b.min_cash_buffer = Money.from_decimal(changes["min_cash_buffer"], b.currency)
        add_audit(s, "setting_changed", business_id=user.business_id, user_id=user.id, inputs={"business": changes},
                  outputs={"effective_from": clock.today()})
        out = _business_out(b)
    return J(out)
