"""Login, logout, current user and Telegram link codes."""

from __future__ import annotations

import asyncio
import secrets
import string
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.common import J, get_business
from app.config import get_settings
from app.core.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    CurrentUser,
    current_user,
    issue_session,
    read_session_cookie,
    verify_password,
)
from app.core.errors import AppError
from app.db.engine import read_session, write_session
from app.harness.audit import add_audit
from app.models.tenancy import User

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def _user_out(u: User | CurrentUser) -> dict[str, object]:
    return {"id": u.id, "username": u.username, "role": u.role, "language": u.language, "business_id": u.business_id}


async def _check_password(user: User, password: str) -> bool:
    # argon2 is deliberately slow; off the event loop it no longer stalls every other request.
    return await asyncio.to_thread(verify_password, user.password_hash, password)


@router.post("/auth/login")
async def login(body: LoginIn, response: Response) -> Response:
    # Usernames are unique per business only (uq business_id+username), so the same name may exist
    # in several businesses: the password decides which account, and a tie is refused rather than
    # logging into whichever row comes first.
    async with read_session() as s:
        candidates = (await s.execute(select(User).where(User.username == body.username, User.active.is_(True))
                                      .order_by(User.created_at))).scalars().all()
    matches = [u for u in candidates if await _check_password(u, body.password)]
    if len(matches) > 1:
        raise AppError(409, "ambiguous_login", message_en="This username and password match more than one business.",
                       message_ar="اسم المستخدم وكلمة المرور يطابقان أكثر من نشاط تجاري.")
    if not matches:
        raise AppError(401, "invalid_login", message_en="Wrong username or password.",
                       message_ar="اسم المستخدم أو كلمة المرور غير صحيحة.")
    user = matches[0]
    cookie, csrf = issue_session(user.id)
    resp = J({"user": _user_out(user), "csrf_token": csrf})
    resp.set_cookie(SESSION_COOKIE, cookie, max_age=SESSION_MAX_AGE, httponly=True, samesite="strict",
                    secure=get_settings().APP_ENV == "prod")  # local dev is plain http
    async with write_session() as s:
        add_audit(s, "login", business_id=user.business_id, user_id=user.id)
    return resp


@router.post("/auth/logout")
async def logout(user: CurrentUser = Depends(current_user)) -> Response:
    resp = J({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict", secure=get_settings().APP_ENV == "prod")
    return resp


@router.get("/me")
async def me(request: Request, user: CurrentUser = Depends(current_user)) -> Response:
    data = read_session_cookie(request.cookies.get(SESSION_COOKIE, "")) or {}
    b = await get_business(user.business_id)
    return J({
        "user": _user_out(user),
        "csrf_token": data.get("csrf"),
        "business": {"id": b.id, "name": b.name, "country": b.country, "currency": b.currency,
                     "decimals": __import__("app.db.currencies", fromlist=["exponent"]).exponent(b.currency),
                     "demo_mode": b.demo_mode},
    })


class MePatch(BaseModel):
    language: Literal["en", "ar"]


@router.patch("/me")
async def patch_me(body: MePatch, user: CurrentUser = Depends(current_user)) -> Response:
    async with write_session() as s:
        u = await s.get(User, user.id)
        assert u is not None
        u.language = body.language
    return J({"language": body.language})


@router.post("/me/telegram-link-code")
async def telegram_link_code(user: CurrentUser = Depends(current_user)) -> Response:
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    expires = datetime.now() + timedelta(minutes=15)
    async with write_session() as s:
        u = await s.get(User, user.id)
        assert u is not None
        u.telegram_link_code = code
        u.telegram_link_expires = expires
    return J({"code": code, "expires_at": expires, "instructions": f"Send /start {code} to the bot."})
