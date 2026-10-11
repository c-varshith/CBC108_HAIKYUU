"""Catalog domain: reference-photo registration, worker indexing, resync, import batches.

Shared by the HTTP router, the catalog importer and the resync script, so all three index photos the same way:
photos are stored first, the AI worker embeds them into Qdrant (no retraining), and `Product.indexed_at` is set ONLY
after the worker acknowledged."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.common import Actor, audit
from app.infra.imaging import ainspect_image, aprocess_reference, hamming
from app.infra.inference import InferenceClient, InferenceError
from app.infra.storage import Storage
from app.models import ImportBatch, Product, ReferenceImage, new_id, utcnow

log = logging.getLogger("calibrex.catalog")

BACKEND_REFS_PER_REQUEST = 10  # POST /catalog/products/{sku}/references accepts at most this many photos
MAX_REFS_PER_PRODUCT = 20
MAX_ERROR_LOG = 500
PHASH_BANDS = 4  # multi-index hashing: with <= 3 differing bits one of 4 16-bit bands matches exactly


def batch_size(s: Settings) -> int:
    """Photos per worker call: the smaller of the backend limit (10) and the worker limit (12 by default)."""
    return max(1, min(BACKEND_REFS_PER_REQUEST, s.ai_worker_max_refs_per_call))


# ---------------------------------------------------------------- status
def product_status(indexed_at: Any, reference_count: int) -> str:
    """ready: photos AND the worker confirmed them. pending: no photos yet. failed: photos stored, worker never confirmed."""
    if reference_count == 0:
        return "pending"
    return "ready" if indexed_at is not None else "failed"


async def index_signature(inference: InferenceClient) -> str | None:
    """'<DINO_MODEL>@<QDRANT_COLLECTION>' of the worker right now, or None when unknown/unreachable."""
    try:
        info = await inference.info()
    except Exception:  # noqa: BLE001 - never block an upload because /health is slow
        return None
    if not info.get("model") or not info.get("collection"):
        return None
    return f"{info['model']}@{info['collection']}"


def mark_indexed(p: Product, signature: str | None) -> None:
    p.indexed_at, p.indexed_with = utcnow(), signature


# ---------------------------------------------------------------- reference preparation / storage
@dataclass
class NewRef:
    rid: str
    ref: bytes  # <=512 px JPEG sent to the worker and kept in storage
    thumb: bytes
    content_hash: str
    phash: str
    source: str  # upload | import | review_crop


async def prepare_ref(data: bytes, source: str) -> NewRef:
    """Validate + normalise (raises ImageRejected) and fingerprint the ORIGINAL bytes."""
    ref, thumb = await aprocess_reference(data)
    info = await ainspect_image(data)
    return NewRef(new_id(), ref, thumb, info.sha256, info.phash, source)


async def store_refs(storage: Storage, p: Product, refs: list[NewRef], created_by: str | None) -> tuple[list[ReferenceImage], list[str]]:
    rows, keys = [], []
    for r in refs:
        k1, k2 = f"catalog/{p.id}/{r.rid}.jpg", f"catalog/{p.id}/{r.rid}.thumb.jpg"
        await storage.put(k1, r.ref, "image/jpeg")
        keys.append(k1)
        await storage.put(k2, r.thumb, "image/jpeg")
        keys.append(k2)
        rows.append(ReferenceImage(id=r.rid, product_id=p.id, storage_key=k1, thumb_key=k2, created_by=created_by,
                                   source=r.source, content_hash=r.content_hash, phash=r.phash))
    return rows, keys


# ---------------------------------------------------------------- worker
async def send_references(inference: InferenceClient, s: Settings, sku: str, name: str, items: list[tuple[str, bytes]]) -> int:
    """Send photos to the worker in batches (<= min(backend 10, worker 12)). Idempotent: the worker's point ids derive from
    the reference ids, so re-sending overwrites. Raises InferenceError when any batch is not acknowledged."""
    total, n = 0, batch_size(s)
    for i in range(0, len(items), n):
        added = await inference.add_references(sku, name, items[i:i + n])
        if added < 1:
            raise InferenceError("the matching service acknowledged 0 vectors")
        total += added
    return total


async def product_items(db: AsyncSession, storage: Storage, p: Product) -> list[tuple[str, bytes]]:
    refs = await db.scalars(select(ReferenceImage).where(ReferenceImage.product_id == p.id).order_by(ReferenceImage.created_at, ReferenceImage.id))
    return [(r.id, await storage.get(r.storage_key)) for r in refs]


async def reindex_product(db: AsyncSession, storage: Storage, inference: InferenceClient, s: Settings, p: Product,
                          signature: str | None) -> int:
    """Re-send every stored photo of one product. Commits on success. Returns the number of photos sent (0 = nothing to send)."""
    items = await product_items(db, storage, p)
    if not items:
        return 0
    await send_references(inference, s, p.sku, p.name, items)
    mark_indexed(p, signature)
    await db.commit()
    return len(items)


# ---------------------------------------------------------------- duplicate detection
class PhashIndex:
    """In-memory near-duplicate lookup over (content_hash, phash) of every reference photo in an organisation."""

    def __init__(self, max_distance: int = 2):
        if not 0 <= max_distance < PHASH_BANDS:
            raise ValueError(f"max_distance must be 0..{PHASH_BANDS - 1}")
        self.max_distance = max_distance
        self._sha: dict[str, str] = {}
        self._bands: list[dict[str, list[tuple[str, str]]]] = [{} for _ in range(PHASH_BANDS)]

    def add(self, sku: str, content_hash: str | None, phash: str | None) -> None:
        if content_hash:
            self._sha.setdefault(content_hash, sku)
        if phash:
            for i in range(PHASH_BANDS):
                self._bands[i].setdefault(phash[i * 4:(i + 1) * 4], []).append((phash, sku))

    def find(self, content_hash: str, phash: str) -> tuple[str, str] | None:
        """(sku, reason) of an existing identical / near-identical photo, else None."""
        if content_hash in self._sha:
            return self._sha[content_hash], "identical file"
        for i in range(PHASH_BANDS):
            for other, sku in self._bands[i].get(phash[i * 4:(i + 1) * 4], ()):
                if hamming(phash, other) <= self.max_distance:
                    return sku, "near-identical image"
        return None

    @classmethod
    async def load(cls, db: AsyncSession, org_id: str, max_distance: int = 2) -> "PhashIndex":
        idx = cls(max_distance)
        q = select(Product.sku, ReferenceImage.content_hash, ReferenceImage.phash).join(
            ReferenceImage, ReferenceImage.product_id == Product.id).where(Product.org_id == org_id)
        for sku, ch, ph in await db.execute(q):
            idx.add(sku, ch, ph)
        return idx


# ---------------------------------------------------------------- batches (importer runs and resyncs)
async def start_batch(db: AsyncSession, org_id: str, source: str, params: dict[str, Any]) -> ImportBatch:
    b = ImportBatch(org_id=org_id, source=source, params=params, counts={}, error_log=[])
    db.add(b)
    await db.commit()
    return b


def batch_error(b: ImportBatch, key: str, kind: str, error: str) -> None:
    """Append to the batch's error log (capped). Caller commits."""
    log_ = list(b.error_log or [])
    if len(log_) < MAX_ERROR_LOG:
        log_.append({"key": key, "kind": kind, "error": error[:300]})
        b.error_log = log_
    else:
        c = dict(b.counts or {})
        c["errors_dropped"] = c.get("errors_dropped", 0) + 1
        b.counts = c


