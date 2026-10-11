"""Catalog index lifecycle against a FakeWorker (colour embedder + in-memory store): status flow, reindex, resync, end-to-end
"register by photos -> detectable at once", review crops, listing filters and credits. No network, no GPU."""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select

from app import db as dbmod
from app.infra.inference import RawDetection, RawMatch
from app.models import AuditLog, ImportBatch, Organization, Product, ReferenceImage, Scan
from app.routers.analyze import reset_analyze_limiters
from tests.fakes import FakeWorker, photo, shelf_photo
from tests.helpers import first_shelf, get_scan, login

RED, GREEN, BLUE, YELLOW = (200, 30, 30), (30, 190, 60), (40, 60, 210), (220, 200, 30)


@pytest.fixture(autouse=True)
def _fresh_limiters():
    reset_analyze_limiters()


class GatedWorker(FakeWorker):
    """A worker whose add_references blocks until released, so a resync can be observed while it runs."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def add_references(self, sku, name, items):
        await self.gate.wait()
        return await super().add_references(sku, name, items)


def install(c, worker: FakeWorker) -> FakeWorker:
    c.app.state.inference = worker
    c.app.state.runner.inference = worker
    return worker


@pytest_asyncio.fixture
async def env(seeded):
    c = seeded
    w = install(c, FakeWorker())
    return c, w, await login(c, "manager"), await login(c, "owner"), await login(c, "staff")


def files(color, n, start=0, name="p"):
    return [("files", (f"{name}{i}.jpg", photo(color, start + i), "image/jpeg")) for i in range(n)]


async def create(c, h, sku, name=None):
    r = await c.post("/catalog/products", headers=h, json={"sku": sku, "name": name or f"Product {sku}"})
    assert r.status_code == 201, r.text
    return r.json()


async def upload(c, h, sku, color, n=1, start=0):
    r = await c.post(f"/catalog/products/{sku}/references", headers=h, files=files(color, n, start))
    assert r.status_code == 201, r.text
    return r.json()


async def product(c, h, sku):
    r = await c.get(f"/catalog/products/{sku}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def q(stmt):
    async with dbmod.sessionmaker()() as db:
        return (await db.execute(stmt)).all()


async def finish_background(c):
    await asyncio.gather(*list(c.app.state.bg_tasks))


async def resync(c, owner, worker: FakeWorker | None = None) -> dict:
    r = await c.post("/catalog/resync", headers=owner)
    assert r.status_code == 202, r.text
    await finish_background(c)
    b = await c.get(f"/catalog/batches/{r.json()['batch_id']}", headers=owner)
    assert b.status_code == 200
    return b.json()


async def upload_scan_bytes(c, h, data: bytes) -> str:
    shelf = await first_shelf(c, h)
    r = await c.post("/scans", headers=h, data={"shelf_id": shelf}, files=[("files", ("shelf.jpg", data, "image/jpeg"))])
    assert r.status_code == 201, r.text
    await c.app.state.runner.wait_idle()
    return r.json()["scan_id"]


async def analyze(c, colors):
    r = await c.post("/analyze", files={"image": ("s.jpg", shelf_photo(colors), "image/jpeg")})
    assert r.status_code == 200, r.text
    return {g["label"]: g for g in r.json()}


# ---------------------------------------------------------------- status flow
async def test_status_flow_pending_upload_ready(env):
    c, w, mgr, owner, staff = env
    p = await create(c, mgr, "NEW-1")
    assert (p["status"], p["reference_count"], p["indexed_at"]) == ("pending", 0, None)
    p = await upload(c, mgr, "NEW-1", RED, 3)
    assert (p["status"], p["reference_count"]) == ("ready", 3) and p["indexed_at"]
    assert {r["source"] for r in p["references"]} == {"upload"}
    assert w.skus() == {"NEW-1"} and len(w.store) == 3
    ((ia, iw),) = await q(select(Product.indexed_at, Product.indexed_with).where(Product.sku == "NEW-1"))
    assert ia is not None and iw == "fake-dino@fake_refs"
    audit = await q(select(AuditLog.after).where(AuditLog.entity == "NEW-1", AuditLog.action == "REFERENCES_ADDED"))
    assert len(audit) == 1 and '"count": 3' in audit[0][0] and '"source": "upload"' in audit[0][0]
    assert (await product(c, staff, "NEW-1"))["status"] == "ready"  # every role sees the same status


async def test_reference_delete_keeps_catalog_and_worker_vectors_consistent_and_retries(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "DELETE-REF")
    ready = await upload(c, mgr, "DELETE-REF", RED, n=2)
    deleted, kept = [ref["id"] for ref in ready["references"]]

    w.down = True
    failed = await c.delete(f"/catalog/products/DELETE-REF/references/{deleted}", headers=mgr)
    assert failed.status_code == 503
    assert (await product(c, mgr, "DELETE-REF"))["reference_count"] == 2
    assert len(w.store) == 2

    w.down = False
    removed = await c.delete(f"/catalog/products/DELETE-REF/references/{deleted}", headers=mgr)
    assert removed.status_code == 204
    current = await product(c, mgr, "DELETE-REF")
    assert current["status"] == "ready" and current["reference_count"] == 1
    assert ("DELETE-REF", deleted) not in w.store and ("DELETE-REF", kept) in w.store

    last = await c.delete(f"/catalog/products/DELETE-REF/references/{kept}", headers=mgr)
    assert last.status_code == 204
    pending = await product(c, mgr, "DELETE-REF")
    assert pending["status"] == "pending" and pending["reference_count"] == 0 and pending["indexed_at"] is None
    assert not [key for key in w.store if key[0] == "DELETE-REF"]


async def test_worker_failure_is_a_502_and_keeps_photos_retryable(env):
    c, w, mgr, *_ = env
    await create(c, mgr, "NEW-2")
    w.fail = 1
    r = await c.post("/catalog/products/NEW-2/references", headers=mgr, files=files(RED, 2))
    assert r.status_code == 502 and "retry" in r.text.lower()
    p = await product(c, mgr, "NEW-2")
    assert (p["status"], p["reference_count"], p["indexed_at"]) == ("failed", 2, None)
    assert len(await q(select(ReferenceImage))) == 2 and not w.store
    assert len(await q(select(AuditLog).where(AuditLog.entity == "NEW-2", AuditLog.action == "REFERENCES_ADDED"))) == 1
    stored = [f for f in c.app.state.storage.root.rglob("*") if f.is_file() and "catalog" in f.parts]
    assert len(stored) == 4  # originals and thumbnails remain available for retry

    w.down = True  # a worker that is simply unreachable behaves the same
    assert (await c.post("/catalog/products/NEW-2/references", headers=mgr, files=files(RED, 1))).status_code == 502
    w.down = False
    assert (await c.post("/catalog/products/NEW-2/reindex", headers=mgr)).json()["status"] == "ready"


async def test_upload_validation(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "NEW-3")
    assert (await c.post("/catalog/products/NEW-3/references", headers=mgr, files=files(RED, 11))).status_code == 422  # > 10 per request
    bad = [("files", ("x.jpg", b"not an image", "image/jpeg"))]
    assert (await c.post("/catalog/products/NEW-3/references", headers=mgr, files=bad)).status_code == 422
    assert (await c.post("/catalog/products/NOPE/references", headers=mgr, files=files(RED, 1))).status_code == 404
    await c.delete("/catalog/products/NEW-3", headers=mgr)
    assert (await c.post("/catalog/products/NEW-3/references", headers=mgr, files=files(RED, 1))).status_code == 409
    assert not w.store and not await q(select(ReferenceImage))


# ---------------------------------------------------------------- reindex ("Failed" -> retry)
async def make_failed(c, w, mgr, owner, sku="FAIL-1", n=2):
    await create(c, mgr, sku)
    await upload(c, mgr, sku, RED, n)
    w.fail = 1
    batch = await resync(c, owner)  # the worker does not acknowledge: the product is left "failed", photos are kept
    assert batch["counts"]["failed"] == 1
    return batch


async def test_reindex_heals_a_failed_product(env):
    c, w, mgr, owner, staff = env
    batch = await make_failed(c, w, mgr, owner)
    assert any(e["key"] == "FAIL-1" and e["kind"] == "resync" for e in batch["errors"])
    p = await product(c, mgr, "FAIL-1")
    assert (p["status"], p["reference_count"], p["indexed_at"]) == ("failed", 2, None)
    assert [x["sku"] for x in (await c.get("/catalog/products?status=failed", headers=mgr)).json()] == ["FAIL-1"]

    w.down = True
    assert (await c.post("/catalog/products/FAIL-1/reindex", headers=mgr)).status_code == 502
    assert (await product(c, mgr, "FAIL-1"))["status"] == "failed"  # a failed retry changes nothing
    w.down = False
    r = await c.post("/catalog/products/FAIL-1/reindex", headers=mgr)
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["indexed_at"]
    assert len({k for k in w.store if k[0] == "FAIL-1"}) == 2
    assert (await c.get("/catalog/products?status=failed", headers=mgr)).json() == []


async def test_reindex_edge_cases(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "EMPTY-1")
    assert (await c.post("/catalog/products/EMPTY-1/reindex", headers=mgr)).status_code == 422  # nothing to send
    assert (await c.post("/catalog/products/NOPE/reindex", headers=mgr)).status_code == 404
    assert (await c.post("/catalog/products/EMPTY-1/reindex", headers=staff)).status_code == 403
    await c.delete("/catalog/products/EMPTY-1", headers=mgr)
    assert (await c.post("/catalog/products/EMPTY-1/reindex", headers=mgr)).status_code == 409


async def test_upload_after_failed_resends_the_earlier_photos_too(env):
    c, w, mgr, owner, staff = env
    await make_failed(c, w, mgr, owner, "FAIL-2", n=2)
    w.store.clear()  # model "stored but never confirmed" (an import whose worker call failed): the worker holds nothing
    p = await upload(c, mgr, "FAIL-2", RED, 1, start=50)
    assert p["status"] == "ready" and p["reference_count"] == 3
    assert len(w.store) == 3  # earlier photos re-sent together with the new one: nothing stays unsearchable


# ---------------------------------------------------------------- resync
async def test_resync_is_owner_only(env):
    c, w, mgr, owner, staff = env
    assert (await c.post("/catalog/resync", headers=mgr)).status_code == 403
    assert (await c.post("/catalog/resync", headers=staff)).status_code == 403
    assert (await c.post("/catalog/resync", headers=owner)).status_code == 202
    await finish_background(c)


async def test_resync_restores_a_wiped_worker_in_batches_of_at_most_10(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "BIG-1")
    await upload(c, mgr, "BIG-1", RED, 10)
    await upload(c, mgr, "BIG-1", RED, 5, start=100)  # 15 photos
    await create(c, mgr, "SMALL-1")
    await upload(c, mgr, "SMALL-1", GREEN, 2)
    await create(c, mgr, "GONE-1")
    await upload(c, mgr, "GONE-1", BLUE, 2)
    await c.delete("/catalog/products/GONE-1", headers=mgr)  # deactivated: must not come back
    await create(c, mgr, "NOPHOTO-1")
    w.store.clear()
    w.calls.clear()
    assert w.skus() == set()

    batch = await resync(c, owner)
    assert w.skus() == {"BIG-1", "SMALL-1"} and "GONE-1" not in w.skus()
    assert len({k for k in w.store if k[0] == "BIG-1"}) == 15
    big_calls = [n for sku, n in w.calls if sku == "BIG-1"]
    assert big_calls == [10, 5] and max(n for _, n in w.calls) <= 10
    assert batch["source"] == "resync" and batch["finished_at"] and batch["running"] is False
    cn = batch["counts"]
    assert cn["resynced"] == 2 and cn["photos"] == 17 and cn["inactive_removed"] == 1 and not cn.get("failed")
    assert cn["skipped"] >= 1  # products without photos are skipped, not failed
    assert cn["signature"] == "fake-dino@fake_refs" and cn["warnings"] == []
    done = await q(select(AuditLog.action).where(AuditLog.action == "CATALOG_RESYNCED"))
    assert len(done) == 1
    assert [b["id"] for b in (await c.get("/catalog/batches?source=resync", headers=owner)).json()] == [batch["id"]]


async def test_resync_removes_a_deactivated_product_the_worker_still_holds(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "OLD-1")
    await upload(c, mgr, "OLD-1", RED, 1)
    async with dbmod.sessionmaker()() as db:  # deactivated behind the API's back (e.g. the worker was down at that moment)
        p = await db.scalar(select(Product).where(Product.sku == "OLD-1"))
        p.active = False
        await db.commit()
    assert "OLD-1" in w.skus()
    await resync(c, owner)
    assert "OLD-1" not in w.skus()


async def test_resync_keeps_going_when_one_product_fails(env):
    c, w, mgr, owner, staff = env
    for sku, col in (("A-1", RED), ("B-1", GREEN), ("C-1", BLUE)):
        await create(c, mgr, sku)
        await upload(c, mgr, sku, col, 1)
    w.store.clear()
    w.fail = 1  # the first product in the run (ordered by id) fails
    batch = await resync(c, owner)
    assert batch["counts"]["failed"] == 1 and batch["counts"]["resynced"] == 2 and len(batch["errors"]) == 1
    statuses = {p["sku"]: p["status"] for p in (await c.get("/catalog/products?slim=true", headers=mgr)).json() if p["sku"] in ("A-1", "B-1", "C-1")}
    assert sorted(statuses.values()) == ["failed", "ready", "ready"]
    failed = batch["errors"][0]["key"]
    assert (await c.post(f"/catalog/products/{failed}/reindex", headers=mgr)).json()["status"] == "ready"


async def test_model_or_collection_mismatch_warns_in_index_status_and_in_the_batch(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "M-1")
    await upload(c, mgr, "M-1", RED, 2)
    st = (await c.get("/catalog/index-status", headers=mgr)).json()
    assert st["warnings"] == [] and st["stale"] == 0 and st["products"]["ready"] == 1 and st["model"] == "fake-dino"

    w.model, w.collection = "bigger-dino", "refs_v2"  # someone changed DINO_MODEL / QDRANT_COLLECTION
    w.store.clear()  # a new collection is empty
    st = (await c.get("/catalog/index-status", headers=mgr)).json()
    assert st["stale"] == 1 and any("different DINO_MODEL / QDRANT_COLLECTION" in x for x in st["warnings"])
    assert st["model"] == "bigger-dino" and st["collection"] == "refs_v2"

    batch = await resync(c, owner)
    assert batch["counts"]["signature"] == "bigger-dino@refs_v2"
    assert any("fake-dino@fake_refs" in x and "bigger-dino@refs_v2" in x for x in batch["counts"]["warnings"])
    assert (await q(select(Product.indexed_with).where(Product.sku == "M-1")))[0][0] == "bigger-dino@refs_v2"
    st = (await c.get("/catalog/index-status", headers=mgr)).json()
    assert st["stale"] == 0 and st["warnings"] == []  # healed


async def test_empty_worker_with_ready_products_warns(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "E-1")
    await upload(c, mgr, "E-1", RED, 1)
    w.store.clear()  # Qdrant wiped
    st = (await c.get("/catalog/index-status", headers=mgr)).json()
    assert st["products"]["ready"] == 1 and st["worker_references"] == 0
    assert any("holds no vectors" in x for x in st["warnings"])
    await resync(c, owner)
    st = (await c.get("/catalog/index-status", headers=mgr)).json()
    assert st["warnings"] == [] and st["worker_references"] == 1


async def test_index_status_when_the_worker_is_unreachable(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "U-1")
    w.down = True
    st = (await c.get("/catalog/index-status", headers=mgr)).json()
    assert st["reachable"] is False and "not reachable" in " ".join(st["warnings"])
    assert (await c.post("/catalog/resync", headers=owner)).status_code == 503  # refuses to start a resync nobody can receive
    assert not await q(select(ImportBatch).where(ImportBatch.source == "resync"))


async def test_second_resync_while_one_is_running_is_409(env):
    c, _, mgr, owner, staff = env
    w = install(c, GatedWorker())
    await create(c, mgr, "G-1")
    w.gate.set()
    await upload(c, mgr, "G-1", RED, 1)
    w.gate.clear()  # from now on the worker blocks
    r1 = await c.post("/catalog/resync", headers=owner)
    assert r1.status_code == 202 and r1.json()["running"] is True
    for _ in range(3):
        await asyncio.sleep(0)
    r2 = await c.post("/catalog/resync", headers=owner)
    assert r2.status_code == 409 and "already running" in r2.text
    running = (await c.get(f"/catalog/batches/{r1.json()['batch_id']}", headers=owner)).json()
    assert running["running"] is True and running["finished_at"] is None
    w.gate.set()
    await finish_background(c)
    done = (await c.get(f"/catalog/batches/{r1.json()['batch_id']}", headers=owner)).json()
    assert done["running"] is False and done["counts"]["resynced"] == 1
    assert (await c.post("/catalog/resync", headers=owner)).status_code == 202  # allowed again once finished
    await finish_background(c)


async def test_batches_are_listed_and_scoped(env):
    c, w, mgr, owner, staff = env
    b = await resync(c, owner)
    assert (await c.get(f"/catalog/batches/{b['id']}", headers=mgr)).status_code == 200  # managers may read
    assert (await c.get("/catalog/batches/nope", headers=mgr)).status_code == 404
    assert (await c.get("/catalog/batches/" + b["id"], headers=staff)).status_code == 403
    async with dbmod.sessionmaker()() as db:  # a batch of another organisation is invisible
        other = Organization(slug="other", name="Other")
        db.add(other)
        await db.flush()
        db.add(ImportBatch(org_id=other.id, source="resync", params={}, counts={}, error_log=[]))
        await db.commit()
        foreign = await db.scalar(select(ImportBatch.id).where(ImportBatch.org_id == other.id))
    assert (await c.get(f"/catalog/batches/{foreign}", headers=owner)).status_code == 404
    assert [x["id"] for x in (await c.get("/catalog/batches", headers=owner)).json()] == [b["id"]]


# ---------------------------------------------------------------- end to end: register by photos, detect, no restart
async def test_registered_product_is_detected_immediately_and_a_second_one_too(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "RED-1", "Red Tin")
    assert set(await analyze(c, [RED])) == {None}  # nothing learned yet: the only box is unknown
    p = await upload(c, mgr, "RED-1", RED, 4)  # 1-10 photos; no restart, no training
    assert p["status"] == "ready"

    g = await analyze(c, [RED])
    assert set(g) == {"RED-1"} and g["RED-1"]["quantity"] == 1
    assert g["RED-1"]["match_score"] > 0.95
    sid = await upload_scan_bytes(c, staff, shelf_photo([RED]))  # the scan workflow sees it as well
    (d,) = (await get_scan(c, staff, sid))["detections"]
    assert d["state"] == "SURE" and d["top_matches"][0]["sku"] == "RED-1"

    await create(c, mgr, "GREEN-1", "Green Tin")  # a second product, added afterwards
    g = await analyze(c, [RED, GREEN, RED])
    assert "GREEN-1" not in g  # pending = no photos = not searchable; /analyze has no similarity floor, so the green box lands on RED-1
    assert g["RED-1"]["quantity"] == 3
    await upload(c, mgr, "GREEN-1", GREEN, 10)  # 10 photos, the maximum per request
    g = await analyze(c, [RED, GREEN, RED])
    assert set(g) == {"RED-1", "GREEN-1"} and g["RED-1"]["quantity"] == 2 and g["GREEN-1"]["quantity"] == 1

    assert (await c.delete("/catalog/products/GREEN-1", headers=mgr)).status_code == 204  # deactivated: stops matching at once
    assert "GREEN-1" not in await analyze(c, [RED, GREEN])


async def test_scan_detection_uses_the_new_product_without_restart(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "BLUE-1")
    await upload(c, mgr, "BLUE-1", BLUE, 3)
    sid = await upload_scan_bytes(c, staff, shelf_photo([BLUE]))
    v = await get_scan(c, staff, sid)
    (d,) = v["detections"]
    assert d["state"] == "SURE" and d["top_matches"][0]["sku"] == "BLUE-1"


async def test_product_registered_after_an_earlier_scan_is_found_by_next_scan(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "OLDER-1", "Earlier product")
    await upload(c, mgr, "OLDER-1", RED, 3)
    old_scan = await upload_scan_bytes(c, staff, shelf_photo([RED]))
    assert (await get_scan(c, staff, old_scan))["detections"][0]["top_matches"][0]["sku"] == "OLDER-1"

    await create(c, mgr, "LATER-1", "Later product")
    await upload(c, mgr, "LATER-1", GREEN, 3)
    next_scan = await upload_scan_bytes(c, staff, shelf_photo([GREEN]))
    detection = (await get_scan(c, staff, next_scan))["detections"][0]
    assert detection["state"] == "SURE" and detection["top_matches"][0]["sku"] == "LATER-1"


async def test_test_recognition_returns_top_matches_without_creating_scan(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "PREVIEW-1", "Preview red")
    await upload(c, mgr, "PREVIEW-1", RED, 3)
    response = await c.post("/catalog/products/PREVIEW-1/test-recognition", headers=mgr,
                            files={"file": ("test.jpg", shelf_photo([RED]), "image/jpeg")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan_created"] is False and body["sku"] == "PREVIEW-1"
    assert body["detections"][0]["matches"][0]["sku"] == "PREVIEW-1"
    assert body["detections"][0]["target_score"] > 0.99
    assert await q(select(Scan.id)) == []


async def test_test_recognition_warns_when_top_matches_are_close(env):
    c, w, mgr, owner, staff = env
    await create(c, mgr, "CLOSE-1", "Close match target")
    await upload(c, mgr, "CLOSE-1", RED, 3)
    await create(c, mgr, "CLOSE-2", "Close match competitor")
    await upload(c, mgr, "CLOSE-2", GREEN, 3)

    async def close_results(image, *, scan_id, catalog, width, height, seed=None, dense=False):
        return [RawDetection((0, 0, width, height), 0.9, [RawMatch("CLOSE-1", 0.82), RawMatch("CLOSE-2", 0.79)])]

    w.detect = close_results
    response = await c.post("/catalog/products/CLOSE-1/test-recognition", headers=mgr,
                            files={"file": ("test.jpg", shelf_photo([RED]), "image/jpeg")})
    assert response.status_code == 200
    assert response.json()["detections"][0]["close_match"] is True


# ---------------------------------------------------------------- review crops
async def corrected_scan(c, w, mgr, staff, sku="CROP-1", color=RED):
    """A product with one catalog photo, a scan of a shelf with that colour, and the box assigned to the product by a reviewer."""
    await create(c, mgr, sku)
    await upload(c, mgr, sku, color, 1)
    sid = await upload_scan_bytes(c, staff, shelf_photo([color, GREEN]))
    dets = (await get_scan(c, staff, sid))["detections"]
    assert len(dets) == 2
    r = await c.patch(f"/scans/{sid}/detections", headers=staff, json={"ids": [dets[0]["id"]], "action": "ASSIGN", "sku": sku})
    assert r.status_code == 200, r.text
    return sid, dets[0]["id"], dets[1]["id"]


async def test_review_crop_becomes_a_reference_photo(env):
    c, w, mgr, owner, staff = env
    sid, box, other = await corrected_scan(c, w, mgr, staff)
    before = len(w.store)
    r = await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["reference_count"] == 2 and p["status"] == "ready"
    assert sorted(x["source"] for x in p["references"]) == ["review_crop", "upload"]
    assert len(w.store) == before + 1  # the worker embedded the crop
    (row,) = await q(select(AuditLog.entity, AuditLog.after).where(AuditLog.action == "REFERENCES_ADDED", AuditLog.after.like("%review_crop%")))
    assert row[0] == "CROP-1" and sid in row[1] and f'"detection_id": "{box}"' in row[1] and '"count": 1' in row[1]
    (ref,) = await q(select(ReferenceImage.content_hash, ReferenceImage.phash).where(ReferenceImage.source == "review_crop"))
    assert len(ref[0]) == 64 and len(ref[1]) == 16

    # the same crop twice adds nothing
    r = await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})
    assert r.status_code == 409 and "already a reference" in r.text
    assert (await product(c, mgr, "CROP-1"))["reference_count"] == 2 and len(w.store) == before + 1


async def test_review_crop_requires_a_box_assigned_to_that_product(env):
    c, w, mgr, owner, staff = env
    sid, box, other = await corrected_scan(c, w, mgr, staff)
    await create(c, mgr, "CROP-2")
    n = len(w.store)
    ok = {"scan_id": sid, "detection_id": other}
    r = await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json=ok)
    assert r.status_code == 409  # `other` was never corrected by a reviewer
    r = await c.post("/catalog/products/CROP-2/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})
    assert r.status_code == 409  # the box was assigned to CROP-1, not CROP-2
    assert (await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": "d99"})).status_code == 404
    assert (await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": "nope", "detection_id": box})).status_code == 404
    assert (await c.post("/catalog/products/NOPE/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})).status_code == 404
    assert (await c.post("/catalog/products/CROP-1/references/from-detection", headers=staff, json={"scan_id": sid, "detection_id": box})).status_code == 403
    await c.patch(f"/scans/{sid}/detections", headers=staff, json={"ids": [box], "action": "NOT_A_PRODUCT"})
    assert (await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})).status_code == 404  # deleted box
    assert len(w.store) == n and (await product(c, mgr, "CROP-1"))["reference_count"] == 1


async def test_review_crop_respects_the_20_photo_cap(env):
    c, w, mgr, owner, staff = env
    sid, box, _ = await corrected_scan(c, w, mgr, staff)  # 1 photo already
    await upload(c, mgr, "CROP-1", RED, 10, start=200)
    await upload(c, mgr, "CROP-1", RED, 9, start=300)  # now exactly 20
    assert (await product(c, mgr, "CROP-1"))["reference_count"] == 20
    r = await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})
    assert r.status_code == 422 and "at most 20" in r.text
    assert (await c.post("/catalog/products/CROP-1/references", headers=mgr, files=files(RED, 1, 900))).status_code == 422  # same cap for uploads


async def test_review_crop_worker_failure_changes_nothing(env):
    c, w, mgr, owner, staff = env
    sid, box, _ = await corrected_scan(c, w, mgr, staff)
    w.fail = 1
    r = await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})
    assert r.status_code == 502
    assert (await product(c, mgr, "CROP-1"))["reference_count"] == 1
    assert (await c.post("/catalog/products/CROP-1/references/from-detection", headers=mgr, json={"scan_id": sid, "detection_id": box})).status_code == 201


# ---------------------------------------------------------------- listing, filters, credits
async def seed_many(n=25):
    async with dbmod.sessionmaker()() as db:
        org = await db.scalar(select(Organization))
        for i in range(n):
            db.add(Product(org_id=org.id, sku=f"BULK-{i:03d}", sku_key=f"bulk-{i:03d}", name=f"Bulk Item {i:03d}",
                           brand="Zeta Foods" if i % 5 == 0 else None, barcode=f"40063813{i:05d}" if i % 2 else None))
        db.add(Product(org_id=org.id, sku="PCT-1", sku_key="pct-1", name="100% Juice"))
        db.add(Product(org_id=org.id, sku="USC_1", sku_key="usc_1", name="Under_score"))
        db.add(Product(org_id=org.id, sku="OFF-1", sku_key="off-1", name="Imported Biscuits", brand="Acme", source="openfoodfacts",
                       image_license="CC BY-SA", image_attribution="Photo by alice via Open Food Facts (CC BY-SA 3.0)",
                       source_url="https://world.openfoodfacts.org/product/OFF-1"))
        db.add(Product(org_id=org.id, sku="OFF-2", sku_key="off-2", name="Imported Crisps", source="openfoodfacts",
                       image_license="CC BY-SA", image_attribution="Photo by bob via Open Food Facts (CC BY-SA 3.0)",
                       source_url="https://world.openfoodfacts.org/product/OFF-2"))
        await db.commit()


async def test_product_list_paging_search_slim_and_status(env):
    c, w, mgr, owner, staff = env
    await seed_many()
    await create(c, mgr, "READY-1")
    await upload(c, mgr, "READY-1", RED, 2)
    everything = (await c.get("/catalog/products", headers=staff)).json()  # no limit = everything, as before
    assert len(everything) == 10 + 25 + 5
    assert [p["sku"] for p in everything] == sorted(p["sku"] for p in everything)

    page1 = (await c.get("/catalog/products?limit=10", headers=staff)).json()
    page2 = (await c.get("/catalog/products?limit=10&offset=10", headers=staff)).json()
    assert [p["sku"] for p in page1 + page2] == [p["sku"] for p in everything[:20]]
    assert (await c.get("/catalog/products?limit=10&offset=1000", headers=staff)).json() == []
    assert (await c.get("/catalog/products?limit=1001", headers=staff)).status_code == 422
    assert (await c.get("/catalog/products?limit=0", headers=staff)).status_code == 422
    assert (await c.get("/catalog/products?offset=-1", headers=staff)).status_code == 422

    skus = lambda r: [p["sku"] for p in r.json()]  # noqa: E731
    assert skus(await c.get("/catalog/products?q=bulk-01", headers=staff)) == [f"BULK-01{i}" for i in range(10)]  # SKU, case-insensitive
    assert skus(await c.get("/catalog/products?q=olive", headers=staff)) == ["SKU-002"]  # name
    assert skus(await c.get("/catalog/products?q=zeta", headers=staff)) == [f"BULK-{i:03d}" for i in (0, 5, 10, 15, 20)]  # brand
    assert skus(await c.get("/catalog/products?q=4006381300003", headers=staff)) == ["BULK-003"]  # barcode
    assert skus(await c.get("/catalog/products?q=100%25", headers=staff)) == ["PCT-1"]  # '%' is a literal, not a wildcard
    assert skus(await c.get("/catalog/products?q=Under_", headers=staff)) == ["USC_1"]
    assert skus(await c.get("/catalog/products?q=UnderXscore", headers=staff)) == []  # '_' is a literal too
    assert skus(await c.get("/catalog/products?q=%20%20", headers=staff)) == skus(await c.get("/catalog/products", headers=staff))  # blank q = no filter
    assert skus(await c.get("/catalog/products?q=bulk&limit=5&offset=5", headers=staff)) == [f"BULK-{i:03d}" for i in range(5, 10)]

    slim = (await c.get("/catalog/products?slim=true&q=READY-1", headers=staff)).json()
    assert slim[0]["references"] == [] and slim[0]["reference_count"] == 2 and slim[0]["status"] == "ready"
    full = (await c.get("/catalog/products?q=READY-1", headers=staff)).json()
    assert len(full[0]["references"]) == 2

    assert skus(await c.get("/catalog/products?status=ready", headers=staff)) == ["READY-1"]
    assert skus(await c.get("/catalog/products?status=failed", headers=staff)) == []
    pending = await c.get("/catalog/products?status=pending", headers=staff)
    assert "READY-1" not in skus(pending) and len(pending.json()) == len(everything) - 1
    assert (await c.get("/catalog/products?status=bogus", headers=staff)).status_code == 422

    await c.delete("/catalog/products/SKU-010", headers=mgr)  # inactive products: only roles that can write the catalog may ask for them
    assert "SKU-010" not in skus(await c.get("/catalog/products?include_inactive=true", headers=staff))
    assert "SKU-010" in skus(await c.get("/catalog/products?include_inactive=true", headers=mgr))


async def test_product_list_chunks_photo_lookups(env):
    """More than 500 products per page must not build a 500+ item IN list per statement (PostgreSQL bind-parameter limit)."""
    c, w, mgr, owner, staff = env
    await seed_many(620)
    r = await c.get("/catalog/products?slim=true", headers=staff)
    assert r.status_code == 200 and len(r.json()) == 10 + 620 + 4
    r = await c.get("/catalog/products", headers=staff)
    assert r.status_code == 200 and len(r.json()) == 10 + 620 + 4


async def test_credits_lists_only_licensed_products_and_is_readable_by_everyone(env):
    c, w, mgr, owner, staff = env
    await seed_many(3)
    aud = await login(c, "auditor")
    for h in (staff, aud, mgr, owner):
        r = await c.get("/catalog/credits", headers=h)
        assert r.status_code == 200
    d = (await c.get("/catalog/credits", headers=staff)).json()
    assert d["total"] == 2 and [i["sku"] for i in d["items"]] == ["OFF-1", "OFF-2"]  # manual products carry no licence
    first = d["items"][0]
    assert first["license"] == "CC BY-SA" and "alice" in first["attribution"] and first["source_url"].endswith("OFF-1")
    assert first["name"] == "Imported Biscuits" and first["brand"] == "Acme"
    d = (await c.get("/catalog/credits?q=crisp", headers=staff)).json()
    assert d["total"] == 1 and d["items"][0]["sku"] == "OFF-2"
    d = (await c.get("/catalog/credits?limit=1&offset=1", headers=staff)).json()
    assert d["total"] == 2 and [i["sku"] for i in d["items"]] == ["OFF-2"]  # total ignores paging
    assert (await c.get("/catalog/credits?limit=501", headers=staff)).status_code == 422
    assert (await c.get("/catalog/credits")).status_code == 401
