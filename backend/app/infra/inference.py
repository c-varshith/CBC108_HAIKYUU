"""Boundary to the Phase 4 AI service.

Wire contract (what the ai-worker must implement; the backend owns classification and counting):

  POST {AI_WORKER_URL}/v1/detect      multipart: file=<jpeg>, scan_id=<str>, top_k=5
    -> {"image": {"width": W, "height": H},
        "detections": [{"bbox": [x1,y1,x2,y2],        # pixels of the image that was sent (normalised original)
                        "det_confidence": 0.0-1.0,
                        "matches": [{"sku": "...", "score": 0.0-1.0}, ...],   # cosine, best first
                        "flags": ["blurry"|"glare"|"tiny"],                   # optional crop-quality flags
                        "match_probability": 0.0-1.0 | null}]}                 # optional: calibrated P(top-1 correct)
  POST {AI_WORKER_URL}/v1/references   multipart: sku, name, ref_ids=<json list>, files=<jpeg>...  -> {"added": n}
  DELETE {AI_WORKER_URL}/v1/products/{sku}                                                         -> 204
  GET  {AI_WORKER_URL}/health  -> {"status": "ok", "references": n, "dim": d, "model": DINO_MODEL, "collection": QDRANT_COLLECTION, ...}

The backend (not the AI service) applies the Owner-editable threshold matrix, so changing a threshold needs no
redeploy of the AI side, and the raw scores stay available for the "what would be flagged" preview."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from app.config import Settings


@dataclass
class RawMatch:
    sku: str
    score: float


@dataclass
class RawDetection:
    bbox: tuple[float, float, float, float]
    det_confidence: float
    matches: list[RawMatch]
    flags: list[str] = field(default_factory=list)
    match_probability: float | None = None  # calibrated P(top-1 SKU correct); None until the worker has a fitted calibration


class InferenceError(Exception):
    pass


class InferenceClient(Protocol):
    async def detect(self, image: bytes, *, scan_id: str, catalog: list[tuple[str, str]], width: int, height: int,
                     seed: int | None = None, dense: bool = False) -> list[RawDetection]: ...
    async def add_references(self, sku: str, name: str, items: list[tuple[str, bytes]]) -> int: ...
    async def remove_reference(self, sku: str, ref_id: str) -> None: ...
    async def remove_product(self, sku: str) -> None: ...
    async def ping(self) -> bool: ...
    async def info(self) -> dict: ...  # {"model", "collection", "dim", "references"}; raises InferenceError when unreachable


# ---------------------------------------------------------------- stub (development, tests, demos)
def synthetic_detections(width: int, height: int, skus: list[str], seed: int, dense: bool = False) -> list[RawDetection]:
    """Deterministic fake shelf: rows of same-SKU runs with a realistic mix of confident / doubtful / unknown boxes.
    Laid out in a 4032x3024 reference frame and scaled to the real image."""
    r = random.Random(seed)
    sx, sy = width / 4032, height / 3024
    rows, step, base = (8, 350, 330) if dense else (4, 700, 380)
    wmin, wspan, hmin, hspan, gap = (62, 20, 220, 40, 8) if dense else (150, 40, 400, 60, 22)
    out: list[RawDetection] = []
    n = len(skus)
    for row in range(rows):
        x, k = 90, r.randrange(n)
        while x < 4032 - (120 if dense else 260):
            if r.random() < 0.22:
                k = r.randrange(n)
            w, h = wmin + r.randrange(wspan), hmin + r.randrange(hspan)
            y2 = base + row * step
            x1, y1 = x, y2 - h
            x += w + gap + r.randrange(6)
            if r.random() < 0.07:
                continue
            roll = r.random()
            alt = (k + 1 + r.randrange(3)) % n
            s1, s2 = 0.88 + r.random() * 0.1, 0.0
            s2 = s1 - 0.14 - r.random() * 0.2
            if roll > 0.88:
                s1 = 0.30 + r.random() * 0.28; s2 = s1 - 0.05
            elif roll > 0.70:
                s1 = 0.62 + r.random() * 0.20; s2 = s1 - 0.02 - r.random() * 0.05
            alt2 = (alt + 2) % n
            matches = [RawMatch(skus[k], round(s1, 3)), RawMatch(skus[alt], round(s2, 3)), RawMatch(skus[alt2], round(s2 - 0.1, 3))]
            out.append(RawDetection((x1 * sx, y1 * sy, (x1 + w) * sx, y2 * sy), round(0.8 + r.random() * 0.19, 3), matches))
    return out


class StubInference:
    """Development stand-in: ignores pixel content, so it cannot detect a newly registered product visually.
    Use INFERENCE_BACKEND=http against the Phase 4 service for real counting."""

    def __init__(self, s: Settings):
        self.s = s

    async def detect(self, image, *, scan_id, catalog, width, height, seed=None, dense=False):
        await asyncio.sleep(self.s.stub_latency_ms / 1000)
        skus = [sku for sku, _ in catalog]
        if not skus:
            return []
        seed = seed if seed is not None else int(hashlib.sha1(image).hexdigest()[:8], 16)
        return synthetic_detections(width, height, skus, seed, dense)

    async def add_references(self, sku, name, items):
        return len(items)

    async def remove_product(self, sku):
        return None

    async def remove_reference(self, sku, ref_id):
        return None

    async def ping(self):
        return True

    async def info(self):
        return {"model": "stub", "collection": "stub", "dim": 0, "references": 0}


# ---------------------------------------------------------------- http (Phase 4 service)
class _WMatch(BaseModel):
    sku: str
    score: float = Field(ge=0, le=1.0001)


class _WDet(BaseModel):
    bbox: tuple[float, float, float, float]
    det_confidence: float = Field(ge=0, le=1.0001)
    matches: list[_WMatch] = []
    flags: list[str] = []
    match_probability: float | None = Field(default=None, ge=0, le=1.0001)


class _WResp(BaseModel):
    detections: list[_WDet]


class HttpInference:
    def __init__(self, s: Settings):
        self.s = s
        headers = {"Authorization": f"Bearer {s.ai_worker_token}"} if s.ai_worker_token else {}
        self.http = httpx.AsyncClient(base_url=s.ai_worker_url, timeout=s.inference_timeout_seconds, headers=headers)
        self._info: tuple[float, dict] | None = None

    async def _post(self, path: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(2):  # one retry on transport errors / 502-504 (worker restarting)
            try:
                r = await self.http.post(path, **kw)
                if r.status_code in (502, 503, 504) and attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                r.raise_for_status()
                return r
            except httpx.TransportError as e:
                last = e
                await asyncio.sleep(1.0)
            except httpx.HTTPStatusError as e:
                raise InferenceError(f"AI service returned {e.response.status_code}") from e
        raise InferenceError(f"AI service unreachable: {last}")

    async def detect(self, image, *, scan_id, catalog, width, height, seed=None, dense=False):
        r = await self._post("/v1/detect", files={"file": ("scan.jpg", image, "image/jpeg")}, data={"scan_id": scan_id, "top_k": "5"})
        try:
            parsed = _WResp.model_validate(r.json())
        except Exception as e:
            raise InferenceError(f"AI service sent an invalid response: {e}") from e
        return [RawDetection(d.bbox, min(d.det_confidence, 1.0), [RawMatch(m.sku, min(m.score, 1.0)) for m in d.matches], d.flags,
                             None if d.match_probability is None else min(d.match_probability, 1.0))
                for d in parsed.detections]

    async def add_references(self, sku, name, items):
        files = [("files", (f"{rid}.jpg", data, "image/jpeg")) for rid, data in items]
        r = await self._post("/v1/references", files=files, data={"sku": sku, "name": name, "ref_ids": json.dumps([i for i, _ in items])})
        return int(r.json().get("added", 0))

    async def remove_product(self, sku):
        try:
            r = await self.http.delete(f"/v1/products/{quote(sku, safe='')}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise InferenceError(str(e)) from e

    async def remove_reference(self, sku, ref_id):
        try:
            r = await self.http.delete(f"/v1/products/{quote(sku, safe='')}/references/{quote(ref_id, safe='')}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise InferenceError(str(e)) from e

    async def ping(self):
        try:
            return (await self.http.get("/health", timeout=3)).status_code == 200
        except httpx.HTTPError:
            return False

    async def info(self):
        """Worker /health (model, collection, vector count). Cached for 30 s: it is read on every photo upload."""
        now = time.monotonic()
        if self._info and now - self._info[0] < 30:
            return self._info[1]
        try:
            r = await self.http.get("/health", timeout=5)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise InferenceError(f"AI service unreachable: {e}") from e
        out = {"model": body.get("model"), "collection": body.get("collection"), "dim": body.get("dim"), "references": body.get("references")}
        self._info = (now, out)
        return out


def make_inference(s: Settings) -> InferenceClient:
    return HttpInference(s) if s.inference_backend == "http" else StubInference(s)
