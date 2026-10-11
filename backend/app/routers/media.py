"""Signed image URLs for local storage. (S3 uses presigned URLs and never hits this route.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.config import Settings
from app.deps import get_settings_dep, get_storage
from app.errors import ApiError, not_found
from app.infra.storage import valid_key
from app.security import verify_media

router = APIRouter(tags=["media"])


@router.get("/media/{key:path}")
async def media(key: str, exp: int = Query(...), sig: str = Query(...), s: Settings = Depends(get_settings_dep), storage=Depends(get_storage)):
    if not valid_key(key) or not verify_media(s, key, exp, sig):
        raise ApiError(403, "This link has expired.")  # same answer for bad key and bad signature
    try:
        data = await storage.get(key)
    except (FileNotFoundError, ValueError):
        raise not_found("Image not found.")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})
