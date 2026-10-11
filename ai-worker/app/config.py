"""Environment-driven settings. Plain dataclass (no extra dependency); every field has a safe default."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _h5_path() -> str:
    """Resolve the project model locally or the mounted model path in Docker."""
    configured = os.environ.get("H5_MODEL_PATH")
    if configured:
        return configured
    filename = "iou_resnet50_csv_06.h5"
    candidates = (Path(__file__).resolve().parents[2] / filename, Path("/models") / filename)
    return str(next((p for p in candidates if p.is_file()), candidates[0]))


def _yolo_path() -> str:
    """Resolve the fine-tuned Calibrex checkpoint locally or its Docker image path."""
    configured = os.environ.get("YOLO_WEIGHTS")
    if configured:
        return configured
    filename = Path("runs/detect/runs/detect/calibrex_s960/weights/best.pt")
    candidates = (Path(__file__).resolve().parents[1] / filename, Path("/srv/calibrex-best.pt"),
                 Path("/models/calibrex-best.pt"))
    return str(next((p for p in candidates if p.is_file()), candidates[0]))


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("ENV", "development"))
    token: str | None = field(default_factory=lambda: os.environ.get("AI_WORKER_TOKEN") or None)
    device: str = field(default_factory=lambda: os.environ.get("DEVICE", "auto"))
    max_concurrency: int = field(default_factory=lambda: _i("MAX_CONCURRENCY", 1))

    yolo_weights: str = field(default_factory=_yolo_path)
    detector_backend: str = field(default_factory=lambda: os.environ.get("DETECTOR_BACKEND", "yolo").strip().lower())
    h5_model_path: str = field(default_factory=_h5_path)
    h5_min_side: int = field(default_factory=lambda: _i("H5_MIN_SIDE", 800))
    h5_max_side: int = field(default_factory=lambda: _i("H5_MAX_SIDE", 1333))
    det_imgsz: int = field(default_factory=lambda: _i("DET_IMGSZ", 1280))
    det_conf: float = field(default_factory=lambda: _f("DET_CONF", 0.25))
    det_iou: float = field(default_factory=lambda: _f("DET_IOU", 0.6))
    det_max: int = field(default_factory=lambda: _i("DET_MAX", 1000))
    tile_trigger_px: int = field(default_factory=lambda: _i("TILE_TRIGGER_PX", 2200))
    tile_size: int = field(default_factory=lambda: _i("TILE_SIZE", 1280))
    tile_overlap: float = field(default_factory=lambda: _f("TILE_OVERLAP", 0.2))

    dino_model: str = field(default_factory=lambda: os.environ.get("DINO_MODEL", "facebook/dinov2-base"))
    crop_pad: float = field(default_factory=lambda: _f("CROP_PAD", 0.04))
    crop_size: int = field(default_factory=lambda: _i("CROP_SIZE", 224))
    embed_batch: int = field(default_factory=lambda: _i("EMBED_BATCH", 64))
    ref_augment: bool = field(default_factory=lambda: _b("REF_AUGMENT", True))

    qdrant_url: str = field(default_factory=lambda: os.environ.get("QDRANT_URL", "http://qdrant:6333"))
    qdrant_api_key: str | None = field(default_factory=lambda: os.environ.get("QDRANT_API_KEY") or None)
    qdrant_collection: str = field(default_factory=lambda: os.environ.get("QDRANT_COLLECTION", "calibrex_refs"))
    search_k: int = field(default_factory=lambda: _i("SEARCH_K", 12))
    # Fitted by `python -m eval.evaluate --calibration-out`. Missing file = similarity only, no probabilities.
    calibration_path: str = field(default_factory=lambda: os.environ.get("CALIBRATION_PATH", "/models/calibration.json"))

    min_crop_side_px: int = field(default_factory=lambda: _i("MIN_CROP_SIDE_PX", 24))
    blur_laplacian_var: float = field(default_factory=lambda: _f("BLUR_LAPLACIAN_VAR", 60.0))
    glare_fraction: float = field(default_factory=lambda: _f("GLARE_FRACTION", 0.30))

    max_upload_mb: int = field(default_factory=lambda: _i("MAX_UPLOAD_MB", 40))
    max_refs_per_call: int = field(default_factory=lambda: _i("MAX_REFS_PER_CALL", 12))
