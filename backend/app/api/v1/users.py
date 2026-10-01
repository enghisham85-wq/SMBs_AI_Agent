"""User management (owner only)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.common import J
from app.core.auth import CurrentUser, RequireOwner, hash_password
from app.core.errors import AppError, not_found
from app.db.engine import read_session, write_session
from app.harness.audit import add_audit
from app.models.tenancy import User

router = APIRouter(tags=["users"])
Role = Literal["owner", "manager", "staff"]


def _out(u: User) -> dict[str, object]:
    return {"id": u.id, "username": u.username, "role": u.role, "language": u.language, "active": u.active,
            "telegram_linked": bool(u.telegram_chat_id)}


@router.get("/users")
async def list_users(user: CurrentUser = RequireOwner) -> Response:
    async with read_session() as s:
        rows = (await s.execute(select(User).where(User.business_id == user.business_id).order_by(User.username))).scalars()
        return J({"users": [_out(u) for u in rows]})


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8)
    role: Role
    language: Literal["en", "ar"] = "en"


@router.post("/users", status_code=201)
async def create_user(body: UserIn, user: CurrentUser = RequireOwner) -> Response:
    async with write_session() as s:
        exists = (await s.execute(select(User).where(User.business_id == user.business_id,
                                                     User.username == body.username))).scalar_one_or_none()
        if exists:
            raise AppError(409, "username_taken", message_en="That username is taken.", message_ar="اسم المستخدم مستخدم بالفعل.")
        u = User(business_id=user.business_id, username=body.username, password_hash=hash_password(body.password),
                 role=body.role, language=body.language)
        s.add(u)
        await s.flush()
        add_audit(s, "user_created", business_id=user.business_id, user_id=user.id, inputs={"username": body.username, "role": body.role})
        out = _out(u)
    return J(out, 201)


class UserPatch(BaseModel):
    role: Role | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


@router.patch("/users/{user_id}")
async def patch_user(user_id: uuid.UUID, body: UserPatch, user: CurrentUser = RequireOwner) -> Response:
    async with write_session() as s:
        u = await s.get(User, user_id)
        if u is None or u.business_id != user.business_id:
            raise not_found("User")
        if body.role is not None:
            u.role = body.role
        if body.active is not None:
            u.active = body.active
        if body.password:
            u.password_hash = hash_password(body.password)
        add_audit(s, "user_changed", business_id=user.business_id, user_id=user.id,
                  inputs={"target": user_id, **body.model_dump(exclude={"password"}, exclude_none=True)})
        out = _out(u)
    return J(out)
