"""Scans: upload, list, detail, corrections, workflow transitions, SSE progress, export, timeline, comments."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import db as dbmod
from app.config import Settings
from app.db import get_db
from app.deps import (
    actor_for,
    current_user_sse,
    get_inference,
    get_runner,
    get_settings_dep,
    get_storage,
    require_permission,
)
from app.domain import exports, uploads
from app.domain import scans as svc
from app.domain.common import active_catalog, get_org_settings
from app.domain.processing import build_detection_rows
from app.errors import ApiError, forbidden
from app.infra.imaging import ImageRejected, aprocess_scan_image
from app.infra.inference import InferenceError
from app.infra.ratelimit import RateLimiter
from app.models import Scan, User
from app.permissions import can

router = APIRouter(tags=["scans"])
_live_limiter = RateLimiter(90, 60)


class PatchBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    action: Literal["ASSIGN", "NOT_A_PRODUCT", "UNKNOWN", "CONFIRM", "RESET"]
    sku: str | None = Field(default=None, max_length=64)


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ReturnBody(BaseModel):
    note: str = Field(max_length=2000)
    pinned: list[str] = Field(default_factory=list, max_length=5000)


class ReasonBody(BaseModel):
    reason: str = Field(max_length=1000)


class CommentBody(BaseModel):
    text: str = Field(max_length=2000)


def _progress(scan: Scan) -> dict:
    return {"scan_id": scan.id, "status": scan.status, "step": scan.proc_step, "pct": scan.proc_pct, "found": scan.proc_found,
            "failure_reason": scan.failure_reason}


@router.get("/scans/live-status")
async def live_status(user: User = Depends(require_permission("scan.capture")),
                      s: Settings = Depends(get_settings_dep), inference=Depends(get_inference)):
    """Report whether real, organization-catalog live detection is available for this session."""
    if s.inference_backend != "http":
        return {"available": False, "reason": "Live detection requires the real AI worker. You can still capture a photo and run a normal scan."}
    if not await inference.ping():
        return {"available": False, "reason": "The AI worker is unavailable right now. You can still capture a photo and run a normal scan."}
    return {"available": True, "reason": None}


@router.post("/scans/live-detect")
async def live_detect(image: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("scan.capture")), s: Settings = Depends(get_settings_dep),
                      inference=Depends(get_inference)):
    """Run one transient camera frame through the same org catalog and classifier as a saved scan."""
    if s.inference_backend != "http":
        raise ApiError(503, "Live detection requires the real AI worker. You can still capture a photo and run a normal scan.")
    wait = _live_limiter.hit(user.id)
    if wait:
        raise ApiError(429, f"Too many live frames. Try again in {wait}s.", headers={"Retry-After": str(wait)})
    data = await image.read(s.max_upload_bytes + 1)
    if not data:
        raise ApiError(422, "The camera frame is empty.")
    if len(data) > s.max_upload_bytes:
        raise ApiError(413, "The camera frame is too large.")
    try:
        processed = await aprocess_scan_image(data, s)
    except ImageRejected as e:
        raise ApiError(e.status, e.message)

    catalog = await active_catalog(db, user.org_id)
    org_settings = await get_org_settings(db, user.org_id)
    try:
        raw = await asyncio.wait_for(
            inference.detect(processed.original, scan_id=f"live-{uuid.uuid4().hex}", catalog=catalog,
                            width=processed.width, height=processed.height),
            timeout=s.inference_timeout_seconds,
        )
    except (InferenceError, asyncio.TimeoutError) as e:
        raise ApiError(503, "The AI worker could not analyze this frame. Live detection is paused; you can still capture a photo.") from e

    rows = build_detection_rows(raw, width=processed.width, height=processed.height,
                                valid_skus={sku for sku, _ in catalog}, thresholds=dict(org_settings.thresholds),
                                det_conf_min=s.det_conf_min, max_n=s.max_detections)
    names = dict(catalog)
    estimates: dict[str | None, list[dict]] = {}
    detections = []
    for row in rows:
        top = row["top_matches"][0] if row["top_matches"] else None
        sku = row["assigned_sku"] or (top["sku"] if top else None)
        estimates.setdefault(sku, []).append(row)
        detections.append({"id": row["id"], "bbox": [row["x1"], row["y1"], row["x2"], row["y2"]],
                           "state": row["state"], "top_matches": row["top_matches"], "assigned_sku": row["assigned_sku"],
                           "det_confidence": row["det_confidence"]})
    estimate_rows = [{"sku": sku, "name": names.get(sku, "Unknown / no catalog match") if sku else "Unknown / no catalog match",
                      "quantity": len(items), "needs_review": sum(item["state"] != "SURE" for item in items)}
                     for sku, items in estimates.items()]
    estimate_rows.sort(key=lambda item: (item["sku"] is None, item["name"].casefold()))
    return {"width": processed.width, "height": processed.height, "detections": detections, "estimates": estimate_rows}


@router.post("/scans", status_code=201)
async def create_scans(request: Request, shelf_id: str = Form(...), device: str | None = Form(default=None),
                       request_ids: str | None = Form(default=None),
                       files: list[UploadFile] = File(...), db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("scan.capture")), storage=Depends(get_storage),
                       s: Settings = Depends(get_settings_dep), runner=Depends(get_runner)):
    """One scan per photo. `scan_id` is the first, `scan_ids` all of them; processing starts in the background."""
    created = await uploads.create_scans(db, storage, s, user, actor_for(request, user), shelf_id, files, device, request_ids)
    for sc in created:
        runner.start(sc.id)
    return {"scan_id": created[0].id, "scan_ids": [c.id for c in created], "status": created[0].status}


@router.get("/scans")
async def list_scans(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view")),
                     status: str | None = None, mine: bool = False, shelf_id: str | None = None, person: str | None = None,
                     date_from: str | None = None, date_to: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    rows = await svc.list_scans(db, user, status=status, mine=mine, shelf_id=shelf_id, person=person, date_from=date_from,
                                date_to=date_to, limit=limit, offset=offset)
    return await svc.list_items(db, rows)


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view")), storage=Depends(get_storage)):
    scan = await svc.load_scan(db, user.org_id, scan_id)
    return await svc.scan_view(db, storage, scan, user)


@router.get("/scans/{scan_id}/progress")
async def scan_progress(scan_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    scan = await svc.load_scan(db, user.org_id, scan_id)
    return _progress(scan)


@router.get("/scans/{scan_id}/events")
async def scan_events(scan_id: str, request: Request, user: User = Depends(current_user_sse)):
    """Server-sent progress for the processing screen. Sessions are short: each poll opens its own DB session."""
    if not can(user.role, "scan.view"):
        raise forbidden()
    org_id = user.org_id

    async def gen():
        last = None
        for _ in range(900):  # ~15 min cap; the client reconnects (resume) if it needs more
            if await request.is_disconnected():
                return
            async with dbmod.sessionmaker()() as db:
                scan = await svc.load_scan(db, org_id, scan_id)
                snap = _progress(scan)
            if snap != last:
                yield f"event: progress\ndata: {json.dumps(snap)}\n\n"
                last = snap
            if snap["status"] not in ("UPLOADED", "PROCESSING"):
                return
            await asyncio.sleep(1)
            yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.patch("/scans/{scan_id}/detections")
async def patch_detections(scan_id: str, body: PatchBody, request: Request, db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_permission("scan.review")), storage=Depends(get_storage)):
    return await svc.patch_detections(db, storage, user, actor_for(request, user), scan_id, body.ids, body.action, body.sku)


@router.post("/scans/{scan_id}/submit")
async def submit(scan_id: str, request: Request, body: NoteBody | None = None, db: AsyncSession = Depends(get_db),
                 user: User = Depends(require_permission("scan.submit")), storage=Depends(get_storage)):
    return await svc.submit(db, storage, user, actor_for(request, user), scan_id, body.note if body else None)


@router.post("/scans/{scan_id}/approve")
async def approve(scan_id: str, request: Request, db: AsyncSession = Depends(get_db),
                  user: User = Depends(require_permission("scan.approve")), storage=Depends(get_storage)):
    return await svc.approve(db, storage, user, actor_for(request, user), scan_id)


@router.post("/scans/{scan_id}/return")
async def return_scan(scan_id: str, body: ReturnBody, request: Request, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("scan.approve")), storage=Depends(get_storage)):
    return await svc.return_scan(db, storage, user, actor_for(request, user), scan_id, body.note, body.pinned)


@router.post("/scans/{scan_id}/reopen")
async def reopen(scan_id: str, body: ReasonBody, request: Request, db: AsyncSession = Depends(get_db),
                 user: User = Depends(require_permission("scan.reopen")), storage=Depends(get_storage)):
    return await svc.reopen(db, storage, user, actor_for(request, user), scan_id, body.reason)


@router.post("/scans/{scan_id}/retry", status_code=202)
async def retry(scan_id: str, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.capture")),
                runner=Depends(get_runner)):
    scan = await svc.retry(db, user, actor_for(request, user), scan_id)
    runner.start(scan.id)
    return _progress(scan)


@router.delete("/scans/{scan_id}", status_code=204)
async def delete_scan(scan_id: str, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.delete")),
                      storage=Depends(get_storage)):
    await svc.delete_scan(db, storage, user, actor_for(request, user), scan_id)
    return Response(status_code=204)


@router.get("/scans/{scan_id}/timeline")
async def timeline(scan_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view")), storage=Depends(get_storage)):
    scan = await svc.load_scan(db, user.org_id, scan_id)
    return (await svc.scan_view(db, storage, scan, user))["timeline"]


@router.post("/scans/{scan_id}/comments", status_code=201)
async def comment(scan_id: str, body: CommentBody, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.review")),
                  storage=Depends(get_storage)):
    return await svc.add_comment(db, storage, user, scan_id, body.text)


@router.get("/scans/{scan_id}/export")
async def export(scan_id: str, request: Request, format: Literal["csv", "pdf"] = "csv", db: AsyncSession = Depends(get_db),
                 user: User = Depends(require_permission("reports.export")), storage=Depends(get_storage)):
    scan = await svc.load_scan(db, user.org_id, scan_id)
    view = await svc.scan_view(db, storage, scan, user)
    name = await svc.shelf_name(db, scan.shelf_id)
    from app.domain.common import audit
    audit(db, actor_for(request, user), "EXPORTED", scan.id, scan_id=scan.id, after=format)
    await db.commit()
    if format == "pdf":
        data = await asyncio.to_thread(exports.scan_pdf, view, name)
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="scan-{scan.id[:8]}.pdf"'})
    return Response(exports.scan_csv(view, name), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="scan-{scan.id[:8]}.csv"'})
