"""Catalog (products + reference photos + new-product requests), mock WMS expected stock, stores and shelves."""
from __future__ import annotations

import asyncio
import csv
import io
import re
from typing import Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import db as dbmod
from app.config import Settings
from app.db import get_db
from app.deps import actor_for, get_inference, get_settings_dep, get_storage, require_permission
from app.domain import catalog as cat
from app.domain.common import audit, get_org_settings, iso, notify, user_ids_with_roles
from app.domain.uploads import read_limited
from app.errors import ApiError, bad_request, conflict, forbidden, not_found, unprocessable
from app.infra.imaging import ImageRejected, acrop_region, aprocess_scan_image
from app.infra.inference import InferenceError
from app.models import (Detection, ExpectedStock, ImportBatch, Product, ProductRequest, ReferenceImage, Scan, Shelf, Store, User, utcnow)
from app.permissions import can

router = APIRouter(tags=["catalog"])
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_REFS_PER_PRODUCT = cat.MAX_REFS_PER_PRODUCT
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 20000


# ------------------------------------------------------------------ products
class ProductIn(BaseModel):
    sku: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=200)
    min_qty: int = Field(default=0, ge=0, le=1_000_000)


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    min_qty: int | None = Field(default=None, ge=0, le=1_000_000)
    active: bool | None = None


async def _product(db: AsyncSession, org_id: str, sku: str) -> Product:
    p = await db.scalar(select(Product).where(Product.org_id == org_id, Product.sku_key == sku.lower()))
    if p is None:
        raise not_found("Product not found.")
    return p


async def _product_views(db: AsyncSession, storage, products: list[Product], slim: bool = False) -> list[dict]:
    refs: dict[str, list[ReferenceImage]] = {}
    counts: dict[str, int] = {}
    ids = [p.id for p in products]
    for i in range(0, len(ids), 500):  # chunked: PostgreSQL allows at most 32767 bind parameters per statement
        chunk = ids[i:i + 500]
        if slim:
            for pid, n in await db.execute(select(ReferenceImage.product_id, func.count()).where(ReferenceImage.product_id.in_(chunk)).group_by(ReferenceImage.product_id)):
                counts[pid] = n
        else:
            for r in await db.scalars(select(ReferenceImage).where(ReferenceImage.product_id.in_(chunk)).order_by(ReferenceImage.created_at)):
                refs.setdefault(r.product_id, []).append(r)
    out = []
    for p in products:
        n = counts.get(p.id, 0) if slim else len(refs.get(p.id, []))
        v = {"sku": p.sku, "name": p.name, "min_qty": p.min_qty, "active": p.active, "reference_count": n,
             "barcode": p.barcode, "brand": p.brand, "source": p.source, "source_url": p.source_url,
             "indexed_at": iso(p.indexed_at), "status": cat.product_status(p.indexed_at, n),
             "image_license": p.image_license, "image_attribution": p.image_attribution,
             "references": [] if slim else [{"id": r.id, "source": r.source, "thumb_url": storage.url(r.thumb_key), "url": storage.url(r.storage_key)} for r in refs.get(p.id, [])]}
        out.append(v)
    return out


