"""Deterministic stand-ins so the pipeline logic is testable without torch/ultralytics/Qdrant."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

DIM = 12


class FakeEmbedder:
    """Colour histogram-ish embedding: items of the same colour are close, different colours far apart."""
    dim = DIM

    def embed(self, crops):
        out = []
        for c in crops:
            a = np.asarray(c.convert("RGB").resize((8, 8)), dtype=np.float32) / 255.0
            m = a.reshape(-1, 3).mean(0)
            v = np.concatenate([m, m ** 2, np.sin(m * 5), np.cos(m * 5)])
            out.append(v / (np.linalg.norm(v) + 1e-9))
        return np.array(out, dtype=np.float32)


class FakeDetector:
    def __init__(self, boxes, scores=None):
        self.boxes = np.array(boxes, dtype=np.float32).reshape(-1, 4)
        self.scores = np.array(scores if scores is not None else [0.9] * len(boxes), dtype=np.float32)

    def detect(self, img):
        return self.boxes, self.scores


def shelf(colors: list[tuple[int, int, int]], w=40, h=60, gap=6, noise=0, seed=0):
    """Image with one coloured rectangle per item, plus the matching boxes."""
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", (len(colors) * (w + gap) + gap, h + 2 * gap), (30, 30, 30))
    d = ImageDraw.Draw(img)
    boxes = []
    for i, c in enumerate(colors):
        x = gap + i * (w + gap)
        cc = tuple(int(np.clip(v + rng.integers(-noise, noise + 1), 0, 255)) if noise else v for v in c)
        d.rectangle([x, gap, x + w, gap + h], fill=cc)
        boxes.append([x, gap, x + w, gap + h])
    return img, boxes


def swatch(color, size=64):
    return Image.new("RGB", (size, size), color)
