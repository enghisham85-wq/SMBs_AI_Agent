"""Login sessions, CSRF and role checks."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select

from app.config import get_settings
from app.core.errors import AppError
from app.db.engine import read_session
from app.harness.audit import audit
from app.models.tenancy import ROLE_RANK, User

SESSION_COOKIE = "sb_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_MAX_AGE = 60 * 60 * 12

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SESSION_SECRET, salt="sb-session")


def issue_session(user_id: uuid.UUID) -> tuple[str, str]:
    """Return (signed cookie value, csrf token)."""
    csrf = secrets.token_urlsafe(24)
    return _serializer().dumps({"uid": str(user_id), "csrf": csrf}), csrf


def read_session_cookie(value: str) -> dict[str, str] | None:
    try:
        data = _serializer().loads(value, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data if isinstance(data, dict) else None


def can(role: str, min_role: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[min_role]


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    business_id: uuid.UUID
    username: str
    role: str
    language: str
    csrf: str


async def current_user(request: Request) -> CurrentUser:
    raw = request.cookies.get(SESSION_COOKIE)
    data = read_session_cookie(raw) if raw else None
    if not data:
        raise AppError(401, "not_authenticated", message_en="Please log in.", message_ar="يرجى تسجيل الدخول.")
    async with read_session() as s:
        user = (
            await s.execute(select(User).where(User.id == uuid.UUID(data["uid"]), User.active.is_(True)))
        ).scalar_one_or_none()
    if user is None:
        raise AppError(401, "not_authenticated", message_en="Please log in.", message_ar="يرجى تسجيل الدخول.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != data.get("csrf"):
            raise AppError(403, "csrf_failed", message_en="Security token missing or invalid.", message_ar="رمز الأمان مفقود أو غير صالح.")
    return CurrentUser(user.id, user.business_id, user.username, user.role, user.language, data["csrf"])


async def deny(user: CurrentUser, what: str, min_role: str) -> AppError:
    await audit(
        "permission_denied",
        business_id=user.business_id,
        user_id=user.id,
        inputs={"what": what, "role": user.role, "required": min_role},
    )
    return AppError(403, "permission_denied")


def require_role(min_role: str) -> Callable[..., Awaitable[CurrentUser]]:
    """FastAPI dependency: the caller must have at least `min_role`; refusals are audited."""

    async def _dep(request: Request, user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not can(user.role, min_role):
            raise await deny(user, f"{request.method} {request.url.path}", min_role)
        return user

    return _dep


RequireStaff = Depends(require_role("staff"))
RequireManager = Depends(require_role("manager"))
RequireOwner = Depends(require_role("owner"))
