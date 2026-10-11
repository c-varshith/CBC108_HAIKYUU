"""Reports and alerts, computed from FROZEN approval snapshots (scan_summaries), so history is stable."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Detection, ExpectedStock, Product, Scan, ScanSummary, Shelf, utcnow


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


async def inventory_report(db: AsyncSession, org_id: str) -> dict[str, Any]:
    """Catalog-wide counts from each shelf's latest approved scan, plus configured expected stock."""
    products = list(await db.scalars(
        select(Product).where(Product.org_id == org_id, Product.active.is_(True)).order_by(Product.name, Product.sku)
    ))
    expected: dict[str, int] = defaultdict(int)
    for sku, qty in await db.execute(
        select(ExpectedStock.sku, ExpectedStock.expected_qty).where(ExpectedStock.org_id == org_id)
    ):
        expected[sku] += int(qty)

    snapshots = await latest_approved_rows(db, org_id)
    counts: dict[str, int] = defaultdict(int)
    for _, rows in snapshots:
        for row in rows:
            counts[row["sku"]] += int(row["reviewed"])

    total_shelves = int(await db.scalar(select(func.count()).select_from(Shelf).where(Shelf.org_id == org_id)) or 0)
    last_counted = await db.scalar(
        select(func.max(ScanSummary.frozen_at)).join(Scan, Scan.id == ScanSummary.scan_id)
        .where(Scan.org_id == org_id, Scan.status == "APPROVED")
    )
    has_counts = bool(snapshots)
    complete_coverage = has_counts and len(snapshots) >= total_shelves
    items = []
    for product in products:
        on_hand = counts.get(product.sku, 0) if has_counts else None
        expected_qty = expected.get(product.sku, 0)
        items.append({
            "sku": product.sku,
            "name": product.name,
            "on_hand": on_hand,
            "expected_qty": expected_qty,
            "variance": on_hand - expected_qty if on_hand is not None and complete_coverage else None,
            "min_qty": product.min_qty,
            "status": "NOT_COUNTED" if on_hand is None else "PARTIAL" if not complete_coverage else "LOW" if product.min_qty > 0 and on_hand < product.min_qty else "IN_STOCK",
        })

    return {
        "product_count": len(items),
        "total_on_hand": sum(item["on_hand"] or 0 for item in items) if has_counts else None,
        "total_expected": sum(item["expected_qty"] for item in items),
        "counted_shelves": len(snapshots),
        "total_shelves": total_shelves,
        "last_counted_at": last_counted.isoformat() if last_counted else None,
        "items": items,
    }


async def low_stock(db: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    on_hand = await on_hand_by_sku(db, org_id)
    prods = await db.scalars(select(Product).where(Product.org_id == org_id, Product.active.is_(True), Product.min_qty > 0).order_by(Product.sku))
    out = []
    for p in prods:
        if p.sku in on_hand and on_hand[p.sku] < p.min_qty:
            oh = on_hand[p.sku]
            # Bring stock back to its configured minimum; avoid a hidden multiplier here.
            out.append({"sku": p.sku, "name": p.name, "on_hand": oh, "min_qty": p.min_qty, "reorder": p.min_qty - oh})
    return out


async def low_stock_skus(db: AsyncSession, org_id: str) -> set[str]:
    return {r["sku"] for r in await low_stock(db, org_id)}


async def summary_report(db: AsyncSession, org_id: str) -> dict[str, Any]:
    now = utcnow()
    active_skus = set((await db.scalars(
        select(Product.sku).where(Product.org_id == org_id, Product.active.is_(True))
    )).all())
    # shrinkage per day, last 8 days (oldest first): units short across scans approved that day
    start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=8)
    rows = await db.execute(select(ScanSummary.frozen_at, ScanSummary.rows).join(Scan, Scan.id == ScanSummary.scan_id)
                            .where(Scan.org_id == org_id, Scan.status == "APPROVED",
                                   ScanSummary.frozen_at >= start, ScanSummary.frozen_at < end))
    days = [
        {"date": (start + timedelta(days=i)).date().isoformat(), "units_short": 0, "approved_scans": 0}
        for i in range(8)
    ]
    for at, srows in rows:
        idx = (at.replace(hour=0, minute=0, second=0, microsecond=0) - start).days
        if 0 <= idx < 8:
            days[idx]["units_short"] += sum(max(-int(r["variance"]), 0) for r in srows if r["sku"] in active_skus)
            days[idx]["approved_scans"] += 1

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
            if r["sku"] in active_skus:
                var[r["sku"]] += int(r["variance"])
    return {"shrinkage_trend": [day["units_short"] for day in days], "shrinkage_days": days,
            "accuracy": round(100 * sure / total) if total else 0,
            "auto_resolved_boxes": sure, "processed_boxes": total,
            "short_skus": sum(1 for v in var.values() if v < 0), "surplus_skus": sum(1 for v in var.values() if v > 0),
            "total_shrinkage": sum(-v for v in var.values() if v < 0)}
