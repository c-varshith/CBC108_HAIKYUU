"""Approval inbox and notifications."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_permission
from app.domain import scans as svc
from app.domain.common import iso
from app.models import Notification, Scan, ScanEvent, User
from app.permissions import approval_rule

router = APIRouter(tags=["workflow"])


@router.get("/approvals")
async def approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.approve")),
                    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    """Inbox for Manager/Owner: submitted scans, oldest first. `can_approve` is false for your own submissions."""
    rows = list((await db.scalars(select(Scan).where(Scan.org_id == user.org_id, Scan.status == "SUBMITTED")
                                  .order_by(Scan.submitted_at, Scan.id).limit(limit).offset(offset))).all())
    items = await svc.list_items(db, rows)
    notes = {}
    if rows:
        ev = await db.execute(select(ScanEvent.scan_id, ScanEvent.note).where(ScanEvent.scan_id.in_([r.id for r in rows]), ScanEvent.event == "SUBMITTED")
                              .order_by(ScanEvent.at))
        for sid, note in ev:
            notes[sid] = note  # latest SUBMITTED event wins
    for it, sc in zip(items, rows):
        it["submitted_at"] = iso(sc.submitted_at)
        it["submit_note"] = notes.get(sc.id)
        it["can_approve"] = approval_rule(user.role, sc.submitted_by, user.id).allowed and it["totals"]["unresolved"] == 0
    return items


class ReadBody(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=500)
    all: bool = False


def _n_view(n: Notification) -> dict:
    return {"id": n.id, "kind": n.kind, "text": n.text, "scan_id": n.scan_id, "read": n.read, "at": iso(n.at)}


@router.get("/notifications")
async def notifications(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view")),
                        unread_only: bool = False, limit: int = Query(50, ge=1, le=200)):
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.read.is_(False))
    items = list((await db.scalars(q.order_by(Notification.at.desc()).limit(limit))).all())
    unread = await db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.read.is_(False)))
    return {"unread": unread, "items": [_n_view(n) for n in items]}


@router.post("/notifications/read", status_code=204)
async def mark_read(body: ReadBody, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    q = update(Notification).where(Notification.user_id == user.id, Notification.read.is_(False))  # own rows only, whatever ids are sent
    if not body.all:
        q = q.where(Notification.id.in_(body.ids or []))
    await db.execute(q.values(read=True))
    await db.commit()
