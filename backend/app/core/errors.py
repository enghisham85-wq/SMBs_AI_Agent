"""Application errors rendered as {"error": {code, message_en, message_ar}}."""

from __future__ import annotations

from typing import Any

from app.core.i18n import CATALOG


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message_en: str | None = None,
        message_ar: str | None = None,
        extra: dict[str, Any] | None = None,
        **fmt: Any,
    ) -> None:
        entry = CATALOG.get(code, {})
        self.status = status
        self.code = code
        self.message_en = message_en or entry.get("en", code).format(**fmt)
        self.message_ar = message_ar or entry.get("ar", entry.get("en", code)).format(**fmt)
        self.extra = extra or {}
        super().__init__(self.message_en)

    def body(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message_en": self.message_en, "message_ar": self.message_ar}
        err.update(self.extra)
        return {"error": err}


def not_found(what: str = "") -> AppError:
    return AppError(404, "not_found", message_en=f"{what or 'Resource'} not found.")
