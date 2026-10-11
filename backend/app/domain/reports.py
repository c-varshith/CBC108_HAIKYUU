"""Reports and alerts, computed from FROZEN approval snapshots (scan_summaries), so history is stable."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Detection, Product, Scan, ScanSummary, utcnow


async def latest_approved_rows(db: AsyncSession, org_id: str) -> list[tuple[str, list[dict[str, Any]]]]:
    """For each shelf: the summary rows of its most recently approved scan."""
    latest = (select(Scan.shelf_id.label("shelf_id"), func.max(ScanSummary.frozen_at).label("mx"))
              .join(ScanSummary, ScanSummary.scan_id == Scan.id)
              .where(Scan.org_id == org_id, Scan.status == "APPROVED").group_by(Scan.shelf_id).subquery())
    q = (select(Scan.shelf_id, ScanSummary.rows).join(ScanSummary, ScanSummary.scan_id == Scan.id)
         .join(latest, and_(latest.c.shelf_id == Scan.shelf_id, latest.c.mx == ScanSummary.frozen_at))
         .where(Scan.org_id == org_id, Scan.status == "APPROVED"))
    seen: dict[str, list[dict[str, Any]]] = {}
    for shelf_id, rows in (await db.execute(q)):
        seen.setdefault(shelf_id, rows)
    return list(seen.items())


async def on_hand_by_sku(db: AsyncSession, org_id: str) -> dict[str, int]:
    """Reviewed units per SKU summed across each shelf's latest approved scan. SKUs never counted are absent."""
    out: dict[str, int] = defaultdict(int)
    for _, rows in await latest_approved_rows(db, org_id):
        for r in rows:
            out[r["sku"]] += int(r["reviewed"])
    return dict(out)


async def low_stock(db: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    on_hand = await on_hand_by_sku(db, org_id)
    prods = await db.scalars(select(Product).where(Product.org_id == org_id, Product.active.is_(True), Product.min_qty > 0).order_by(Product.sku))
    out = []
    for p in prods:
        if p.sku in on_hand and on_hand[p.sku] < p.min_qty:
            oh = on_hand[p.sku]
            out.append({"sku": p.sku, "name": p.name, "on_hand": oh, "min_qty": p.min_qty, "reorder": max(p.min_qty * 2 - oh, 0)})
    return out


async def low_stock_skus(db: AsyncSession, org_id: str) -> set[str]:
    return {r["sku"] for r in await low_stock(db, org_id)}


async def summary_report(db: AsyncSession, org_id: str) -> dict[str, Any]:
    now = utcnow()
    # shrinkage per day, last 8 days (oldest first): units short across scans approved that day
    start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await db.execute(select(ScanSummary.frozen_at, ScanSummary.rows).join(Scan, Scan.id == ScanSummary.scan_id)
                            .where(Scan.org_id == org_id, Scan.status == "APPROVED", ScanSummary.frozen_at >= start))
    per_day = [0] * 8
    for at, srows in rows:
        idx = (at.replace(hour=0, minute=0, second=0, microsecond=0) - start).days
        if 0 <= idx < 8:
            per_day[idx] += sum(max(-int(r["variance"]), 0) for r in srows)

    # accuracy = share of boxes the matcher resolved by itself (still SURE, never touched), last 30 days
    since = now - timedelta(days=30)
    agg = (await db.execute(
        select(func.count(), func.sum(case((Detection.state == "SURE", 1), else_=0)))
        .join(Scan, Scan.id == Detection.scan_id)
        .where(Scan.org_id == org_id, Scan.created_at >= since, Scan.status.in_(("SUBMITTED", "APPROVED")), Detection.deleted.is_(False)))).one()
    total, sure = int(agg[0] or 0), int(agg[1] or 0)

    # current position: per-SKU variance summed over each shelf's latest approved scan
    var: dict[str, int] = defaultdict(int)
    for _, srows in await latest_approved_rows(db, org_id):
        for r in srows:
            var[r["sku"]] += int(r["variance"])
    return {"shrinkage_trend": per_day, "accuracy": round(100 * sure / total) if total else 0,
            "short_skus": sum(1 for v in var.values() if v < 0), "surplus_skus": sum(1 for v in var.values() if v > 0),
            "total_shrinkage": sum(-v for v in var.values() if v < 0)}
