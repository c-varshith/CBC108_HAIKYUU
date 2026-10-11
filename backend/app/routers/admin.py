"""Settings (thresholds, tolerance, approval policy), reports and alerts, audit log."""
from __future__ import annotations

import csv
import io
from datetime import datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.deps import actor_for, get_settings_dep, require_permission
from app.domain import reports
from app.domain.common import audit, get_org_settings, iso, settings_dict, validate_thresholds, validate_tolerance
from app.domain.exports import _cell
from app.domain.selectors import classify, demote
from app.errors import unprocessable
from app.models import AuditLog, Detection, Scan, User
from app.permissions import can

router = APIRouter(tags=["admin"])


# ------------------------------------------------------------------ settings
class ThresholdsIn(BaseModel):
    sure: float
    margin: float
    check: float


class ToleranceIn(BaseModel):
    pct: float
    units: float


class PolicyIn(BaseModel):
    auto_approve: bool | None = None
    retention_days: int | None = Field(default=None, ge=30, le=3650)


class PreviewIn(BaseModel):
    thresholds: ThresholdsIn


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    """Read by every role (the UI needs tolerance to explain flags); only the Owner can change anything."""
    out = settings_dict(await get_org_settings(db, user.org_id))
    await db.commit()
    return out


@router.get("/settings/thresholds")
async def get_thresholds(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    return dict((await get_org_settings(db, user.org_id)).thresholds)


@router.put("/settings/thresholds")
async def put_thresholds(body: ThresholdsIn, request: Request, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("settings.thresholds"))):
    new = validate_thresholds(body.model_dump())
    st = await get_org_settings(db, user.org_id)
    old = dict(st.thresholds)
    st.thresholds = new  # replace the dict object so the JSON column registers the change
    audit(db, actor_for(request, user), "THRESHOLDS_CHANGED", "thresholds", before=old, after=new)
    await db.commit()
    return new


@router.get("/settings/tolerance")
async def get_tolerance(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    return dict((await get_org_settings(db, user.org_id)).tolerance)


@router.put("/settings/tolerance")
async def put_tolerance(body: ToleranceIn, request: Request, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("settings.thresholds"))):
    new = validate_tolerance(body.model_dump())
    st = await get_org_settings(db, user.org_id)
    old = dict(st.tolerance)
    st.tolerance = new
    audit(db, actor_for(request, user), "TOLERANCE_CHANGED", "tolerance", before=old, after=new)
    await db.commit()
    return new


@router.get("/settings/approval-policy")
async def get_policy(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    st = await get_org_settings(db, user.org_id)
    return {"auto_approve": st.auto_approve, "retention_days": st.retention_days}


@router.put("/settings/approval-policy")
async def put_policy(body: PolicyIn, request: Request, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("settings.thresholds"))):
    st = await get_org_settings(db, user.org_id)
    before = {"auto_approve": st.auto_approve, "retention_days": st.retention_days}
    if body.auto_approve is not None:
        st.auto_approve = body.auto_approve
    if body.retention_days is not None:
        st.retention_days = body.retention_days
    after = {"auto_approve": st.auto_approve, "retention_days": st.retention_days}
    if before != after:
        audit(db, actor_for(request, user), "APPROVAL_POLICY_CHANGED", "approval-policy", before=before, after=after)
    await db.commit()
    return after


@router.post("/settings/thresholds/preview")
async def preview_thresholds(body: PreviewIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("settings.thresholds")),
                             cfg: Settings = Depends(get_settings_dep)):
    """"What would be flagged": re-classify the stored raw scores of the 20 most recent processed scans with the proposed thresholds."""
    proposed = validate_thresholds(body.thresholds.model_dump())
    st = await get_org_settings(db, user.org_id)
    ids = list((await db.scalars(select(Scan.id).where(Scan.org_id == user.org_id, Scan.status.notin_(("DRAFT", "UPLOADED", "PROCESSING", "FAILED")))
                                 .order_by(Scan.created_at.desc()).limit(20))).all())
    out = {"scans": len(ids), "boxes": 0, "current": {"SURE": 0, "CHECK": 0, "UNKNOWN": 0}, "proposed": {"SURE": 0, "CHECK": 0, "UNKNOWN": 0}}
    if ids:
        for d in await db.scalars(select(Detection).where(Detection.scan_id.in_(ids))):
            out["boxes"] += 1
            for key, th in (("current", dict(st.thresholds)), ("proposed", proposed)):
                state, _ = demote(classify(list(d.top_matches), th), d.det_confidence, [f for f in d.flags if f != "low_det_conf"], cfg.det_conf_min)
                out[key][state] += 1
    return out