def batch_count(b: ImportBatch, **inc: int) -> None:
    c = dict(b.counts or {})
    for k, v in inc.items():
        c[k] = c.get(k, 0) + v
    b.counts = c


def batch_view(b: ImportBatch) -> dict[str, Any]:
    from app.domain.common import iso
    return {"id": b.id, "source": b.source, "started_at": iso(b.started_at), "finished_at": iso(b.finished_at),
            "running": b.finished_at is None, "counts": b.counts or {}, "params": b.params or {}, "errors": b.error_log or []}


# ---------------------------------------------------------------- resync
SYSTEM_ACTOR_NAME = "System"


async def resync_org(sm: async_sessionmaker[AsyncSession], storage: Storage, inference: InferenceClient, s: Settings, org_id: str,
                     batch_id: str, actor: Actor, *, skus: list[str] | None = None,
                     progress: Callable[[int, int], Awaitable[None] | None] | None = None) -> dict[str, Any]:
    """Re-send the stored photos of every ACTIVE product to the worker (e.g. after Qdrant was wiped or DINO_MODEL /
    QDRANT_COLLECTION changed). Product by product, each in its own transaction; a failure is logged and the run continues.
    Inactive products are removed from the worker (best effort) so they cannot match."""
    signature = await index_signature(inference)
    async with sm() as db:
        b = await db.get(ImportBatch, batch_id)
        previous = {w for (w,) in await db.execute(select(Product.indexed_with).where(
            Product.org_id == org_id, Product.indexed_with.is_not(None)).distinct())}
        warnings: list[str] = []
        if signature is None:
            warnings.append("The matching service did not report its DINO_MODEL / QDRANT_COLLECTION; mismatches cannot be checked.")
        elif previous - {signature}:
            warnings.append(f"Products were indexed with {', '.join(sorted(previous - {signature}))} but the service now uses "
                            f"{signature}. Vectors from another model/collection are not comparable: this resync re-embeds everything.")
        c = dict(b.counts or {})
        c.update(signature=signature, warnings=warnings)
        b.counts = c
        await db.commit()

    async with sm() as db:
        q = select(Product.id, Product.sku, Product.active).where(Product.org_id == org_id).order_by(Product.id)
        if skus:
            q = q.where(Product.sku_key.in_([k.lower() for k in skus]))
        rows = list((await db.execute(q)).all())
    total, done = len(rows), 0
    for pid, sku, active in rows:
        async with sm() as db:
            b = await db.get(ImportBatch, batch_id)
            p = await db.get(Product, pid)
            try:
                if not active:
                    try:
                        await inference.remove_product(sku)
                    except InferenceError:
                        pass
                    batch_count(b, inactive_removed=1)
                else:
                    n = await reindex_product(db, storage, inference, s, p, signature)
                    if n == 0:
                        batch_count(b, skipped=1)  # no photos: nothing to send
                    else:
                        batch_count(b, resynced=1, photos=n)
            except Exception as e:  # noqa: BLE001 - one bad product must not stop the run
                await db.rollback()
                b = await db.get(ImportBatch, batch_id)
                p = await db.get(Product, pid)
                p.indexed_at = None  # not confirmed by the worker: shows as "failed" until retried
                batch_count(b, failed=1)
                batch_error(b, sku, "resync", str(e))
            await db.commit()
        done += 1
        if progress:
            r = progress(done, total)
            if r is not None:
                await r
    async with sm() as db:
        b = await db.get(ImportBatch, batch_id)
        b.finished_at = utcnow()
        audit(db, actor, "CATALOG_RESYNCED", f"batch {b.id}", after={"products": total, **{k: v for k, v in (b.counts or {}).items() if k in ("resynced", "failed", "skipped", "photos")}}, org_id=org_id)
        await db.commit()
        return dict(b.counts or {})


