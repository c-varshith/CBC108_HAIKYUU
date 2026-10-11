"""POST /analyze: the endpoint the AI-inventory frontend calls (multipart field "image").

Returns one detection *group* per SKU, in the shape frontend/src/lib/analysis.ts expects:
[{id, label, quantity, confidence, box: {x,y,w,h} normalised 0..1, flags: [...],
  boxes: [{x,y,w,h,confidence}] one per counted unit, uncertain_units,
  detection_confidence, match_score, match_probability, calibrated}]

quantity = number of detected boxes whose best match is that SKU.
detection_confidence = mean detector score. match_score = mean DINOv2 cosine *similarity* of the best SKU (NOT a probability).
match_probability = mean calibrated P(best SKU correct), present only when the worker has a calibration fitted on labelled
boxes (calibrated=true); otherwise null and the UI must call match_score "similarity".
confidence (kept for older clients) = mean of (match_probability if calibrated else match_score) * detection_confidence.

`?live=1` marks a live-camera frame: same processing, separate (higher) rate-limit bucket so a 1 fps stream is not throttled
by the single-upload limit.
Runs the same image pipeline and inference client (stub or Phase-4 http worker) as the scan workflow."""
from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from app.config import Settings
from app.deps import get_inference, get_settings_dep
from app.errors import ApiError, bad_request
from app.infra.imaging import ImageRejected, aprocess_scan_image
from app.infra.ratelimit import RateLimiter

router = APIRouter(tags=["analyze"])
_limiters: dict[tuple[bool, int], RateLimiter] = {}  # (live, per-minute limit) -> limiter, per client IP


def _limiter(live: bool, per_minute: int) -> RateLimiter:
    return _limiters.setdefault((live, per_minute), RateLimiter(per_minute, 60))


def reset_analyze_limiters() -> None:  # tests
    _limiters.clear()


@router.post("/analyze")
async def analyze(request: Request, image: UploadFile = File(...), live: bool = Query(False),
                  s: Settings = Depends(get_settings_dep), inference=Depends(get_inference)):
    if not s.analyze_enabled:
        raise ApiError(404, "Not found")
    wait = _limiter(live, s.analyze_live_per_minute if live else s.analyze_per_minute).hit(
        request.client.host if request.client else "?")
    if wait:
        raise ApiError(429, f"Too many requests. Try again in {wait}s.", headers={"Retry-After": str(wait)})
    data = await image.read(s.max_upload_bytes + 1)
    if not data:
        raise bad_request("The uploaded file is empty.")
    if len(data) > s.max_upload_bytes:
        raise ApiError(413, "File is too large.")
    try:
        img = await aprocess_scan_image(data, s)
    except ImageRejected as e:
        raise ApiError(e.status, e.message)

    catalog = [(name, name) for name in s.analyze_catalog]
    raw = await inference.detect(img.original, scan_id=uuid.uuid4().hex, catalog=catalog,
                                 width=img.width, height=img.height)

    groups: dict[str, list] = defaultdict(list)
    unknown: list = []
    for d in raw[: s.max_detections]:
        top = d.matches[0] if d.matches else None
        if top is None or d.det_confidence < s.det_conf_min:
            unknown.append(d)
        else:
            groups[top.sku].append(d)

    def unit_conf(d, calibrated: bool) -> float:
        top = d.matches[0] if d.matches else None
        match = (d.match_probability if calibrated else top.score) if top else d.det_confidence
        return match * d.det_confidence

    def pack(label: str | None, dets: list) -> dict:
        x1 = min(d.bbox[0] for d in dets); y1 = min(d.bbox[1] for d in dets)
        x2 = max(d.bbox[2] for d in dets); y2 = max(d.bbox[3] for d in dets)
        calibrated = label is not None and all(d.match_probability is not None for d in dets)
        units = [unit_conf(d, calibrated) for d in dets]
        conf = sum(units) / len(dets)
        sims = [d.matches[0].score for d in dets if d.matches]
        flags = sorted({f for d in dets for f in d.flags})
        if conf < s.analyze_review_threshold:
            flags.append("uncertain")
        if label is None:
            flags.append("unsupported")
        n = lambda v, m: round(min(1.0, max(0.0, v / m)), 3)
        return {"id": f"api-{uuid.uuid4().hex[:8]}", "label": label, "quantity": len(dets),
                "confidence": round(conf, 3), "flags": flags,
                "detection_confidence": round(sum(d.det_confidence for d in dets) / len(dets), 3),
                "match_score": round(sum(sims) / len(sims), 3) if sims else None,
                "match_probability": round(sum(d.match_probability for d in dets) / len(dets), 3) if calibrated else None,
                "calibrated": calibrated,
                "uncertain_units": sum(1 for u in units if u < s.analyze_review_threshold),
                "box": {"x": n(x1, img.width), "y": n(y1, img.height),
                        "w": n(x2 - x1, img.width), "h": n(y2 - y1, img.height)},
                "boxes": [{"x": n(d.bbox[0], img.width), "y": n(d.bbox[1], img.height),
                           "w": n(d.bbox[2] - d.bbox[0], img.width), "h": n(d.bbox[3] - d.bbox[1], img.height),
                           "confidence": round(u, 3)} for d, u in zip(dets, units)]}

    out = [pack(sku, dets) for sku, dets in groups.items()]
    if unknown:
        out.append(pack(None, unknown))
    return out
