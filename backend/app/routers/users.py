"""Team administration (Owner): list, invite, change role, deactivate."""
from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.deps import actor_for, get_mailer, get_settings_dep, require_permission
from app.domain.common import audit, iso
from app.errors import conflict, not_found
from app.models import Invite, RefreshToken, User, utcnow
from app.security import hash_token, new_token

router = APIRouter(tags=["users"])
Role = Literal["OWNER", "MANAGER", "STAFF", "AUDITOR"]


def user_row(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role, "active": u.active, "pending": u.pending,
            "last_active": iso(u.last_active)}


class InviteBody(BaseModel):
    email: EmailStr
    role: Role
    name: str | None = Field(default=None, max_length=200)


class PatchUser(BaseModel):
    role: Role | None = None
    active: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)


async def _active_owners(db: AsyncSession, org_id: str) -> int:
    return await db.scalar(select(func.count()).select_from(User).where(User.org_id == org_id, User.role == "OWNER", User.active.is_(True), User.pending.is_(False)))


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db), me: User = Depends(require_permission("users.manage"))):
    rows = await db.scalars(select(User).where(User.org_id == me.org_id).order_by(User.created_at))
    return [user_row(u) for u in rows]


@router.post("/users/invite", status_code=201)
async def invite(body: InviteBody, request: Request, db: AsyncSession = Depends(get_db), me: User = Depends(require_permission("users.manage")),
                 s: Settings = Depends(get_settings_dep), mailer=Depends(get_mailer)):
    email = body.email.lower()
    existing = await db.scalar(select(User).where(User.email == email))
    moving_account = bool(existing and not existing.pending and existing.org_id != me.org_id)
    if existing and existing.pending and existing.org_id != me.org_id:
        raise conflict("That email already has a pending invite to another workspace.")
    if existing and not existing.pending and existing.org_id == me.org_id:
        raise conflict("This person is already a member of your workspace.")
    if moving_account and existing and existing.role == "OWNER":
        raise conflict("An Owner account cannot be moved. Use a separate team account.")
    user = existing or User(org_id=me.org_id, email=email, name=(body.name or email.split("@")[0]).strip(), role=body.role, active=False, pending=True)
    if existing is None:
        db.add(user)
        await db.flush()
    elif not moving_account:
        user.role = body.role
        if body.name:
            user.name = body.name.strip()
    await db.execute(update(Invite).where(Invite.user_id == user.id, Invite.used_at.is_(None)).values(used_at=utcnow()))  # old links die
    raw = new_token()
    db.add(Invite(org_id=me.org_id, user_id=user.id, email=email, role=body.role, token_hash=hash_token(raw),
                  expires_at=utcnow() + timedelta(hours=s.invite_ttl_hours), created_by=me.id))
    audit(db, actor_for(request, me), "USER_INVITED", email, after=body.role)
    await db.commit()
    link = f"{s.public_app_url.rstrip('/')}/#/invite/{raw}"
    await mailer.send(email, "You have been invited to Calibrex",
                      f"{me.name} invited you as {body.role.title()}.\nAccept the invite: {link}\nThe link expires in {s.invite_ttl_hours // 24} days.")
    out = user_row(user)
    if moving_account:
        out.update(role=body.role, active=False, pending=True, existing_account_move=True)
    if s.expose_invite_token:  # demo / e2e only (refused in production by config validation)
        out["invite_token"] = raw
    return out


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, body: PatchUser, request: Request, db: AsyncSession = Depends(get_db),
                     me: User = Depends(require_permission("users.manage"))):
    u = await db.scalar(select(User).where(User.id == user_id, User.org_id == me.org_id).with_for_update())
    if u is None:
        raise not_found("User not found.")
    if u.pending and body.active:
        raise conflict("This person has not accepted their invite yet.")
    new_role = body.role or u.role
    new_active = u.active if body.active is None else body.active
    loses_owner = u.role == "OWNER" and not u.pending and (new_role != "OWNER" or not new_active)
    if loses_owner and await _active_owners(db, me.org_id) <= 1:
        raise conflict("There must always be at least one active Owner.")
    changes = {}
    if body.role and body.role != u.role:
        changes["role"] = (u.role, body.role)
        u.role = body.role
    if body.active is not None and body.active != u.active:
        changes["active"] = (u.active, body.active)
        u.active = body.active
    if body.name:
        u.name = body.name.strip()
    if changes:
        u.token_version += 1  # demotion / deactivation takes effect on the very next request
        if not u.active:
            await db.execute(update(RefreshToken).where(RefreshToken.user_id == u.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
        audit(db, actor_for(request, me), "USER_UPDATED", u.email, before={k: v[0] for k, v in changes.items()},
              after={k: v[1] for k, v in changes.items()})
    await db.commit()
    return user_row(u)
