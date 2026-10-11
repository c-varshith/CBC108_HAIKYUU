import io
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.store import InMemoryStore  # noqa: E402
from tests.fakes import DIM, FakeDetector, FakeEmbedder, shelf, swatch  # noqa: E402


def jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG"); return b.getvalue()


def client(token="t0k"):
    img, boxes = shelf([(200, 30, 30), (30, 190, 60)])
    s = Settings(token=token)
    p = Pipeline(s, FakeDetector(boxes), FakeEmbedder(), InMemoryStore(DIM))
    return TestClient(create_app(s, p)), img


def test_auth_required_and_roundtrip():
    with client()[0] as c0:
        assert c0.get("/health").status_code == 200  # health is open for orchestrators
    c, img = client()
    with c:
        h = {"Authorization": "Bearer t0k"}
        assert c.post("/v1/detect", files={"file": ("a.jpg", jpeg(img), "image/jpeg")}).status_code == 401
        r = c.post("/v1/references", headers=h, data={"sku": "R", "name": "Red", "ref_ids": json.dumps(["r1"])},
                   files=[("files", ("r1.jpg", jpeg(swatch((200, 30, 30))), "image/jpeg"))])
        assert r.status_code == 200 and r.json()["added"] >= 1
        d = c.post("/v1/detect", headers=h, data={"scan_id": "s1", "top_k": "3"}, files={"file": ("a.jpg", jpeg(img), "image/jpeg")})
        body = d.json()
        assert d.status_code == 200 and len(body["detections"]) == 2 and body["image"]["width"] == img.width
        assert c.delete("/v1/products/R", headers=h).status_code == 204
        assert c.post("/v1/detect", headers=h, files={"file": ("a.txt", b"nope", "text/plain")}).status_code == 422


def test_health_reports_model_and_collection_so_the_backend_can_detect_a_mismatch():
    c, _ = client()
    with c:
        h = c.get("/health").json()
        s = Settings()
        assert h["model"] == s.dino_model and h["collection"] == s.qdrant_collection
        weights = s.yolo_weights if s.detector_backend == "yolo" else s.h5_model_path
        assert h["detector"] == {"backend": s.detector_backend, "weights": str(Path(weights).resolve())}
        assert h["status"] == "ok" and h["dim"] == DIM and h["references"] == 0
    s2 = Settings(token="t", dino_model="facebook/dinov2-small", qdrant_collection="refs_small")
    img, boxes = shelf([(200, 30, 30)])
    with TestClient(create_app(s2, Pipeline(s2, FakeDetector(boxes), FakeEmbedder(), InMemoryStore(DIM)))) as c2:
        h = c2.get("/health").json()
        assert (h["model"], h["collection"]) == ("facebook/dinov2-small", "refs_small")


def test_references_enforces_the_per_call_limit_and_accepts_exactly_the_limit():
    c, _ = client()
    with c:
        hdr = {"Authorization": "Bearer t0k"}
        limit = Settings().max_refs_per_call

        def send(n):
            files = [("files", (f"r{i}.jpg", jpeg(swatch((200, 30, 30), size=64 + i)), "image/jpeg")) for i in range(n)]
            return c.post("/v1/references", headers=hdr, data={"sku": "R", "name": "Red", "ref_ids": json.dumps([f"r{i}" for i in range(n)])}, files=files)

        r = send(limit + 1)  # 13 photos with the default limit of 12
        assert r.status_code == 422 and str(limit) in r.json()["detail"]
        assert c.get("/health").json()["references"] == 0  # a rejected call stores nothing
        assert send(0).status_code == 422  # FastAPI: files are required
        ok = send(limit)
        assert ok.status_code == 200 and ok.json()["added"] >= limit


def test_reference_delete_is_idempotent_and_preserves_other_references():
    c, _ = client()
    with c:
        hdr = {"Authorization": "Bearer t0k"}
        photos = [
            ("files", ("r1.jpg", jpeg(swatch((200, 30, 30))), "image/jpeg")),
            ("files", ("r2.jpg", jpeg(swatch((30, 190, 60))), "image/jpeg")),
        ]
        added = c.post("/v1/references", headers=hdr, data={"sku": "R", "name": "Red",
                       "ref_ids": json.dumps(["r1", "r2"])}, files=photos)
        assert added.status_code == 200
        before = c.get("/health").json()["references"]
        first = c.delete("/v1/products/R/references/r1", headers=hdr)
        after = c.get("/health").json()["references"]
        second = c.delete("/v1/products/R/references/r1", headers=hdr)
        assert first.status_code == second.status_code == 204
        assert 0 < after < before
        assert c.get("/health").json()["references"] == after