@router.get("/catalog/products")
async def list_products(include_inactive: bool = False, q: str | None = Query(default=None, max_length=100), limit: int | None = Query(default=None, ge=1, le=1000),
                        offset: int = Query(default=0, ge=0), slim: bool = False, status: Literal["ready", "pending", "failed"] | None = None,
                        db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view")), storage=Depends(get_storage)):
    """No `limit` = everything (old behaviour). `q` matches SKU, name, brand or barcode. `slim` skips the photo list (counts only)."""
    query = select(Product).where(Product.org_id == user.org_id).order_by(Product.sku)
    if not (include_inactive and can(user.role, "catalog.write")):
        query = query.where(Product.active.is_(True))
    if q and q.strip():
        like = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(func.lower(Product.sku).like(like, escape="\\") | func.lower(Product.name).like(like, escape="\\")
                            | func.lower(func.coalesce(Product.brand, "")).like(like, escape="\\") | func.lower(func.coalesce(Product.barcode, "")).like(like, escape="\\"))
    if status:
        n = select(func.count()).select_from(ReferenceImage).where(ReferenceImage.product_id == Product.id).scalar_subquery()
        query = query.where({"pending": n == 0, "ready": (n > 0) & Product.indexed_at.is_not(None), "failed": (n > 0) & Product.indexed_at.is_(None)}[status])
    if limit:
        query = query.limit(limit).offset(offset)
    return await _product_views(db, storage, list((await db.scalars(query)).all()), slim=slim)


@router.post("/catalog/products", status_code=201)
async def create_product(body: ProductIn, request: Request, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("catalog.write")), storage=Depends(get_storage)):
    sku = body.sku.strip()
    if not SKU_RE.match(sku):
        raise unprocessable("SKU may use letters, numbers, dot, dash and underscore (up to 64 characters).")
    p = Product(org_id=user.org_id, sku=sku, sku_key=sku.lower(), name=body.name.strip(), min_qty=body.min_qty)
    db.add(p)
    audit(db, actor_for(request, user), "PRODUCT_CREATED", sku, after={"name": p.name, "min_qty": p.min_qty, "source": "manual"})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict(f"A product with SKU {sku} already exists.")
    return (await _product_views(db, storage, [p]))[0]


@router.get("/catalog/products/{sku}")
async def get_product(sku: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view")), storage=Depends(get_storage)):
    return (await _product_views(db, storage, [await _product(db, user.org_id, sku)]))[0]


@router.patch("/catalog/products/{sku}")
async def patch_product(sku: str, body: ProductPatch, request: Request, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("catalog.write")), storage=Depends(get_storage), inference=Depends(get_inference)):
    p = await _product(db, user.org_id, sku)
    if body.min_qty is not None and not can(user.role, "alerts.write"):
        raise forbidden()
    before, after = {}, {}
    for f in ("name", "min_qty", "active"):
        v = getattr(body, f)
        if v is not None and v != getattr(p, f):
            before[f], after[f] = getattr(p, f), v.strip() if isinstance(v, str) else v
            setattr(p, f, after[f])
    if after:
        audit(db, actor_for(request, user), "PRODUCT_UPDATED", p.sku, before=before, after=after)
    await db.commit()
    if after.get("active") is False:
        try:  # a deactivated product must stop matching in new scans
            await inference.remove_product(p.sku)
        except InferenceError:
            pass
    return (await _product_views(db, storage, [p]))[0]


@router.delete("/catalog/products/{sku}", status_code=204)
async def delete_product(sku: str, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("catalog.write")),
                         inference=Depends(get_inference)):
    """Soft delete: old scans keep reading correctly; the product just stops being offered and matched."""
    p = await _product(db, user.org_id, sku)
    if p.active:
        p.active = False
        audit(db, actor_for(request, user), "PRODUCT_DEACTIVATED", p.sku)
        await db.commit()
        try:
            await inference.remove_product(p.sku)
        except InferenceError:
            pass
    return Response(status_code=204)


async def _register(db: AsyncSession, storage, inference, s: Settings, request: Request, user: User, p: Product,
                    refs: list[cat.NewRef], audit_extra: dict | None = None) -> None:
    """Persist photos first, then index them. An indexing failure leaves photos in the DB as a retryable failed product."""
    keys: list[str] = []
    try:
        rows, keys = await cat.store_refs(storage, p, refs, user.id)
        db.add_all(rows)
        audit(db, actor_for(request, user), "REFERENCES_ADDED", p.sku,
              after={"count": len(rows), "source": refs[0].source, **(audit_extra or {})})
        await db.commit()
    except BaseException:
        await db.rollback()
        for k in keys:
            await storage.delete(k)
        raise

    try:
        signature = await cat.index_signature(inference)
        # If the product has not been confirmed yet, re-send every stored photo. Otherwise only index the new photos.
        items = await cat.product_items(db, storage, p) if p.indexed_at is None else [(r.rid, r.ref) for r in refs]
        await cat.send_references(inference, s, p.sku, p.name, items)
    except (InferenceError, OSError, KeyError) as e:
        raise ApiError(502, "The photos are saved, but the matching service could not learn them. Retry when it is available.") from e
    cat.mark_indexed(p, signature)
    await db.commit()


