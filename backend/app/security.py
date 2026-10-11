"""Password hashing, JWT access tokens, opaque refresh/invite tokens, signed media URLs."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings

_ph = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
_dummy_hash: str | None = None


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str | None, pw: str) -> bool:
    """Constant-ish time even when the user does not exist (hashed=None) so login can't enumerate emails."""
    global _dummy_hash
    if hashed is None:
        _dummy_hash = _dummy_hash or _ph.hash("calibrex-dummy-password")
        try:
            _ph.verify(_dummy_hash, pw)
        except (VerificationError, InvalidHashError):
            pass
        return False
    try:
        return _ph.verify(hashed, pw)
    except (VerificationError, InvalidHashError):
        return False


async def ahash_password(pw: str) -> str:
    return await asyncio.to_thread(hash_password, pw)


async def averify_password(hashed: str | None, pw: str) -> bool:
    return await asyncio.to_thread(verify_password, hashed, pw)


def password_problem(pw: str) -> str | None:
    """Same rules and wording as the client (lib/password.ts); the server enforces them too."""
    if len(pw) < 10:
        return "Use at least 10 characters."
    if len(pw) > 128:
        return "Use at most 128 characters."
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        return "Include at least one letter and one number."
    return None


# ---------------------------------------------------------------- JWT access tokens
def create_access_token(settings: Settings, user_id: str, token_version: int) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": user_id, "tv": token_version, "typ": "access", "iat": now,
              "exp": now + timedelta(minutes=settings.access_ttl_minutes)}
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(settings: Settings, token: str) -> dict:
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub"]})
    if claims.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return claims


# ---------------------------------------------------------------- opaque tokens (refresh, invite)
def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- signed media URLs
def _media_key(settings: Settings) -> bytes:
    return hmac.new(settings.jwt_secret.encode(), b"calibrex-media-v1", hashlib.sha256).digest()


def media_signature(settings: Settings, key: str, exp: int) -> str:
    return hmac.new(_media_key(settings), f"{key}\n{exp}".encode(), hashlib.sha256).hexdigest()


def media_expiry(ttl: int, bucket: int) -> int:
    """Quantised so the same URL is returned for `bucket` seconds: browsers can cache the image."""
    t = int(time.time()) + ttl
    return ((t + bucket - 1) // bucket) * bucket


def verify_media(settings: Settings, key: str, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    return hmac.compare_digest(media_signature(settings, key, exp), sig)
