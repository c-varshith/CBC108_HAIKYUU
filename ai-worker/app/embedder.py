"""DINOv2 embeddings: crop (small padding) -> resize 224 -> CLS token -> L2-normalise. Batched, FP16 on CUDA."""
from __future__ import annotations

import logging
from typing import Protocol

import numpy as np
from PIL import Image

from app.config import Settings

log = logging.getLogger("ai.embedder")
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class Embedder(Protocol):
    dim: int

    def embed(self, crops: list[Image.Image]) -> np.ndarray: ...  # (n, dim) float32, L2-normalised


def resolve_device(pref: str) -> str:
    if pref != "auto":
        return pref
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def preprocess(img: Image.Image, size: int) -> np.ndarray:
    """RGB PIL -> CHW float32, ImageNet-normalised. Bicubic resize to a square (shelf items are not centre-cropped:
    dropping the edges of a narrow box would lose the label)."""
    a = np.asarray(img.convert("RGB").resize((size, size), Image.BICUBIC), dtype=np.float32) / 255.0
    return ((a - _MEAN) / _STD).transpose(2, 0, 1)


def padded_crop(img: Image.Image, box: tuple[float, float, float, float], pad: float) -> Image.Image:
    x1, y1, x2, y2 = box
    pw, ph = (x2 - x1) * pad, (y2 - y1) * pad
    return img.crop((max(0, int(x1 - pw)), max(0, int(y1 - ph)), min(img.width, int(x2 + pw) + 1), min(img.height, int(y2 + ph) + 1)))


class DinoEmbedder:
    def __init__(self, s: Settings, device: str):
        import torch
        from transformers import AutoModel

        self.torch, self.s, self.device = torch, s, device
        self.model = AutoModel.from_pretrained(s.dino_model).to(device).eval()
        self.half = device == "cuda"
        if self.half:
            self.model = self.model.half()
        self.dim = int(self.model.config.hidden_size)
        log.info("DINOv2 loaded: %s dim=%d device=%s fp16=%s", s.dino_model, self.dim, device, self.half)

    def embed(self, crops: list[Image.Image]) -> np.ndarray:
        if not crops:
            return np.zeros((0, self.dim), dtype=np.float32)
        torch, out = self.torch, []
        with torch.inference_mode():
            for i in range(0, len(crops), self.s.embed_batch):
                batch = np.stack([preprocess(c, self.s.crop_size) for c in crops[i:i + self.s.embed_batch]])
                x = torch.from_numpy(batch).to(self.device)
                if self.half:
                    x = x.half()
                cls = self.model(pixel_values=x).last_hidden_state[:, 0]
                cls = torch.nn.functional.normalize(cls.float(), dim=-1)
                out.append(cls.cpu().numpy())
        return np.concatenate(out).astype(np.float32)