@router.post("/catalog/products/{sku}/references", status_code=201)
async def add_references(sku: str, request: Request, files: list[UploadFile] = File(...), db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("catalog.write")), storage=Depends(get_storage),
                         inference=Depends(get_inference), s: Settings = Depends(get_settings_dep)):
    """Register a product from photos: store, embed (AI service upserts to Qdrant). No retraining involved.
    On success the product is searchable immediately (indexed_at is set); the next /analyze or scan can return it."""
    p = await _product(db, user.org_id, sku)
    if not p.active:
        raise conflict("This product is deactivated.")
    if not files or len(files) > cat.BACKEND_REFS_PER_REQUEST:
        raise unprocessable(f"Send between 1 and {cat.BACKEND_REFS_PER_REQUEST} reference photos.")
    have = await db.scalar(select(func.count()).select_from(ReferenceImage).where(ReferenceImage.product_id == p.id))
    if have + len(files) > MAX_REFS_PER_PRODUCT:
        raise unprocessable(f"A product can have at most {MAX_REFS_PER_PRODUCT} reference photos.")
    refs: list[cat.NewRef] = []
    for f in files:
        data = await read_limited(f, s.max_reference_bytes)
        try:
            refs.append(await cat.prepare_ref(data, "upload"))
        except ImageRejected as e:
            raise ApiError(e.status, e.message)
    await _register(db, storage, inference, s, request, user, p, refs)
    return (await _product_views(db, storage, [p]))[0]


@router.post("/catalog/products/{sku}/test-recognition")
async def test_recognition(sku: str, file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_permission("catalog.write")),
                           inference=Depends(get_inference), s: Settings = Depends(get_settings_dep)):
    """Run a one-off detection and catalog search without creating or saving a scan."""
    p = await _product(db, user.org_id, sku)
    if not p.active:
        raise conflict("This product is deactivated.")
    n = await db.scalar(select(func.count()).select_from(ReferenceImage).where(ReferenceImage.product_id == p.id))
    if not n or p.indexed_at is None:
        raise conflict("This product is not searchable yet. Add reference photos and wait for Ready status.")
    data = await read_limited(file, s.max_upload_bytes)
    try:
        img = await aprocess_scan_image(data, s)
    except ImageRejected as e:
        raise ApiError(e.status, e.message)
    products = (await db.scalars(select(Product).where(Product.org_id == user.org_id, Product.active.is_(True)).order_by(Product.sku))).all()
    names = {item.sku_key: item.name for item in products}
    raw = await inference.detect(img.original, scan_id="catalog-preview", catalog=[(item.sku, item.name) for item in products],
                                 width=img.width, height=img.height)
    margin = float((await get_org_settings(db, user.org_id)).thresholds.get("margin", 0.08))
    detections = []
    for det in raw[:s.max_detections]:
        matches = [{"sku": m.sku, "name": names.get(m.sku.lower(), m.sku), "score": m.score} for m in det.matches if m.sku.lower() in names]
        gap = matches[0]["score"] - matches[1]["score"] if len(matches) > 1 else None
        detections.append({"bbox": det.bbox, "detector_score": det.det_confidence, "matches": matches,
                           "target_score": next((m["score"] for m in matches if m["sku"].lower() == p.sku_key), None),
                           "close_match": gap is not None and gap < margin, "match_probability": det.match_probability})
    return {"sku": p.sku, "name": p.name, "detections": detections, "close_match_margin": margin,
            "scan_created": False}


class FromDetectionIn(BaseModel):
    scan_id: str = Field(max_length=64)
    detection_id: str = Field(max_length=32)


