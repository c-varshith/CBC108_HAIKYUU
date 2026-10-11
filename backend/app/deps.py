"""FastAPI dependencies: settings/services from app.state, authentication, `require_permission`."""
from __future__ import annotations

from datetime import timedelta
from typing import Callable

import jwt
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.domain.common import Actor
from app.errors import forbidden, unauthorized
from app.models import User, utcnow
from app.permissions import can
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request):
    return request.app.state.storage


def get_inference(request: Request):
    return request.app.state.inference


def get_mailer(request: Request):
    return request.app.state.mailer


def get_runner(request: Request):
    return request.app.state.runner


def client_ip(request: Request) -> str | None:
    if request.app.state.settings.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


async def _load_user(request: Request, db: AsyncSession, token: str | None) -> User:
    if not token:
        raise unauthorized()
    try:
        claims = decode_access_token(request.app.state.settings, token)
    except jwt.ExpiredSignatureError:
        raise unauthorized("Session expired")
    except jwt.PyJWTError:
        raise unauthorized("Invalid token")
    user = await db.get(User, claims["sub"])
    # Role, active flag and token version are re-read on EVERY request: demotion/deactivation bite immediately.
    if user is None or not user.active or user.pending or claims.get("tv") != user.token_version:
        raise unauthorized("Session no longer valid")
    request.state.user_id = user.id
    if utcnow() - user.last_active > timedelta(minutes=5):  # throttled so reads don't become writes
        user.last_active = utcnow()
        await db.commit()
    return user


async def current_user(request: Request, db: AsyncSession = Depends(get_db),
                       creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    return await _load_user(request, db, creds.credentials if creds else None)


async def current_user_sse(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                           token: str | None = Query(default=None)) -> User:
    """EventSource cannot send headers, so the events endpoint (only) also accepts ?token=<access token>.
    Uses its own short session: a long-lived stream must not pin a pooled connection for its whole life."""
    from app.db import sessionmaker
    async with sessionmaker()() as db:
        return await _load_user(request, db, creds.credentials if creds else token)


def require_permission(cap: str) -> Callable:
    async def dep(user: User = Depends(current_user)) -> User:
        if not can(user.role, cap):
            raise forbidden()
        return user
    return dep


def actor_for(request: Request, user: User) -> Actor:
    return Actor.of(user, client_ip(request), request.headers.get("user-agent"))
