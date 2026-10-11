"""detect -> crop -> embed -> vector search -> per-SKU aggregation. Thresholds are NOT applied here: the backend owns
the Owner-editable matrix, this service returns raw cosine scores plus crop-quality flags."""
from __future__ import annotations

import io
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

from app.calibration import Calibrator
from app.config import Settings
from app.detector import Detector
from app.embedder import Embedder, padded_crop
from app.quality import crop_flags
from app.store import VectorStore, aggregate_by_sku


@dataclass
class Timings:
    detect_ms: float = 0.0
    embed_ms: float = 0.0
    search_ms: float = 0.0


def load_image(data: bytes) -> Image.Image:
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # the backend already normalises, but never trust the caller
    return img.convert("RGB")


class Pipeline:
    def __init__(self, s: Settings, detector: Detector, embedder: Embedder, store: VectorStore,
                 calibrator: Calibrator | None = None):
        self.s, self.detector, self.embedder, self.store = s, detector, embedder, store
        self.calibrator = calibrator

    # -------------------------------------------------------------- detect
    def detect(self, img: Image.Image, top_k: int = 5) -> tuple[list[dict], Timings]:
        s, t = self.s, Timings()
        t0 = time.perf_counter()
        boxes, scores = self.detector.detect(img)
        t.detect_ms = (time.perf_counter() - t0) * 1000
        if len(boxes) == 0:
            return [], t

        crops, flags = [], []
        for b in boxes:
            c = padded_crop(img, tuple(float(v) for v in b), s.crop_pad)
            crops.append(c)
            flags.append(crop_flags(np.asarray(c), s))

        t0 = time.perf_counter()
        vecs = self.embedder.embed(crops)
        t.embed_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        hits = self.store.search(vecs, max(s.search_k, top_k))
        t.search_ms = (time.perf_counter() - t0) * 1000

        out = []
        for b, sc, fl, h in zip(boxes, scores, flags, hits):
            m = aggregate_by_sku(h, top_k)
            # P(top-1 SKU is correct), only when a calibration fitted on labelled boxes is loaded; otherwise null.
            prob = round(self.calibrator.one(m[0].score), 4) if self.calibrator and m else None
            out.append({"bbox": [round(float(v), 1) for v in b], "det_confidence": round(float(sc), 4),
                        "matches": [{"sku": x.sku, "score": round(x.score, 4)} for x in m],
                        "match_probability": prob, "flags": fl})
        return out, t

    # -------------------------------------------------------------- references
    def add_references(self, sku: str, name: str, items: list[tuple[str, Image.Image]]) -> int:
        """Embed every reference photo (+ an 85% centre crop as light augmentation) and upsert. No training."""
        added = 0
        for ref_id, im in items:
            variants = [im]
            if self.s.ref_augment and min(im.size) >= 64:
                w, h = im.size
                dx, dy = int(w * 0.075), int(h * 0.075)
                variants.append(im.crop((dx, dy, w - dx, h - dy)))
            added += self.store.upsert(sku, name, ref_id, self.embedder.embed(variants))
        return added

    def remove_product(self, sku: str) -> None:
        self.store.delete_sku(sku)

    def remove_reference(self, sku: str, ref_id: str) -> None:
        self.store.delete_reference(sku, ref_id)