@router.post("/catalog/products/{sku}/references/from-detection", status_code=201)
async def add_reference_from_detection(sku: str, body: FromDetectionIn, request: Request, db: AsyncSession = Depends(get_db),
                                       user: User = Depends(require_permission("catalog.write")), storage=Depends(get_storage),
                                       inference=Depends(get_inference), s: Settings = Depends(get_settings_dep)):
    """Close the loop: a reviewer corrected a box to this SKU, so keep that shelf crop as an extra reference photo
    (source='review_crop'). Shelf crops close the packshot-vs-shelf gap the catalog photos leave."""
    p = await _product(db, user.org_id, sku)
    if not p.active:
        raise conflict("This product is deactivated.")
    scan = await db.get(Scan, body.scan_id)
    if scan is None or scan.org_id != user.org_id or not scan.image_key:
        raise not_found("Scan not found.")
    det = await db.get(Detection, (scan.id, body.detection_id))
    if det is None or det.deleted:
        raise not_found("Box not found.")
    if det.state != "CORRECTED" or (det.assigned_sku or "").lower() != p.sku_key:
        raise conflict("Only a box a reviewer assigned to this product can become a reference photo.")
    have = await db.scalar(select(func.count()).select_from(ReferenceImage).where(ReferenceImage.product_id == p.id))
    if have + 1 > MAX_REFS_PER_PRODUCT:
        raise unprocessable(f"A product can have at most {MAX_REFS_PER_PRODUCT} reference photos.")
    try:
        crop = await acrop_region(await storage.get(scan.image_key), (det.x1, det.y1, det.x2, det.y2))
        ref = await cat.prepare_ref(crop, "review_crop")
    except ImageRejected as e:
        raise ApiError(e.status, e.message)
    idx = await cat.PhashIndex.load(db, user.org_id, 0)  # exact duplicates only: a second look at the same crop adds nothing
    hit = idx.find(ref.content_hash, ref.phash)
    if hit and hit[0].lower() == p.sku_key:
        raise conflict("This crop is already a reference photo of this product.")
    await _register(db, storage, inference, s, request, user, p, [ref], {"scan_id": scan.id, "detection_id": det.id})
    return (await _product_views(db, storage, [p]))[0]


@router.post("/catalog/products/{sku}/reindex")
async def reindex(sku: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("catalog.write")), storage=Depends(get_storage),
                  inference=Depends(get_inference), s: Settings = Depends(get_settings_dep)):
    """Retry for a product whose photos are stored but were never confirmed by the matching service ("Failed")."""
    p = await _product(db, user.org_id, sku)
    if not p.active:
        raise conflict("This product is deactivated.")
    try:
        n = await cat.reindex_product(db, storage, inference, s, p, await cat.index_signature(inference))
    except (InferenceError, OSError, KeyError) as e:
        await db.rollback()
        raise ApiError(502, "The matching service could not learn these photos. Try again in a moment.") from e
    if n == 0:
        raise unprocessable("This product has no reference photos yet.")
    return (await _product_views(db, storage, [p]))[0]


# ------------------------------------------------------------------ search-index health, resync, batches, credits
@router.get("/catalog/index-status")
async def index_status(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("catalog.write")), inference=Depends(get_inference)):
    return await cat.index_status(db, inference, user.org_id)


@router.post("/catalog/resync", status_code=202)
async def resync(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("catalog.resync")),
                 storage=Depends(get_storage), inference=Depends(get_inference), s: Settings = Depends(get_settings_dep)):
    """Owner only. Re-sends every ACTIVE product's stored photos to the worker in the background (fixes a wiped Qdrant or a
    changed DINO_MODEL / QDRANT_COLLECTION). Poll GET /catalog/batches/{id}."""
    running = request.app.state.resync_running
    if user.org_id in running:
        raise conflict("A resync is already running.")
    if not await inference.ping():
        raise ApiError(503, "The matching service is not reachable.")
    b = await cat.start_batch(db, user.org_id, "resync", {"by": user.id})
    actor = actor_for(request, user)
    running.add(user.org_id)

    async def job():
        try:
            await cat.resync_org(dbmod.sessionmaker(), storage, inference, s, user.org_id, b.id, actor)
        except Exception as e:  # noqa: BLE001
            async with dbmod.sessionmaker()() as session:
                row = await session.get(ImportBatch, b.id)
                row.finished_at = utcnow()
                cat.batch_error(row, "resync", "fatal", repr(e))
                await session.commit()
        finally:
            running.discard(user.org_id)

    task = asyncio.create_task(job())
    request.app.state.bg_tasks.add(task)
    task.add_done_callback(request.app.state.bg_tasks.discard)
    return {"batch_id": b.id, "running": True}


@router.get("/catalog/batches")
async def list_batches(source: str | None = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("catalog.write"))):
    q = select(ImportBatch).where(ImportBatch.org_id == user.org_id).order_by(ImportBatch.started_at.desc()).limit(50)
    if source:
        q = q.where(ImportBatch.source == source)
    return [cat.batch_view(b) for b in await db.scalars(q)]


