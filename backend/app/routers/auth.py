"""Auth: first-user bootstrap, login, rotating per-device refresh tokens, logout(-all), invite acceptance."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.deps import actor_for, client_ip, current_user, get_mailer, get_settings_dep
from app.domain.common import audit, get_org_settings, system_actor
from app.errors import ApiError, bad_request, conflict, unauthorized, unprocessable
from app.infra.ratelimit import RateLimiter
from app.models import Invite, Organization, PasswordReset, RefreshToken, User, new_id, utcnow
from app.security import (ahash_password, averify_password, create_access_token, hash_token, new_token, password_problem)

router = APIRouter(tags=["auth"])
_login_limiter: RateLimiter | None = None
_reset_limiter: RateLimiter | None = None
_signup_limiter: RateLimiter | None = None
log = logging.getLogger("calibrex.auth")


def _limiter(s: Settings) -> RateLimiter:
    global _login_limiter
    if _login_limiter is None or _login_limiter.max != s.login_max_attempts:
        _login_limiter = RateLimiter(s.login_max_attempts, s.login_window_seconds)
    return _login_limiter


def reset_login_limiter() -> None:  # tests
    global _login_limiter, _reset_limiter, _signup_limiter
    _login_limiter = _reset_limiter = _signup_limiter = None


def _password_reset_limiter() -> RateLimiter:
    global _reset_limiter
    if _reset_limiter is None:
        _reset_limiter = RateLimiter(3, 3600)
    return _reset_limiter


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)
    name: str = Field(min_length=1, max_length=200)
    org_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Literal["OWNER", "MANAGER", "STAFF", "AUDITOR"] = "OWNER"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank.")
        return value

    @field_validator("org_name")
    @classmethod
    def clean_org_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Workspace name cannot be blank.")
        return value


class AcceptBody(BaseModel):
    password: str = Field(max_length=256)
    name: str | None = Field(default=None, max_length=200)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(max_length=256)


def me_view(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}


def _set_cookie(resp: Response, s: Settings, raw: str) -> None:
    resp.set_cookie(s.refresh_cookie_name, raw, max_age=s.refresh_ttl_days * 86400, httponly=True, secure=bool(s.cookie_secure),
                    samesite=s.cookie_samesite, path=s.refresh_cookie_path)


async def _issue(db: AsyncSession, s: Settings, resp: Response, request: Request, user: User, *, family: str | None = None) -> dict:
    raw = new_token()
    rt = RefreshToken(user_id=user.id, family_id=family or new_id(), token_hash=hash_token(raw),
                      device=(request.headers.get("user-agent") or "")[:200], ip=client_ip(request),
                      expires_at=utcnow() + timedelta(days=s.refresh_ttl_days))
    db.add(rt)
    await db.flush()
    _set_cookie(resp, s, raw)
    return {"access_token": create_access_token(s, user.id, user.token_version), "token_type": "bearer",
            "expires_in": s.access_ttl_minutes * 60, "user": me_view(user)}


@router.post("/auth/register", status_code=201)
async def register(body: RegisterBody, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                   s: Settings = Depends(get_settings_dep)):
    """Only works while the system has no users: the first account becomes OWNER."""
    if (await db.scalar(select(func.count()).select_from(User))) > 0:
        raise ApiError(403, "Registration is closed. Ask your Owner for an invite.")
    if (p := password_problem(body.password)):
        raise unprocessable(p)
    org = await db.scalar(select(Organization).where(Organization.slug == "default"))
    try:
        if org is None:
            org = Organization(slug="default", name=(body.org_name or "My organization").strip())  # unique slug: a double-click race loses cleanly
            db.add(org)
            await db.flush()
        user = User(org_id=org.id, email=body.email.lower(), name=body.name.strip(), role="OWNER",
                    password_hash=await ahash_password(body.password))
        db.add(user)
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise conflict("Registration is closed. Ask your Owner for an invite.")
    await get_org_settings(db, org.id)
    audit(db, actor_for(request, user), "USER_REGISTERED", user.email, after="OWNER")
    out = await _issue(db, s, response, request, user)
    await db.commit()
    return out


@router.post("/auth/signup", status_code=201)
async def signup(body: RegisterBody, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                 s: Settings = Depends(get_settings_dep)):
    """Create a new supermarket workspace; the creator is its Owner and invites other roles."""
    global _signup_limiter
    if _signup_limiter is None:
        _signup_limiter = RateLimiter(5, 3600)
    if wait := _signup_limiter.hit(f"ip:{client_ip(request)}"):
        raise ApiError(429, f"Too many sign-up attempts. Try again in {wait}s.", headers={"Retry-After": str(wait)})
    if problem := password_problem(body.password):
        raise unprocessable(problem)
    org_name = (body.org_name or f"{body.name.strip()}'s workspace").strip()
    org = Organization(slug=f"workspace-{new_id()}", name=org_name[:200])
    db.add(org)
    try:
        await db.flush()
        # Public signup creates a new supermarket, so its creator is its Owner.
        # Other roles join an existing supermarket through its Owner's invitation.
        user = User(org_id=org.id, email=body.email.lower(), name=body.name.strip(), role="OWNER",
                    password_hash=await ahash_password(body.password))
        db.add(user)
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise conflict("An account with this email already exists.")
    await get_org_settings(db, org.id)
    audit(db, actor_for(request, user), "USER_REGISTERED", user.email, after="OWNER")
    out = await _issue(db, s, response, request, user)
    await db.commit()
    return out


@router.post("/auth/login")
async def login(body: Credentials, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                s: Settings = Depends(get_settings_dep)):
    email = body.email.lower()
    lim = _limiter(s)
    keys = (f"ip:{client_ip(request)}", f"em:{email}")
    for k in keys:
        if (wait := lim.hit(k)):
            raise ApiError(429, f"Too many sign-in attempts. Try again in {wait}s.", headers={"Retry-After": str(wait)})
    user = await db.scalar(select(User).where(User.email == email))
    ok = await averify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or not user.active or user.pending:
        raise unauthorized("Wrong email or password.")  # same message for every failure: no account enumeration
    lim.reset(f"em:{email}")
    audit(db, actor_for(request, user), "LOGIN", user.email)
    out = await _issue(db, s, response, request, user)
    await db.commit()
    return out


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db), s: Settings = Depends(get_settings_dep)):
    raw = request.cookies.get(s.refresh_cookie_name)
    if not raw:
        raise unauthorized("Session expired")
    rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)).with_for_update())
    if rt is None:
        raise unauthorized("Session expired")
    if rt.revoked_at is not None:
        # a rotated token being replayed means it leaked: kill the whole device family
        await db.execute(update(RefreshToken).where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=utcnow()))
        await db.commit()
        raise unauthorized("Session expired")
    user = await db.get(User, rt.user_id)
    if rt.expires_at < utcnow() or user is None or not user.active or user.pending:
        raise unauthorized("Session expired")
    out = await _issue(db, s, response, request, user, family=rt.family_id)
    rt.revoked_at = utcnow()
    rt.replaced_by = (await db.scalar(select(RefreshToken.id).where(RefreshToken.family_id == rt.family_id).order_by(RefreshToken.created_at.desc()).limit(1)))
    await db.commit()
    return out


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db), s: Settings = Depends(get_settings_dep)):
    raw = request.cookies.get(s.refresh_cookie_name)
    if raw:
        rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if rt:
            await db.execute(update(RefreshToken).where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                             .values(revoked_at=utcnow()))
            await db.commit()
    response.delete_cookie(s.refresh_cookie_name, path=s.refresh_cookie_path)


@router.post("/auth/logout-all", status_code=204)
async def logout_all(request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
                     s: Settings = Depends(get_settings_dep)):
    user.token_version += 1  # every access token dies on its next request
    await db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, actor_for(request, user), "LOGOUT_ALL", user.email)
    await db.commit()
    response.delete_cookie(s.refresh_cookie_name, path=s.refresh_cookie_path)


@router.get("/auth/me")
async def me(user: User = Depends(current_user)):
    return me_view(user)


@router.post("/auth/password/forgot", status_code=202)
async def request_password_reset(body: PasswordResetRequest, request: Request, db: AsyncSession = Depends(get_db),
                                 s: Settings = Depends(get_settings_dep), mailer=Depends(get_mailer)):
    """Always return the same response so callers cannot discover whether an account exists."""
    email = body.email.lower()
    limiter = _password_reset_limiter()
    for key in (f"ip:{client_ip(request)}", f"email:{email}"):
        if wait := limiter.hit(key):
            raise ApiError(429, f"Too many recovery requests. Try again in {wait}s.", headers={"Retry-After": str(wait)})
    user = await db.scalar(select(User).where(User.email == email, User.active.is_(True), User.pending.is_(False)))
    if user is not None:
        await db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
                         .values(used_at=utcnow()))
        raw = new_token()
        db.add(PasswordReset(user_id=user.id, token_hash=hash_token(raw), expires_at=utcnow() + timedelta(minutes=s.password_reset_ttl_minutes)))
        audit(db, actor_for(request, user), "PASSWORD_RESET_REQUESTED", user.email)
        await db.commit()
        link = f"{s.public_app_url.rstrip('/')}/#/reset-password/{raw}"
        try:
            await mailer.send(user.email, "Reset your Calibrex password",
                              f"Use this link to choose a new password: {link}\nThe link expires in {s.password_reset_ttl_minutes} minutes.\nIf you did not request this, ignore this message.")
        except Exception:
            # Do not expose whether an account exists or whether mail delivery succeeded.
            log.exception("password recovery email could not be delivered", extra={"user_id": user.id})
    else:
        await db.commit()
    return {"message": "If an active account matches that email, a password reset link has been sent."}


@router.post("/auth/password/reset", status_code=204)
async def reset_password(body: PasswordResetBody, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                         s: Settings = Depends(get_settings_dep)):
    if problem := password_problem(body.password):
        raise unprocessable(problem)
    reset = await db.scalar(select(PasswordReset).where(PasswordReset.token_hash == hash_token(body.token)).with_for_update())
    if reset is None or reset.used_at is not None or reset.expires_at < utcnow():
        raise bad_request("This password reset link is invalid or has expired.")
    user = await db.get(User, reset.user_id)
    if user is None or not user.active or user.pending:
        raise bad_request("This password reset link is invalid or has expired.")
    user.password_hash = await ahash_password(body.password)
    user.token_version += 1
    reset.used_at = utcnow()
    await db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.id != reset.id,
                                                PasswordReset.used_at.is_(None)).values(used_at=utcnow()))
    await db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                     .values(revoked_at=utcnow()))
    audit(db, actor_for(request, user), "PASSWORD_RESET_COMPLETED", user.email)
    await db.commit()
    response.delete_cookie(s.refresh_cookie_name, path=s.refresh_cookie_path)


@router.get("/invites/{token}")
async def invite_info(token: str, db: AsyncSession = Depends(get_db)):
    inv = await _valid_invite(db, token)
    user = await db.get(User, inv.user_id)
    out = {"email": inv.email, "role": inv.role}
    if user and user.active and not user.pending and user.org_id != inv.org_id:
        out["existing_account"] = True
    return out


async def _valid_invite(db: AsyncSession, token: str, lock: bool = False) -> Invite:
    q = select(Invite).where(Invite.token_hash == hash_token(token))
    inv = await db.scalar(q.with_for_update() if lock else q)
    if inv is None or inv.used_at is not None or inv.expires_at < utcnow():
        raise bad_request("This invite link is invalid or has expired.")
    return inv


@router.post("/invites/{token}/accept", status_code=201)
async def accept_invite(token: str, body: AcceptBody, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                        s: Settings = Depends(get_settings_dep)):
    inv = await _valid_invite(db, token, lock=True)
    user = await db.get(User, inv.user_id)
    if user is None:
        raise bad_request("This invite link is invalid or has expired.")
    if user.pending:
        if (p := password_problem(body.password)):
            raise unprocessable(p)
        user.password_hash, user.pending, user.active = await ahash_password(body.password), False, True
    else:
        if user.org_id == inv.org_id or user.role == "OWNER":
            raise bad_request("This invite cannot move this account between workspaces.")
        if not await averify_password(user.password_hash, body.password):
            raise unauthorized("Current password is incorrect.")
        # Membership changes only after the invited user proves account ownership.
        # Revoke old sessions so every request immediately uses the new workspace and role.
        old_org_id = user.org_id
        audit(db, actor_for(request, user), "WORKSPACE_MEMBERSHIP_TRANSFERRED", user.email,
              before=old_org_id, after=inv.org_id, org_id=old_org_id)
        await db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=utcnow()))
        user.org_id, user.role = inv.org_id, inv.role
        user.token_version += 1
    if body.name and body.name.strip():
        user.name = body.name.strip()
    inv.used_at = utcnow()
    audit(db, actor_for(request, user), "INVITE_ACCEPTED", user.email, after=user.role)
    out = await _issue(db, s, response, request, user)
    await db.commit()
    return out
