"""ai-worker HTTP service. Implements the contract documented at the top of backend/app/infra/inference.py:
  POST /v1/detect (file, scan_id, top_k) | POST /v1/references (sku, name, ref_ids, files) | DELETE /v1/products/{sku} | GET /health"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.config import Settings
from app.pipeline import Pipeline, load_image

log = logging.getLogger("ai.main")
logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}')


def build_pipeline(s: Settings) -> Pipeline:
    from app.calibration import load_calibrator
    from app.detector import build_detector
    from app.embedder import DinoEmbedder, resolve_device
    from app.store import QdrantStore

    detector_weights = s.yolo_weights if s.detector_backend == "yolo" else s.h5_model_path
    model_path = Path(detector_weights)
    if not model_path.is_file() or model_path.stat().st_size == 0:
        detector_name = "YOLO checkpoint" if s.detector_backend == "yolo" else "H5 detector model"
        raise RuntimeError(f"Configured {detector_name} is missing or empty: {model_path}")
    device = resolve_device(s.device)
    detector = build_detector(s, device)
    emb = DinoEmbedder(s, device)
    return Pipeline(s, detector, emb,
                    QdrantStore(s.qdrant_url, s.qdrant_collection, emb.dim, s.qdrant_api_key),
                    load_calibrator(s.calibration_path))


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    s = settings or Settings()
    if s.env == "production" and not s.token:
        raise RuntimeError("AI_WORKER_TOKEN must be set when ENV=production")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pipeline = pipeline or await run_in_threadpool(build_pipeline, s)
        app.state.sem = asyncio.Semaphore(s.max_concurrency)
        yield

    app = FastAPI(title="CALIBREX ai-worker", version="1.0", lifespan=lifespan)

    async def auth(authorization: str | None = Header(default=None)):
        if s.token and not hmac.compare_digest(authorization or "", f"Bearer {s.token}"):
            raise HTTPException(401, "invalid or missing bearer token")

    async def read_limited(f: UploadFile) -> bytes:
        data = await f.read(s.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > s.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"file larger than {s.max_upload_mb} MB")
        return data

    @app.get("/health")
    async def health(request: Request):
        p: Pipeline = request.app.state.pipeline
        ok = await run_in_threadpool(p.store.ping)
        if not ok:
            raise HTTPException(503, "vector store unreachable")
        detector_weights = s.yolo_weights if s.detector_backend == "yolo" else s.h5_model_path
        return {"status": "ok", "references": await run_in_threadpool(p.store.count), "dim": p.embedder.dim,
                "calibrated": p.calibrator is not None, "calibration_samples": p.calibrator.n if p.calibrator else 0,
                "model": s.dino_model, "collection": s.qdrant_collection,
                "detector": {"backend": s.detector_backend, "weights": str(Path(detector_weights).resolve())}}

    @app.post("/v1/detect", dependencies=[Depends(auth)])
    async def detect(request: Request, file: UploadFile = File(...), scan_id: str = Form(""), top_k: int = Form(5)):
        p: Pipeline = request.app.state.pipeline
        data = await read_limited(file)
        try:
            img = await run_in_threadpool(load_image, data)
        except Exception:
            raise HTTPException(422, "not a readable image")
        t0 = time.perf_counter()
        async with request.app.state.sem:
            dets, tm = await run_in_threadpool(p.detect, img, max(1, min(top_k, 10)))
        total = (time.perf_counter() - t0) * 1000
        log.info("scan=%s %dx%d dets=%d total_ms=%.0f detect=%.0f embed=%.0f search=%.0f", scan_id, img.width, img.height,
                 len(dets), total, tm.detect_ms, tm.embed_ms, tm.search_ms)
        return {"image": {"width": img.width, "height": img.height}, "detections": dets,
                "timings_ms": {"total": round(total), "detect": round(tm.detect_ms), "embed": round(tm.embed_ms), "search": round(tm.search_ms)}}

    @app.post("/v1/references", dependencies=[Depends(auth)])
    async def references(request: Request, sku: str = Form(...), name: str = Form(""), ref_ids: str = Form("[]"),
                         files: list[UploadFile] = File(...)):
        p: Pipeline = request.app.state.pipeline
        if not 1 <= len(files) <= s.max_refs_per_call:
            raise HTTPException(422, f"send 1 to {s.max_refs_per_call} reference photos per call")
        try:
            ids = [str(i) for i in json.loads(ref_ids)]
        except Exception:
            raise HTTPException(422, "ref_ids must be a JSON list")
        if len(ids) != len(files):
            ids = [f"{sku}-{i}" for i in range(len(files))]
        items = []
        for rid, f in zip(ids, files):
            try:
                items.append((rid, await run_in_threadpool(load_image, await read_limited(f))))
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(422, f"reference {rid} is not a readable image")
        async with request.app.state.sem:
            added = await run_in_threadpool(p.add_references, sku, name, items)
        return {"added": added}

    @app.delete("/v1/products/{sku}", status_code=204, dependencies=[Depends(auth)])
    async def remove(sku: str, request: Request):
        await run_in_threadpool(request.app.state.pipeline.remove_product, sku)

    @app.delete("/v1/products/{sku}/references/{ref_id}", status_code=204, dependencies=[Depends(auth)])
    async def remove_reference(sku: str, ref_id: str, request: Request):
        await run_in_threadpool(request.app.state.pipeline.remove_reference, sku, ref_id)

    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()