@router.get("/catalog/batches/{batch_id}")
async def get_batch(batch_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("catalog.write"))):
    b = await db.get(ImportBatch, batch_id)
    if b is None or b.org_id != user.org_id:
        raise not_found("Batch not found.")
    return cat.batch_view(b)


@router.get("/catalog/credits")
async def credits(q: str | None = Query(default=None, max_length=100), limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
                  db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    """Image credits: every product whose photos carry a licence (imported from Open Food Facts). Any signed-in user may read it."""
    base = select(Product).where(Product.org_id == user.org_id, Product.image_license.is_not(None))
    if q and q.strip():
        like = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        base = base.where(func.lower(Product.name).like(like, escape="\\") | func.lower(Product.sku).like(like, escape="\\"))
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await db.scalars(base.order_by(Product.sku).limit(limit).offset(offset))).all()
    return {"total": total, "items": [{"sku": p.sku, "name": p.name, "brand": p.brand, "license": p.image_license,
                                       "attribution": p.image_attribution, "source_url": p.source_url} for p in rows]}


@router.delete("/catalog/products/{sku}/references/{ref_id}", status_code=204)
async def delete_reference(sku: str, ref_id: str, request: Request, db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_permission("catalog.write")), storage=Depends(get_storage),
                           inference=Depends(get_inference)):
    p = await _product(db, user.org_id, sku)
    r = await db.get(ReferenceImage, ref_id)
    if r is None or r.product_id != p.id:
        raise not_found("Reference photo not found.")
    # Keep enough source data to compensate if the relational delete cannot commit.
    # The worker uses deterministic IDs, so adding it again is safe to retry.
    reference_bytes = await storage.get(r.storage_key)
    reference_id = r.id
    reference_name = p.name
    try:
        # Worker deletion is idempotent. If it is unavailable, keep the DB row so the caller can safely retry.
        await inference.remove_reference(p.sku, r.id)
    except InferenceError as e:
        raise ApiError(503, "The matching service could not remove this photo from its index. Retry when it is available.") from e
    keys = [r.storage_key, r.thumb_key]
    await db.delete(r)
    if await db.scalar(select(func.count()).select_from(ReferenceImage).where(ReferenceImage.product_id == p.id)) == 0:
        p.indexed_at = None
    audit(db, actor_for(request, user), "REFERENCE_REMOVED", p.sku)
    try:
        await db.commit()
    except BaseException:
        await db.rollback()
        try:
            await inference.add_references(p.sku, reference_name, [(reference_id, reference_bytes)])
        except InferenceError:
            # The catalog row remains authoritative; the Owner resync operation can
            # restore the missing vector if the worker is still unavailable.
            pass
        raise
    for k in keys:
        await storage.delete(k)
    return Response(status_code=204)


# ------------------------------------------------------------------ product requests (Staff -> Manager)
class RequestIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class DecideIn(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    note: str | None = Field(default=None, max_length=1000)


def _req_view(r: ProductRequest, names: dict[str, str]) -> dict:
    from app.domain.common import iso
    return {"id": r.id, "name": r.name, "status": r.status, "note": r.note, "requested_by": {"id": r.requested_by, "name": names.get(r.requested_by, "")},
            "decided_by": r.decided_by, "decided_at": iso(r.decided_at), "created_at": iso(r.created_at)}


@router.post("/catalog/product-requests", status_code=201)
async def create_request(body: RequestIn, request: Request, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("catalog.request"))):
    r = ProductRequest(org_id=user.org_id, name=body.name.strip(), requested_by=user.id, note=(body.note or "").strip() or None)
    db.add(r)
    await db.flush()
    notify(db, await user_ids_with_roles(db, user.org_id, ("MANAGER", "OWNER")), "SUBMITTED", f"{user.name} requested a new product: {r.name}")
    audit(db, actor_for(request, user), "PRODUCT_REQUESTED", r.name)
    await db.commit()
    return _req_view(r, {user.id: user.name})


