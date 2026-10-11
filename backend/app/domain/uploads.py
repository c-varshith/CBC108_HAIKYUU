"""POST /scans: validate -> normalise -> store -> create scan (UPLOADED) -> hand to the runner."""
from __future__ import annotations

import re
import json

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain import scans as svc
from app.domain.common import Actor
from app.errors import ApiError, conflict, not_found, unprocessable
from app.infra.imaging import ImageRejected, aprocess_scan_image
from app.infra.storage import Storage
from app.models import Shelf, User, new_id


async def read_limited(f: UploadFile, limit: int) -> bytes:
    """Reads at most `limit` bytes: an oversized upload is refused without buffering all of it."""
    buf = bytearray()
    while chunk := await f.read(1024 * 1024):
        buf += chunk
        if len(buf) > limit:
            raise ApiError(413, "Photo is too large for the server.")
    if not buf:
        raise unprocessable("This file is empty.")
    return bytes(buf)


async def create_scans(db: AsyncSession, storage: Storage, s: Settings, user: User, actor: Actor, shelf_id: str,
                       files: list[UploadFile], device: str | None, request_ids_json: str | None = None) -> list[svc.Scan]:
    if not files:
        raise unprocessable("Attach at least one photo.")
    if len(files) > s.max_files_per_request:
        raise unprocessable(f"Send at most {s.max_files_per_request} photos per request.")
    try:
        request_ids = json.loads(request_ids_json) if request_ids_json else [None] * len(files)
    except (TypeError, json.JSONDecodeError):
        raise unprocessable("request_ids must be a JSON list.")
    if not isinstance(request_ids, list) or len(request_ids) != len(files):
        raise unprocessable("Send one request id per photo.")
    if any(rid is not None and (not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid)) for rid in request_ids):
        raise unprocessable("Each request id must contain only letters, numbers, underscores or hyphens.")
    concrete = [rid for rid in request_ids if rid is not None]
    if len(concrete) != len(set(concrete)):
        raise unprocessable("Each photo must have a different request id.")
    shelf = await db.get(Shelf, shelf_id)
    if shelf is None or shelf.org_id != user.org_id:
        raise not_found("Shelf not found.")
    device = re.sub(r"[^\w .·/()-]", "", device or "")[:120] or None
    stored: list[str] = []
    out = []
    try:
        for f, request_id in zip(files, request_ids):
            if request_id:
                previous = await db.scalar(select(svc.Scan).where(svc.Scan.created_by == user.id,
                                                                  svc.Scan.client_request_id == request_id))
                if previous is not None:
                    out.append(previous)
                    continue
            data = await read_limited(f, s.max_upload_bytes)
            try:
                img = await aprocess_scan_image(data, s)
            except ImageRejected as e:
                raise ApiError(e.status, e.message)
            sid = new_id()
            await storage.put(f"scans/{sid}/original.jpg", img.original, "image/jpeg")
            await storage.put(f"scans/{sid}/display.jpg", img.display, "image/jpeg")
            stored.append(sid)
            out.append(await svc.create_shelf_scan_row(db, user, actor, shelf, image_key=f"scans/{sid}/original.jpg",
                                                        display_key=f"scans/{sid}/display.jpg", width=img.width, height=img.height,
                                                        device=device, scan_id=sid, client_request_id=request_id))
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        for sid in stored:
            await storage.delete_prefix(f"scans/{sid}")
        if concrete:
            rows = list((await db.scalars(select(svc.Scan).where(svc.Scan.created_by == user.id,
                                                                   svc.Scan.client_request_id.in_(concrete)))).all())
            by_request = {row.client_request_id: row for row in rows}
            if all(rid in by_request for rid in request_ids):
                return [by_request[rid] for rid in request_ids]
        raise conflict("This upload was received concurrently. Retry with the same request ids.") from e
    except BaseException:
        await db.rollback()
        for sid in stored:  # no orphan files when any photo of the batch is rejected
            await storage.delete_prefix(f"scans/{sid}")
        raise
    return out
