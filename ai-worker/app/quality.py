"""Crop-quality flags. The backend demotes SURE to CHECK when any flag is present (spec 5.4)."""
from __future__ import annotations

import numpy as np

from app.config import Settings


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian: low = blurry. gray is a 2-D float array."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    g = gray.astype(np.float32)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def glare_fraction(rgb: np.ndarray, thr: int = 245) -> float:
    """Share of pixels where every channel is near saturation (specular highlights on shrink-wrap, glass, foil)."""
    return float((rgb.min(axis=2) >= thr).mean())


def crop_flags(rgb: np.ndarray, s: Settings) -> list[str]:
    """rgb: HxWx3 uint8 crop (before resizing to 224, so blur is measured on real pixels)."""
    h, w = rgb.shape[:2]
    flags: list[str] = []
    if min(h, w) < s.min_crop_side_px:
        flags.append("tiny")
        return flags  # blur/glare statistics are meaningless on a few pixels
    gray = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    # normalise by size: downscale big crops so the metric does not grow with resolution
    step = max(1, min(h, w) // 96)
    if laplacian_variance(gray[::step, ::step]) < s.blur_laplacian_var:
        flags.append("blurry")
    if glare_fraction(rgb) > s.glare_fraction:
        flags.append("glare")
    return flags
