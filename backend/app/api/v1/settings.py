"""Per-business settings (thresholds, limits, auto-approval rules). Read: manager; write: owner, audited."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.api.common import J
from app.core import settings_store
from app.core.auth import CurrentUser, RequireManager, RequireOwner
from app.core.errors import AppError
from app.db.engine import write_session
from app.harness.audit import add_audit
from app.models.tenancy import SETTING_DEFAULTS

router = APIRouter(tags=["settings"])

# key -> (min, max); percentages are 0-100, confidences 0-1, money in minor units, hours > 0.
RANGES: dict[str, tuple[float, float]] = {
    "price_change_pct": (0, 100), "stock_variance_pct": (0, 100), "approval_timeout_hours": (0.1, 24 * 14),
    "journal_value_limit": (0, 10**15), "stale_bank_days": (0, 30), "dead_stock_days": (1, 365),
    "po_auto_approve_limit": (0, 10**15), "manual_bookkeeping_hours_per_week": (0, 168),
    "forecast_mape_threshold": (0, 5), "confidence_high": (0, 1), "confidence_low": (0, 1),
    "cash_variance_pct": (0, 100), "action_failure_threshold": (0, 1),
}
CHOICES: dict[str, tuple[str, ...]] = {"reminder_auto_approve": ("off", "polite_only")}
# Internal keys that are not user settings.
HIDDEN = {"demo_feed", "books_start_date"}


def _visible(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k not in HIDDEN}


@router.get("/settings")
async def get_settings_endpoint(user: CurrentUser = RequireManager) -> Response:
    return J({"settings": _visible(await settings_store.get_all(user.business_id))})


class SettingsPatch(BaseModel):
    values: dict[str, Any]


def _invalid(key: str, why: str) -> AppError:
    return AppError(422, "invalid_setting", message_en=f"{key}: {why}", message_ar=f"قيمة غير صالحة: {key}")


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, user: CurrentUser = RequireOwner) -> Response:
    unknown = set(body.values) - set(SETTING_DEFAULTS)
    if unknown:
        raise AppError(422, "unknown_setting", message_en=f"Unknown settings: {sorted(unknown)}", message_ar="إعدادات غير معروفة.")
    for k, v in body.values.items():
        if k in CHOICES and v not in CHOICES[k]:
            raise _invalid(k, f"must be one of {list(CHOICES[k])}")
        if k in RANGES:
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise _invalid(k, "must be a number")
            lo, hi = RANGES[k]
            if not lo <= v <= hi:
                raise _invalid(k, f"must be between {lo} and {hi}")
    merged = {**await settings_store.get_all(user.business_id), **body.values}
    if float(merged["confidence_low"]) >= float(merged["confidence_high"]):
        raise _invalid("confidence_low", "must be below confidence_high")
    async with write_session() as s:
        before = await settings_store.get_all(user.business_id, s)
        for k, v in body.values.items():
            await settings_store.put(s, user.business_id, k, v)
        add_audit(s, "setting_changed", business_id=user.business_id, user_id=user.id, inputs=body.values,
                  outputs={"previous": {k: before.get(k) for k in body.values}})
    return J({"settings": _visible(await settings_store.get_all(user.business_id))})