@router.get("/catalog/product-requests")
async def list_requests(status: str | None = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    if not (can(user.role, "catalog.write") or can(user.role, "catalog.request")):
        raise forbidden()
    q = select(ProductRequest).where(ProductRequest.org_id == user.org_id).order_by(ProductRequest.created_at.desc()).limit(200)
    if user.role == "STAFF":
        q = q.where(ProductRequest.requested_by == user.id)
    if status:
        q = q.where(ProductRequest.status == status)
    rows = list((await db.scalars(q)).all())
    names = {u.id: u.name for u in await db.scalars(select(User).where(User.id.in_({r.requested_by for r in rows})))} if rows else {}
    return [_req_view(r, names) for r in rows]


@router.post("/catalog/product-requests/{req_id}/decision")
async def decide_request(req_id: str, body: DecideIn, request: Request, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("catalog.write"))):
    r = await db.scalar(select(ProductRequest).where(ProductRequest.id == req_id, ProductRequest.org_id == user.org_id).with_for_update())
    if r is None:
        raise not_found("Request not found.")
    if r.status != "PENDING":
        raise conflict("This request was already decided.")
    r.status, r.decided_by, r.decided_at = body.decision, user.id, utcnow()
    if body.note:
        r.note = body.note.strip()
    notify(db, [r.requested_by], "APPROVED" if body.decision == "APPROVED" else "RETURNED",
           f"Your request for {r.name} was {body.decision.lower()}")
    audit(db, actor_for(request, user), f"PRODUCT_REQUEST_{body.decision}", r.name)
    await db.commit()
    names = {u.id: u.name for u in await db.scalars(select(User).where(User.id == r.requested_by))}
    return _req_view(r, names)


# ------------------------------------------------------------------ stores and shelves
class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ShelfIn(BaseModel):
    store_id: str
    name: str = Field(min_length=1, max_length=200)


@router.get("/shelves")
async def list_shelves(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    stores = {st.id: st.name for st in await db.scalars(select(Store).where(Store.org_id == user.org_id))}
    shelves = await db.scalars(select(Shelf).where(Shelf.org_id == user.org_id).order_by(Shelf.name))
    return [{"id": sh.id, "name": sh.name, "store_id": sh.store_id, "store_name": stores.get(sh.store_id, "")} for sh in shelves]


@router.get("/stores")
async def list_stores(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    return [{"id": st.id, "name": st.name} for st in await db.scalars(select(Store).where(Store.org_id == user.org_id).order_by(Store.name))]


@router.post("/stores", status_code=201)
async def create_store(body: StoreIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("users.manage"))):
    st = Store(org_id=user.org_id, name=body.name.strip(), name_key=body.name.strip().lower())
    db.add(st)
    audit(db, actor_for(request, user), "STORE_CREATED", st.name)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict("A store with that name already exists.")
    return {"id": st.id, "name": st.name}


@router.post("/shelves", status_code=201)
async def create_shelf(body: ShelfIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("users.manage"))):
    st = await db.get(Store, body.store_id)
    if st is None or st.org_id != user.org_id:
        raise not_found("Store not found.")
    sh = Shelf(org_id=user.org_id, store_id=st.id, name=body.name.strip(), name_key=body.name.strip().lower())
    db.add(sh)
    audit(db, actor_for(request, user), "SHELF_CREATED", f"{st.name} / {sh.name}")
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict("That store already has a shelf with this name.")
    return {"id": sh.id, "name": sh.name, "store_id": st.id, "store_name": st.name}


# ------------------------------------------------------------------ expected stock (mock WMS)
class ExpectedIn(BaseModel):
    shelf_id: str
    sku: str = Field(max_length=64)
    expected_qty: int = Field(ge=0, le=10_000_000)


def _exp_view(e: ExpectedStock) -> dict:
    return {"shelf_id": e.shelf_id, "sku": e.sku, "expected_qty": e.expected_qty}


@router.get("/wms/expected")
async def get_expected(shelf_id: str | None = Query(default=None), db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("scan.view"))):
    q = select(ExpectedStock).where(ExpectedStock.org_id == user.org_id).order_by(ExpectedStock.shelf_id, ExpectedStock.sku)
    if shelf_id:
        q = q.where(ExpectedStock.shelf_id == shelf_id)
    return [_exp_view(e) for e in await db.scalars(q)]


