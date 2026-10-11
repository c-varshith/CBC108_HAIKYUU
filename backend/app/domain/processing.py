"""Background inference. Work is claimed with a LEASE (scans.lease_until) so that after an API restart, or
with several workers, each scan is processed once and stuck scans are picked up again (user can leave and resume).
For heavy load move `ProcessingRunner.run` behind a real queue (ARQ/Celery); the claim logic stays the same."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update

from app.config import Settings
from app.db import sessionmaker
from app.domain.common import active_catalog, audit, get_org_settings, notify, system_actor
from app.domain.scans import add_event, load_dets, transition
from app.domain.selectors import classify, demote, reading_order
from app.infra.inference import InferenceClient, RawDetection
from app.infra.storage import Storage
from app.models import Detection, Scan, utcnow

log = logging.getLogger("calibrex.processing")


def build_detection_rows(raw: list[RawDetection], *, width: int, height: int, valid_skus: set[str], thresholds: dict,
                         det_conf_min: float, max_n: int) -> list[dict[str, Any]]:
    """Raw AI output -> rows to store: clamp boxes, drop junk, classify with the Owner's thresholds,
    demote doubtful crops, number boxes in shelf reading order (d1, d2, ...)."""
    items = []
    for r in raw:
        x1, y1, x2, y2 = r.bbox
        x1, x2 = sorted((max(0.0, min(x1, width)), max(0.0, min(x2, width))))
        y1, y2 = sorted((max(0.0, min(y1, height)), max(0.0, min(y2, height))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        best: dict[str, float] = {}
        for m in r.matches:
            if m.sku in valid_skus and m.score > best.get(m.sku, -1):
                best[m.sku] = m.score
        matches = [{"sku": k, "score": round(v, 4)} for k, v in sorted(best.items(), key=lambda kv: -kv[1])][:5]
        flags = list(r.flags)
        if min(x2 - x1, y2 - y1) < 16:
            flags.append("tiny")
        state, flags = demote(classify(matches, thresholds), r.det_confidence, flags, det_conf_min)
        items.append({"bbox": (x1, y1, x2, y2), "conf": r.det_confidence, "matches": matches, "state": state, "flags": flags})
    items.sort(key=lambda i: -i["conf"])
    items = items[:max_n]
    order = reading_order([i["bbox"] for i in items])
    rows = []
    for n, idx in enumerate(order, 1):
        i = items[idx]
        sure = i["state"] == "SURE"
        sku = i["matches"][0]["sku"] if sure else None
        rows.append({"id": f"d{n}", "seq": n, "x1": i["bbox"][0], "y1": i["bbox"][1], "x2": i["bbox"][2], "y2": i["bbox"][3],
                     "state": i["state"], "assigned_sku": sku, "det_confidence": i["conf"], "top_matches": i["matches"],
                     "needs_review": not sure, "orig_state": i["state"], "orig_sku": sku, "flags": i["flags"]})
    return rows


class ProcessingRunner:
    def __init__(self, settings: Settings, storage: Storage, inference: InferenceClient):
        self.s, self.storage, self.inference = settings, storage, inference
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    def start(self, scan_id: str) -> None:
        t = asyncio.create_task(self._guard(scan_id))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:  # tests / graceful shutdown
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _guard(self, scan_id: str) -> None:
        try:
            await self.run(scan_id)
        except Exception:
            log.exception("processing crashed", extra={"scan_id": scan_id})
            await self._fail(scan_id, "Processing crashed.")

    async def _progress(self, scan_id: str, step: str, pct: int, found: int) -> None:
        async with sessionmaker()() as db:
            await db.execute(update(Scan).where(Scan.id == scan_id).values(
                proc_step=step, proc_pct=pct, proc_found=found, lease_until=utcnow() + timedelta(seconds=self.s.processing_lease_seconds)))
            await db.commit()

    async def _fail(self, scan_id: str, reason: str) -> None:
        async with sessionmaker()() as db:
            scan = await db.get(Scan, scan_id)
            if not scan or scan.status not in ("UPLOADED", "PROCESSING"):
                return
            actor = system_actor(scan.org_id)
            if scan.status == "UPLOADED":
                transition(db, scan, "PROCESSING", actor)
            transition(db, scan, "FAILED", actor, note=reason)
            scan.failure_reason, scan.proc_step, scan.proc_pct, scan.lease_until = reason, "Done", 100, None
            from app.domain.scans import shelf_name
            notify(db, [scan.created_by], "PROCESSED", f"{await shelf_name(db, scan.shelf_id)} could not be processed", scan.id)
            await db.commit()

    async def run(self, scan_id: str) -> None:
        now = utcnow()
        async with sessionmaker()() as db:
            scan = await db.get(Scan, scan_id)
            if not scan or scan.status not in ("UPLOADED", "PROCESSING"):
                return
            prev = scan.status
            res = await db.execute(update(Scan).where(
                Scan.id == scan_id, Scan.status.in_(("UPLOADED", "PROCESSING")),
                or_(Scan.lease_until.is_(None), Scan.lease_until < now)).values(
                lease_until=now + timedelta(seconds=self.s.processing_lease_seconds), proc_step="Detecting", proc_pct=5, proc_found=0))
            if res.rowcount != 1:
                return  # someone else holds the lease
            await db.refresh(scan)
            if prev == "UPLOADED":
                transition(db, scan, "PROCESSING", system_actor(scan.org_id))
            else:
                scan.status = "PROCESSING"
                add_event(db, scan.id, "System", "PROCESSING", "Resumed after interruption")
            await db.commit()
            org_id, key, w, h = scan.org_id, scan.image_key, scan.width, scan.height
            catalog = await active_catalog(db, org_id)
            thresholds = dict((await get_org_settings(db, org_id)).thresholds)
            await db.commit()

        try:
            image = await self.storage.get(key)
            await self._progress(scan_id, "Detecting", 10, 0)
            raw = await asyncio.wait_for(self.inference.detect(image, scan_id=scan_id, catalog=catalog, width=w, height=h),
                                         timeout=self.s.inference_timeout_seconds)
            await self._progress(scan_id, "Matching", 60, len(raw))
            rows = build_detection_rows(raw, width=w, height=h, valid_skus={s for s, _ in catalog}, thresholds=thresholds,
                                        det_conf_min=self.s.det_conf_min, max_n=self.s.max_detections)
            await self._progress(scan_id, "Counting", 85, len(rows))
        except Exception as e:
            log.warning("inference failed", extra={"scan_id": scan_id, "error": repr(e)})
            await self._fail(scan_id, "AI analysis failed. Check that the AI worker and model are available, then retry the scan.")
            return

        async with sessionmaker()() as db:
            scan = await db.get(Scan, scan_id)
            if not scan or scan.status != "PROCESSING":
                return
            for old in await load_dets(db, scan_id):
                await db.delete(old)
            await db.flush()
            db.add_all([Detection(scan_id=scan_id, **r) for r in rows])
            flagged = any(r["needs_review"] for r in rows)
            actor = system_actor(scan.org_id)
            transition(db, scan, "NEEDS_REVIEW" if flagged else "AUTO_READY", actor)
            scan.proc_step, scan.proc_pct, scan.proc_found, scan.lease_until = "Done", 100, len(rows), None
            from app.domain.scans import shelf_name
            notify(db, [scan.created_by], "PROCESSED", f"{await shelf_name(db, scan.shelf_id)} finished processing", scan.id)
            await db.commit()

    async def recover_once(self) -> int:
        async with sessionmaker()() as db:
            ids = (await db.scalars(select(Scan.id).where(
                Scan.status.in_(("UPLOADED", "PROCESSING")), or_(Scan.lease_until.is_(None), Scan.lease_until < utcnow())).limit(20))).all()
        for i in ids:
            self.start(i)
        return len(ids)

    def start_recovery_loop(self) -> None:
        if self.s.recovery_interval_seconds <= 0:
            return

        async def loop() -> None:
            while True:
                try:
                    await self.recover_once()
                except Exception:
                    log.exception("recovery sweep failed")
                await asyncio.sleep(self.s.recovery_interval_seconds)
        self._loop_task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
        await self.wait_idle()