async def index_status(db: AsyncSession, inference: InferenceClient, org_id: str) -> dict[str, Any]:
    """Catalog health: how many products are searchable, and whether the worker's model/collection still matches."""
    refs = select(ReferenceImage.product_id, func.count().label("n")).group_by(ReferenceImage.product_id).subquery()
    rows = (await db.execute(select(Product.indexed_at, Product.indexed_with, func.coalesce(refs.c.n, 0)).outerjoin(
        refs, refs.c.product_id == Product.id).where(Product.org_id == org_id, Product.active.is_(True)))).all()
    out = {"ready": 0, "pending": 0, "failed": 0}
    for ia, _, n in rows:
        out[product_status(ia, n)] += 1
    try:
        info = await inference.info()
        reachable = True
    except Exception:  # noqa: BLE001
        info, reachable = {}, False
    sig = f"{info['model']}@{info['collection']}" if info.get("model") and info.get("collection") else None
    stale = sum(1 for ia, w, _ in rows if ia is not None and w is not None and sig is not None and w != sig)
    warnings = []
    if not reachable:
        warnings.append("The matching service is not reachable.")
    if stale:
        warnings.append(f"{stale} product(s) were indexed with a different DINO_MODEL / QDRANT_COLLECTION than the service uses now. Run a resync.")
    if reachable and info.get("references") == 0 and out["ready"]:
        warnings.append("The matching service holds no vectors but products are marked ready (Qdrant was wiped?). Run a resync.")
    return {"products": out, "reachable": reachable, "model": info.get("model"), "collection": info.get("collection"),
            "worker_references": info.get("references"), "stale": stale, "warnings": warnings}