# ------------------------------------------------------------------ reports and alerts
@router.get("/reports/summary")
async def reports_summary(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    return await reports.summary_report(db, user.org_id)


@router.get("/inventory")
async def inventory(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("inventory.view"))):
    return await reports.inventory_report(db, user.org_id)


@router.get("/alerts/low-stock")
async def low_stock(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    return await reports.low_stock(db, user.org_id)


@router.get("/reports/low-stock/export")
async def low_stock_export(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("reports.export"))):
    rows = await reports.low_stock(db, user.org_id)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["sku", "name", "on_hand", "min_qty", "suggested_reorder"])
    for r in rows:
        w.writerow([_cell(r["sku"]), _cell(r["name"]), r["on_hand"], r["min_qty"], r["reorder"]])
    audit(db, actor_for(request, user), "EXPORTED", "low-stock", after="csv")
    await db.commit()
    return Response(("\ufeff" + out.getvalue()).encode(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="low-stock.csv"'})


# ------------------------------------------------------------------ audit log
def _audit_query(org_id: str, q: str | None, actor: str | None, action: str | None, scan_id: str | None, date_from: str | None, date_to: str | None):
    stmt = select(AuditLog).where(AuditLog.org_id == org_id)
    if q:
        like = f"%{q.strip().lower()}%"
        from sqlalchemy import func, or_
        stmt = stmt.where(or_(func.lower(AuditLog.entity).like(like), func.lower(AuditLog.actor_name).like(like), func.lower(AuditLog.action).like(like)))
    if actor:
        stmt = stmt.where(AuditLog.actor_id == actor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if scan_id:
        stmt = stmt.where(AuditLog.scan_id == scan_id)

    def day(s: str, end: bool) -> datetime:
        try:
            d = datetime.fromisoformat(s[:10]).date()
        except ValueError:
            raise unprocessable("Dates must look like YYYY-MM-DD.")
        return datetime.combine(d + timedelta(days=1) if end else d, time.min, tzinfo=timezone.utc)
    if date_from:
        stmt = stmt.where(AuditLog.at >= day(date_from, False))
    if date_to:
        stmt = stmt.where(AuditLog.at < day(date_to, True))
    return stmt.order_by(AuditLog.at.desc(), AuditLog.id)


def _a_view(a: AuditLog) -> dict[str, Any]:
    return {"id": a.id, "at": iso(a.at), "actor": a.actor_name, "actor_id": a.actor_id, "action": a.action, "entity": a.entity,
            "scan_id": a.scan_id, "before": a.before, "after": a.after, "ip": a.ip}


@router.get("/audit-log")
async def audit_log(q: str | None = None, actor: str | None = None, action: str | None = None, scan_id: str | None = None,
                    date_from: str | None = None, date_to: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                    db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("audit.view"))):
    stmt = _audit_query(user.org_id, q, actor, action, scan_id, date_from, date_to).limit(limit).offset(offset)
    return [_a_view(a) for a in await db.scalars(stmt)]


@router.get("/audit-log/export")
async def audit_export(request: Request, q: str | None = None, actor: str | None = None, action: str | None = None, scan_id: str | None = None,
                       date_from: str | None = None, date_to: str | None = None, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("reports.export"))):
    if not can(user.role, "audit.view"):
        from app.errors import forbidden
        raise forbidden()
    rows = list((await db.scalars(_audit_query(user.org_id, q, actor, action, scan_id, date_from, date_to).limit(20000))).all())
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["at", "actor", "action", "entity", "scan_id", "before", "after", "ip"])
    for a in rows:
        w.writerow([iso(a.at), _cell(a.actor_name), a.action, _cell(a.entity), a.scan_id or "", _cell(a.before or ""), _cell(a.after or ""), a.ip or ""])
    audit(db, actor_for(request, user), "EXPORTED", "audit-log", after=f"csv, {len(rows)} rows")
    await db.commit()
    return Response(("\ufeff" + out.getvalue()).encode(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'})