@router.put("/wms/expected")
async def put_expected(body: ExpectedIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("stock.write"))):
    shelf = await db.get(Shelf, body.shelf_id)
    if shelf is None or shelf.org_id != user.org_id:
        raise not_found("Shelf not found.")
    prod = await _product(db, user.org_id, body.sku)
    row = await db.scalar(select(ExpectedStock).where(ExpectedStock.shelf_id == shelf.id, ExpectedStock.sku == prod.sku))
    before = row.expected_qty if row else None
    if row is None:
        row = ExpectedStock(org_id=user.org_id, shelf_id=shelf.id, sku=prod.sku, expected_qty=body.expected_qty)
        db.add(row)
    else:
        row.expected_qty, row.updated_at = body.expected_qty, utcnow()
    audit(db, actor_for(request, user), "EXPECTED_STOCK_SET", f"{shelf.name} / {prod.sku}", before=before, after=body.expected_qty)
    await db.commit()
    return _exp_view(row)


@router.post("/wms/expected/import")
async def import_expected(request: Request, file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission("stock.write"))):
    """CSV columns: shelf (name or id), sku, expected_qty. All-or-nothing: any bad row rejects the file with row numbers."""
    data = await read_limited(file, MAX_CSV_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise bad_request("The file must be UTF-8 text (save as CSV UTF-8).")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise bad_request("The file is empty.")
    cols = {(c or "").strip().lower(): c for c in reader.fieldnames}
    missing = [c for c in ("shelf", "sku", "expected_qty") if c not in cols]
    if missing:
        raise bad_request(f"Missing column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}.")
    shelves = list(await db.scalars(select(Shelf).where(Shelf.org_id == user.org_id)))
    by_key: dict[str, Shelf] = {}
    for sh in shelves:
        by_key[sh.id] = sh
        by_key.setdefault(sh.name.lower(), sh)
    skus = {p.sku_key: p.sku for p in await db.scalars(select(Product).where(Product.org_id == user.org_id))}
    parsed: dict[tuple[str, str], int] = {}
    errors: list[str] = []
    for n, row in enumerate(reader, start=2):
        if n - 1 > MAX_CSV_ROWS:
            raise unprocessable(f"Too many rows (limit {MAX_CSV_ROWS}).")
        shelf_cell = (row.get(cols["shelf"]) or "").strip()
        sku_cell = (row.get(cols["sku"]) or "").strip()
        qty_cell = (row.get(cols["expected_qty"]) or "").strip()
        if not (shelf_cell or sku_cell or qty_cell):
            continue
        sh = by_key.get(shelf_cell) or by_key.get(shelf_cell.lower())
        if sh is None:
            errors.append(f"row {n}: unknown shelf '{shelf_cell[:40]}'")
        elif sku_cell.lower() not in skus:
            errors.append(f"row {n}: unknown SKU '{sku_cell[:40]}'")
        elif not re.fullmatch(r"\d{1,8}", qty_cell):
            errors.append(f"row {n}: expected_qty must be a whole number, 0 or more")
        else:
            parsed[(sh.id, skus[sku_cell.lower()])] = int(qty_cell)  # a later row for the same shelf+SKU wins
        if len(errors) >= 10:
            break
    if errors:
        raise unprocessable("Import rejected, nothing was changed. " + "; ".join(errors))
    if not parsed:
        raise bad_request("The file has no data rows.")
    existing = {(e.shelf_id, e.sku): e for e in await db.scalars(select(ExpectedStock).where(ExpectedStock.org_id == user.org_id))}
    created = updated = 0
    for (shelf_id, sku), qty in parsed.items():
        e = existing.get((shelf_id, sku))
        if e is None:
            db.add(ExpectedStock(org_id=user.org_id, shelf_id=shelf_id, sku=sku, expected_qty=qty))
            created += 1
        elif e.expected_qty != qty:
            e.expected_qty, e.updated_at = qty, utcnow()
            updated += 1
    audit(db, actor_for(request, user), "EXPECTED_STOCK_IMPORTED", file.filename or "csv", after=f"{created} added, {updated} changed")
    await db.commit()
    return {"rows": len(parsed), "created": created, "updated": updated}
