"""Scan service layer: views, permissions, corrections and the submit / approve / return / reopen workflow.
Business rules live HERE (not in routers) so every entry point enforces them: separation of duties,
locking of approved scans, "no submit while anything is unresolved", audit + notifications on every change."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import reports
from app.domain.common import (Actor, active_catalog, audit, get_org_settings, iso, notify, product_names,
                               user_ids_with_roles, user_ref)
from app.domain.selectors import compute_summary, compute_totals, is_unresolved
from app.domain.statemachine import EDITABLE, SUBMITTABLE, can_transition
from app.errors import conflict, forbidden, not_found, unprocessable
from app.infra.storage import Storage
from app.models import (Approval, Comment, Detection, ExpectedStock, Scan, ScanEvent, ScanSummary, Shelf, Store,
                        User, utcnow)
from app.permissions import approval_rule, can, can_delete_scan

AUTO_REF = {"id": "system", "name": "Auto-approval (within tolerance)", "role": "MANAGER"}
ACTIONS = {"ASSIGN", "NOT_A_PRODUCT", "UNKNOWN", "CONFIRM", "RESET"}


# ------------------------------------------------------------------ loading
async def load_scan(db: AsyncSession, org_id: str, scan_id: str, *, lock: bool = False) -> Scan:
    q = select(Scan).where(Scan.id == scan_id, Scan.org_id == org_id)
    if lock:
        q = q.with_for_update()  # serialises concurrent submit/approve/edit on PostgreSQL (no-op on SQLite)
    scan = (await db.execute(q)).scalar_one_or_none()
    if scan is None:
        raise not_found("This scan no longer exists.")
    return scan


async def load_dets(db: AsyncSession, scan_id: str) -> list[Detection]:
    return list((await db.scalars(select(Detection).where(Detection.scan_id == scan_id).order_by(Detection.seq))).all())


async def expected_for_shelf(db: AsyncSession, shelf_id: str) -> dict[str, int]:
    rows = await db.execute(select(ExpectedStock.sku, ExpectedStock.expected_qty).where(ExpectedStock.shelf_id == shelf_id))
    return {sku: qty for sku, qty in rows}


async def shelf_name(db: AsyncSession, shelf_id: str) -> str:
    return (await db.scalar(select(Shelf.name).where(Shelf.id == shelf_id))) or shelf_id


def add_event(db: AsyncSession, scan_id: str, actor_name: str, event: str, note: str | None = None) -> None:
    db.add(ScanEvent(scan_id=scan_id, actor_name=actor_name, event=event, note=note or None))


def transition(db: AsyncSession, scan: Scan, to: str, actor: Actor, *, event: str | None = None, note: str | None = None,
               audit_action: str | None = None, audit_note: bool = False) -> None:
    if not can_transition(scan.status, to):
        raise conflict(f"A scan that is {scan.status.lower().replace('_', ' ')} cannot move to {to.lower().replace('_', ' ')}.")
    before, scan.status = scan.status, to
    add_event(db, scan.id, actor.name, event or to, note)
    audit(db, actor, audit_action or to, scan.id, scan_id=scan.id, before=before,
          after=f"{to}: {note}" if (audit_note and note) else to)


# ------------------------------------------------------------------ permissions (computed per user, per scan)
def scan_permissions(user: User, scan: Scan, unresolved: int) -> dict[str, bool]:
    senior_or_own = user.role != "STAFF" or scan.created_by == user.id
    manager_fix = scan.status == "SUBMITTED" and can(user.role, "scan.approve")
    rule = approval_rule(user.role, scan.submitted_by, user.id)
    return {
        "can_edit": can(user.role, "scan.review") and ((scan.status in EDITABLE and senior_or_own) or manager_fix),
        "can_submit": can(user.role, "scan.submit") and scan.status in SUBMITTABLE and unresolved == 0 and senior_or_own,
        "can_approve": scan.status == "SUBMITTED" and rule.allowed and unresolved == 0,
        "can_reopen": scan.status == "APPROVED" and can(user.role, "scan.reopen"),
    }


# ------------------------------------------------------------------ views
async def _users(db: AsyncSession, ids: set[str | None]) -> dict[str, User]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u for u in (await db.scalars(select(User).where(User.id.in_(ids))))}


def _det_view(d: Detection, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": d.id, "bbox": [round(d.x1, 1), round(d.y1, 1), round(d.x2, 1), round(d.y2, 1)], "state": d.state,
        "top_matches": [{"sku": m["sku"], "name": names.get(m["sku"], m["sku"]), "score": round(m["score"], 3)} for m in d.top_matches[:3]],
        "assigned_sku": d.assigned_sku, "det_confidence": round(d.det_confidence, 3), "corrected_by": d.corrected_by,
        "deleted": d.deleted, "needs_review": d.needs_review, "accepted_unknown": d.accepted_unknown,
    }


async def scan_view(db: AsyncSession, storage: Storage, scan: Scan, user: User) -> dict[str, Any]:
    dets = await load_dets(db, scan.id)
    names = await product_names(db, scan.org_id)
    st = await get_org_settings(db, scan.org_id)
    totals = compute_totals(dets)
    snap = await db.get(ScanSummary, scan.id) if scan.status == "APPROVED" else None
    summary = snap.rows if snap else compute_summary(dets, names, await expected_for_shelf(db, scan.shelf_id), st.tolerance)

    appr = await db.get(Approval, scan.current_approval_id) if scan.current_approval_id else None
    users = await _users(db, {scan.created_by, scan.submitted_by, appr.decided_by if appr else None})
    approval = None
    if appr:
        decider = users.get(appr.decided_by or "")
        approval = {"decided_by": user_ref(decider) if (decider and not appr.auto) else AUTO_REF,
                    "decision": appr.decision, "note": appr.note, "at": iso(appr.at)}

    events = (await db.scalars(select(ScanEvent).where(ScanEvent.scan_id == scan.id))).all()
    comments = (await db.scalars(select(Comment).where(Comment.scan_id == scan.id))).all()
    timeline = sorted(
        [{"at": e.at, "actor": e.actor_name, "event": e.event, "note": e.note} for e in events]
        + [{"at": c.at, "actor": c.author_name, "event": "COMMENT", "note": c.text} for c in comments], key=lambda x: x["at"])
    for t in timeline:
        t["at"] = iso(t["at"])
        if t["note"] is None:
            del t["note"]

    return {
        "scan_id": scan.id, "status": scan.status, "store_id": scan.store_id, "shelf_id": scan.shelf_id,
        "created_by": user_ref(users[scan.created_by]), "submitted_by": user_ref(users[scan.submitted_by]) if scan.submitted_by in users else None,
        "approval": approval,
        "image": {"url": storage.url(scan.image_key) if scan.image_key else "", "display_url": storage.url(scan.display_key) if scan.display_key else "",
                  "width": scan.width, "height": scan.height},
        "detections": [_det_view(d, names) for d in dets], "summary": summary, "totals": totals,
        "permissions": scan_permissions(user, scan, totals["unresolved"]), "timeline": timeline,
        "pinned_detection_ids": list(scan.pinned or []),
    }


async def totals_for(db: AsyncSession, scan_ids: list[str]) -> dict[str, dict[str, int]]:
    """SQL twin of selectors.compute_totals for list screens (kept equal by a test)."""
    if not scan_ids:
        return {}
    live = Detection.deleted.is_(False)
    resolved = (Detection.deleted.is_(True)) | (Detection.accepted_unknown.is_(True)) | Detection.state.in_(("SURE", "CORRECTED"))
    q = (select(Detection.scan_id,
                func.sum(case((live, 1), else_=0)),
                func.sum(case((live & (Detection.state == "SURE"), 1), else_=0)),
                func.sum(case((live & (Detection.state == "CHECK") & Detection.accepted_unknown.is_(False), 1), else_=0)),
                func.sum(case((live & (Detection.state == "UNKNOWN") & Detection.accepted_unknown.is_(False), 1), else_=0)),
                func.sum(case((resolved, 0), else_=1)))
         .where(Detection.scan_id.in_(scan_ids)).group_by(Detection.scan_id))
    out = {sid: {"detected": 0, "sure": 0, "check": 0, "unknown": 0, "unresolved": 0} for sid in scan_ids}
    for sid, dt, su, ch, un, ur in await db.execute(q):
        out[sid] = {"detected": int(dt or 0), "sure": int(su or 0), "check": int(ch or 0), "unknown": int(un or 0), "unresolved": int(ur or 0)}
    return out


async def list_items(db: AsyncSession, scans: list[Scan]) -> list[dict[str, Any]]:
    if not scans:
        return []
    users = await _users(db, {s.created_by for s in scans} | {s.submitted_by for s in scans})
    shelves = {sh.id: sh.name for sh in await db.scalars(select(Shelf).where(Shelf.id.in_({s.shelf_id for s in scans})))}
    totals = await totals_for(db, [s.id for s in scans])
    return [{"scan_id": s.id, "status": s.status, "shelf_id": s.shelf_id, "shelf_name": shelves.get(s.shelf_id, s.shelf_id),
             "created_by": user_ref(users[s.created_by]), "created_at": iso(s.created_at), "totals": totals[s.id],
             "submitted_by": user_ref(users[s.submitted_by]) if s.submitted_by in users else None, "device": s.device} for s in scans]


def _day(s: str, end: bool) -> datetime:
    try:
        d = datetime.fromisoformat(s[:10]).date()
    except ValueError:
        raise unprocessable("Dates must look like YYYY-MM-DD.")
    return datetime.combine(d + timedelta(days=1) if end else d, time.min, tzinfo=timezone.utc)


async def list_scans(db: AsyncSession, user: User, *, status: str | None, mine: bool, shelf_id: str | None, person: str | None,
                     date_from: str | None, date_to: str | None, limit: int, offset: int) -> list[Scan]:
    q = select(Scan).where(Scan.org_id == user.org_id)
    if mine or user.role == "STAFF":  # staff list = own scans (spec: "My scans"); detail by id follows the matrix
        q = q.where(Scan.created_by == user.id)
    if person:
        q = q.where(Scan.created_by == person)
    if status:
        q = q.where(Scan.status == status)
    if shelf_id:
        q = q.where(Scan.shelf_id == shelf_id)
    if date_from:
        q = q.where(Scan.created_at >= _day(date_from, False))
    if date_to:
        q = q.where(Scan.created_at < _day(date_to, True))
    return list((await db.scalars(q.order_by(Scan.created_at.desc(), Scan.id).limit(limit).offset(offset))).all())


# ------------------------------------------------------------------ corrections
def _apply(d: Detection, action: str, sku: str | None, user_id: str) -> None:
    if action == "RESET":
        d.state, d.assigned_sku, d.corrected_by = d.orig_state, d.orig_sku, None
        d.deleted = d.accepted_unknown = False
        return
    d.state, d.corrected_by = "CORRECTED", user_id
    if action in ("ASSIGN", "CONFIRM"):
        d.assigned_sku = sku if action == "ASSIGN" else d.top_matches[0]["sku"]
        d.deleted = d.accepted_unknown = False
    elif action == "NOT_A_PRODUCT":
        d.assigned_sku, d.deleted, d.accepted_unknown = None, True, False
    else:  # UNKNOWN: consciously accepted as an unknown item
        d.assigned_sku, d.deleted, d.accepted_unknown = None, False, True


async def patch_detections(db: AsyncSession, storage: Storage, user: User, actor: Actor, scan_id: str,
                           ids: list[str], action: str, sku: str | None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise unprocessable("Unknown action.")
    if not ids or len(ids) > 5000:
        raise unprocessable("Send between 1 and 5000 box ids.")
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if not can(user.role, "scan.review"):
        raise forbidden()
    manager_fix = scan.status == "SUBMITTED" and can(user.role, "scan.approve")
    if user.role == "STAFF" and scan.created_by != user.id:
        raise forbidden("You can only change your own scans.")
    if not (scan.status in EDITABLE or manager_fix):
        raise conflict("This scan is locked." if scan.status in ("APPROVED", "ARCHIVED") else f"This scan cannot be edited while it is {scan.status.lower().replace('_', ' ')}.")

    dets = {d.id: d for d in await load_dets(db, scan.id)}
    uniq = list(dict.fromkeys(ids))
    if (missing := [i for i in uniq if i not in dets]):
        raise unprocessable(f"Unknown box id: {missing[0]}.")
    if action == "ASSIGN":
        if not sku or sku not in {s for s, _ in await active_catalog(db, scan.org_id)}:
            raise unprocessable("Choose a product from the catalog.")
    if action == "CONFIRM" and any(not dets[i].top_matches for i in uniq):
        raise unprocessable("A box without any suggestion cannot be confirmed. Choose a product instead.")

    before = ", ".join(f"{i}:{dets[i].state}" for i in uniq[:20]) + (f" +{len(uniq) - 20} more" if len(uniq) > 20 else "")
    for i in uniq:
        _apply(dets[i], action, sku, user.id)
    audit(db, actor, "DETECTIONS_CORRECTED", scan.id, scan_id=scan.id, before=before,
          after=f"{action}{' ' + sku if action == 'ASSIGN' else ''} on {len(uniq)} box{'es' if len(uniq) != 1 else ''}")

    if manager_fix:
        last = await db.scalar(select(ScanEvent.event).where(ScanEvent.scan_id == scan.id).order_by(ScanEvent.at.desc()).limit(1))
        if last != "MANAGER_EDIT":
            add_event(db, scan.id, actor.name, "MANAGER_EDIT", "Counts adjusted during review")
            audit(db, actor, "MANAGER_EDIT", scan.id, scan_id=scan.id, before="SUBMITTED", after="SUBMITTED")
    elif scan.status != "IN_REVIEW":
        transition(db, scan, "IN_REVIEW", actor)
    scan.updated_at = utcnow()
    await db.commit()
    return await scan_view(db, storage, scan, user)


# ------------------------------------------------------------------ submit / approve / return / reopen
async def _summary_rows(db: AsyncSession, scan: Scan, dets: list[Detection]):
    st = await get_org_settings(db, scan.org_id)
    return compute_summary(dets, await product_names(db, scan.org_id), await expected_for_shelf(db, scan.shelf_id), st.tolerance), st


async def submit(db: AsyncSession, storage: Storage, user: User, actor: Actor, scan_id: str, note: str | None) -> dict[str, Any]:
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if not can(user.role, "scan.submit"):
        raise forbidden()
    if user.role == "STAFF" and scan.created_by != user.id:
        raise forbidden("You can only submit your own scans.")
    if scan.status not in SUBMITTABLE:
        raise conflict(f"A scan that is {scan.status.lower().replace('_', ' ')} cannot be submitted.")
    dets = await load_dets(db, scan.id)
    unresolved = sum(1 for d in dets if is_unresolved(d))
    if unresolved:
        raise conflict(f"{unresolved} flagged box{'es' if unresolved != 1 else ''} still need a decision.")
    note = (note or "").strip()[:1000] or None

    transition(db, scan, "SUBMITTED", actor, note=note)
    scan.submitted_by, scan.submitted_at, scan.current_approval_id, scan.pinned = user.id, utcnow(), None, []
    name = await shelf_name(db, scan.shelf_id)
    notify(db, await user_ids_with_roles(db, scan.org_id, ("MANAGER", "OWNER"), {user.id}), "SUBMITTED", f"{user.name} submitted {name}", scan.id)

    rows, st = await _summary_rows(db, scan, dets)
    if st.auto_approve and all(r["within_tolerance"] for r in rows):  # in tolerance AND the Owner switched this on
        await _do_approve(db, scan, dets, rows, st.tolerance, Actor(None, "Auto-approval", None, scan.org_id, actor.ip, actor.ua), None, auto=True, override=False)
    await db.commit()
    return await scan_view(db, storage, scan, user)


async def _do_approve(db: AsyncSession, scan: Scan, dets: list[Detection], rows: list[dict], tol: dict, actor: Actor,
                      approver: User | None, *, auto: bool, override: bool) -> None:
    low_before = await reports.low_stock_skus(db, scan.org_id)
    if auto:
        event, act, note = "APPROVED (auto, within tolerance)", "APPROVED_AUTO", "Auto-approved: every SKU is within tolerance."
    elif override:
        event, act, note = "APPROVED (owner override)", "APPROVED_OVERRIDE", "Owner override"
    else:
        event, act, note = "APPROVED", "APPROVED", ""
    transition(db, scan, "APPROVED", actor, event=event, audit_action=act)
    appr = Approval(scan_id=scan.id, decision="APPROVED", note=note, decided_by=approver.id if approver else None, auto=auto, override=override)
    db.add(appr)
    await db.flush()
    scan.current_approval_id, scan.pinned = appr.id, []
    await db.merge(ScanSummary(scan_id=scan.id, rows=rows, totals=compute_totals(dets), tolerance=dict(tol), frozen_at=utcnow()))
    await db.flush()

    name = await shelf_name(db, scan.shelf_id)
    skip = {approver.id} if approver else set()
    notify(db, [i for i in (scan.created_by, scan.submitted_by) if i and i not in skip], "APPROVED", f"{name} was approved and locked", scan.id)
    newly_low = (await reports.low_stock_skus(db, scan.org_id)) - low_before
    if newly_low:
        names = await product_names(db, scan.org_id)
        mgrs = await user_ids_with_roles(db, scan.org_id, ("MANAGER", "OWNER"))
        for sku in sorted(newly_low):
            notify(db, mgrs, "LOW_STOCK", f"{names.get(sku, sku)} is below its minimum", scan.id)


async def approve(db: AsyncSession, storage: Storage, user: User, actor: Actor, scan_id: str) -> dict[str, Any]:
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    rule = approval_rule(user.role, scan.submitted_by, user.id)
    if not rule.allowed:
        raise forbidden(rule.reason or "You cannot approve this scan.")
    if scan.status != "SUBMITTED":
        raise conflict("This scan is not waiting for a decision.")
    dets = await load_dets(db, scan.id)
    if (n := sum(1 for d in dets if is_unresolved(d))):  # a manager may have reset a box after submission
        raise conflict(f"{n} flagged box{'es' if n != 1 else ''} still need a decision.")
    rows, st = await _summary_rows(db, scan, dets)
    await _do_approve(db, scan, dets, rows, st.tolerance, actor, user, auto=False, override=rule.override)
    await db.commit()
    return await scan_view(db, storage, scan, user)


async def return_scan(db: AsyncSession, storage: Storage, user: User, actor: Actor, scan_id: str, note: str, pinned: list[str]) -> dict[str, Any]:
    if not can(user.role, "scan.approve"):
        raise forbidden()
    note = (note or "").strip()
    if not note or len(note) > 2000:
        raise unprocessable("Add a note (up to 2000 characters) so staff know what to fix.")
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if scan.status != "SUBMITTED":
        raise conflict("This scan is not waiting for a decision.")
    ids = {d.id for d in await load_dets(db, scan.id)}
    pinned = list(dict.fromkeys(pinned or []))
    if (bad := [p for p in pinned if p not in ids]):
        raise unprocessable(f"Unknown box id: {bad[0]}.")
    transition(db, scan, "RETURNED", actor, note=note, audit_note=True)
    appr = Approval(scan_id=scan.id, decision="RETURNED", note=note, pinned=pinned, decided_by=user.id)
    db.add(appr)
    await db.flush()
    scan.current_approval_id, scan.pinned = appr.id, pinned
    name = await shelf_name(db, scan.shelf_id)
    notify(db, [i for i in (scan.created_by, scan.submitted_by) if i and i != user.id], "RETURNED", f"{name} was returned with a note", scan.id)
    await db.commit()
    return await scan_view(db, storage, scan, user)


async def reopen(db: AsyncSession, storage: Storage, user: User, actor: Actor, scan_id: str, reason: str) -> dict[str, Any]:
    if not can(user.role, "scan.reopen"):
        raise forbidden()
    reason = (reason or "").strip()
    if not reason or len(reason) > 1000:
        raise unprocessable("A reason is required to reopen an approved scan (up to 1000 characters).")
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if scan.status != "APPROVED":
        raise conflict("Only an approved scan can be reopened.")
    transition(db, scan, "REOPENED", actor, note=reason, audit_note=True)
    await db.execute(delete(ScanSummary).where(ScanSummary.scan_id == scan.id))  # approval no longer feeds reports
    scan.current_approval_id, scan.pinned = None, []
    name = await shelf_name(db, scan.shelf_id)
    notify(db, [i for i in (scan.created_by, scan.submitted_by) if i and i != user.id], "RETURNED", f"{name} was reopened: {reason[:120]}", scan.id)
    await db.commit()
    return await scan_view(db, storage, scan, user)


async def add_comment(db: AsyncSession, storage: Storage, user: User, scan_id: str, text: str) -> dict[str, Any]:
    if not can(user.role, "scan.review"):
        raise forbidden()
    text = (text or "").strip()
    if not text or len(text) > 2000:
        raise unprocessable("Write a comment of up to 2000 characters.")
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if scan.status == "ARCHIVED":
        raise conflict("Archived scans are read-only.")
    db.add(Comment(scan_id=scan.id, author_id=user.id, author_name=user.name, text=text))
    await db.commit()
    return await scan_view(db, storage, scan, user)


async def delete_scan(db: AsyncSession, storage: Storage, user: User, actor: Actor, scan_id: str) -> None:
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if not can_delete_scan(user.role, scan.status, scan.created_by, user.id):
        raise forbidden("You cannot delete this scan." if user.role != "STAFF" else "Staff can only delete their own drafts.")
    audit(db, actor, "SCAN_DELETED", scan.id, scan_id=scan.id, before=scan.status, after="DELETED")
    for model in (Comment, ScanEvent, Approval, ScanSummary, Detection):
        await db.execute(delete(model).where(model.scan_id == scan.id))
    from app.models import Notification
    await db.execute(delete(Notification).where(Notification.scan_id == scan.id))
    await db.delete(scan)
    await db.commit()
    await storage.delete_prefix(f"scans/{scan_id}")  # after the commit: a failed delete leaves a harmless orphan file


async def retry(db: AsyncSession, user: User, actor: Actor, scan_id: str) -> Scan:
    scan = await load_scan(db, user.org_id, scan_id, lock=True)
    if user.role == "STAFF" and scan.created_by != user.id or not can(user.role, "scan.capture"):
        raise forbidden()
    if scan.status != "FAILED":
        raise conflict("Only a failed scan can be retried.")
    transition(db, scan, "UPLOADED", actor, event="RETRIED")
    scan.failure_reason, scan.proc_step, scan.proc_pct, scan.proc_found, scan.lease_until = None, "Detecting", 0, 0, None
    await db.commit()
    return scan


async def create_shelf_scan_row(db: AsyncSession, user: User, actor: Actor, shelf: Shelf, *, image_key: str, display_key: str,
                                width: int, height: int, device: str | None, scan_id: str,
                                client_request_id: str | None = None) -> Scan:
    scan = Scan(id=scan_id, org_id=user.org_id, store_id=shelf.store_id, shelf_id=shelf.id, status="DRAFT", created_by=user.id,
                image_key=image_key, display_key=display_key, width=width, height=height, device=device,
                client_request_id=client_request_id)
    db.add(scan)
    await db.flush()
    add_event(db, scan.id, user.name, "UPLOADED")
    audit(db, actor, "UPLOADED", scan.id, scan_id=scan.id, before="DRAFT", after="UPLOADED")
    scan.status = "UPLOADED"
    return scan


async def store_name(db: AsyncSession, store_id: str) -> str:
    return (await db.scalar(select(Store.name).where(Store.id == store_id))) or store_id
