"""Class-agnostic localisation with YOLO11: boxes + confidence ONLY. Tiled inference for high-resolution photos."""
from __future__ import annotations

import logging
import os
from typing import Protocol

import numpy as np
from PIL import Image

from app.config import Settings
from app.geometry import clip_boxes, drop_border_cut, merge_passes, nms, reading_order, tile_grid

log = logging.getLogger("ai.detector")


class Detector(Protocol):
    def detect(self, img: Image.Image) -> tuple[np.ndarray, np.ndarray]: ...  # boxes (n,4) xyxy pixels, scores (n,)


class YoloDetector:
    def __init__(self, s: Settings, device: str):
        path = s.yolo_weights
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise FileNotFoundError(f"Configured YOLO detector weights are missing or empty: {path}")
        from ultralytics import YOLO

        try:
            self.model = YOLO(path)
        except Exception as exc:
            raise RuntimeError(f"Could not load configured YOLO detector weights: {path}") from exc
        self.s, self.device = s, device
        self.half = device == "cuda"
        log.info("YOLO loaded: %s device=%s", path, device)

    def _predict(self, arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        r = self.model.predict(arr, imgsz=self.s.det_imgsz, conf=self.s.det_conf, iou=self.s.det_iou, max_det=self.s.det_max,
                               agnostic_nms=True, device=self.device, half=self.half, verbose=False)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
        return r.boxes.xyxy.cpu().numpy().astype(np.float32), r.boxes.conf.cpu().numpy().astype(np.float32)

    def detect(self, img: Image.Image):
        s, (w, h) = self.s, img.size
        arr = np.asarray(img.convert("RGB"))
        passes = [self._predict(arr)]  # full-image pass: sees large items and global context
        if max(w, h) > s.tile_trigger_px:
            for t in tile_grid(w, h, s.tile_size, s.tile_overlap):  # tiled pass: small items on 12-50 MP photos
                b, sc = self._predict(arr[t[1]:t[3], t[0]:t[2]])
                if len(b):
                    b = b + np.array([t[0], t[1], t[0], t[1]], dtype=np.float32)
                    passes.append(drop_border_cut(b, sc, t, (w, h)))
        boxes, scores = merge_passes([(clip_boxes(b, w, h), sc) for b, sc in passes], s.det_iou, s.det_max)
        o = reading_order(boxes)
        return boxes[o], scores[o]


def retinanet_anchors(height: int, width: int) -> np.ndarray:
    """Generate the default P3-P7 RetinaNet anchors used by the bundled Keras model."""
    ratios = np.array([0.5, 1.0, 2.0], dtype=np.float32)
    scales = np.array([1.0, 2 ** (1 / 3), 2 ** (2 / 3)], dtype=np.float32)
    anchors = []
    for level, stride in enumerate((8, 16, 32, 64, 128)):
        base = 32 * (2 ** level)
        sizes = base * scales
        # This ordering matches the model's 3 ratios x 3 scales classification/regression heads.
        areas = np.tile(sizes, (len(ratios), 1)) ** 2
        ws = np.sqrt(areas / ratios[:, None])
        hs = ws * ratios[:, None]
        base_anchors = np.stack((-ws / 2, -hs / 2, ws / 2, hs / 2), axis=-1).reshape(-1, 4)
        rows, cols = (height + stride - 1) // stride, (width + stride - 1) // stride
        xx, yy = np.meshgrid((np.arange(cols) + 0.5) * stride, (np.arange(rows) + 0.5) * stride)
        shifts = np.stack((xx, yy, xx, yy), axis=-1).reshape(-1, 1, 4)
        anchors.append((shifts + base_anchors[None, :, :]).reshape(-1, 4))
    return np.concatenate(anchors).astype(np.float32)


def decode_retinanet(regression: np.ndarray, classification: np.ndarray, anchors: np.ndarray,
                     image_width: int, image_height: int, scale: float, conf: float, iou: float,
                     max_det: int) -> tuple[np.ndarray, np.ndarray]:
    """Decode RetinaNet corner deltas and single-class scores into original-image xyxy boxes."""
    reg = np.asarray(regression, dtype=np.float32).reshape(-1, 4)
    cls = np.asarray(classification, dtype=np.float32).reshape(len(reg), -1)
    if len(reg) != len(anchors) or cls.shape[1] < 1:
        raise ValueError(f"RetinaNet output has {len(reg)} boxes/{cls.shape[1]} classes for {len(anchors)} anchors")
    scores = cls.max(axis=1)
    valid = np.isfinite(scores) & (scores >= conf) & np.isfinite(reg).all(axis=1)
    indices = np.flatnonzero(valid)
    if not len(indices):
        return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)

    # Training targets use corner deltas normalised by 0.2 (keras-retinanet default).
    anc = anchors[indices]
    delta = reg[indices] * 0.2
    boxes = anc + delta * np.stack((anc[:, 2] - anc[:, 0], anc[:, 3] - anc[:, 1],
                                    anc[:, 2] - anc[:, 0], anc[:, 3] - anc[:, 1]), axis=1)
    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, image_width * scale)
    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, image_height * scale)
    boxes /= max(scale, 1e-9)
    candidate_scores = scores[indices]
    top = np.argsort(-candidate_scores, kind="stable")[:max(max_det * 10, 1000)]
    boxes, candidate_scores = boxes[top], candidate_scores[top]
    keep = nms(boxes, candidate_scores, iou, contain_thr=2.0)[:max_det]
    return boxes[keep].astype(np.float32), candidate_scores[keep].astype(np.float32)


def _load_keras_retinanet(path: str):
    """Load the full legacy Keras model; its only nonstandard layer is UpsampleLike."""
    os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
    try:
        import tensorflow as tf
        import tf_keras
    except ImportError as exc:
        raise RuntimeError("DETECTOR_BACKEND=h5 requires optional ai-worker/requirements-h5.txt dependencies") from exc

    class UpsampleLike(tf_keras.layers.Layer):
        def call(self, inputs, **kwargs):
            source, reference = inputs
            target_hw = tf.shape(reference)[1:3]
            return tf.image.resize(source, target_hw, method="nearest")

    class LegacyBatchNormalization(tf_keras.layers.BatchNormalization):
        def __init__(self, freeze=False, **kwargs):
            self.legacy_freeze = freeze
            if freeze:
                kwargs["trainable"] = False
            super().__init__(**kwargs)

    class PriorProbability(tf_keras.initializers.Initializer):
        def __init__(self, probability=0.01):
            self.probability = probability

        def __call__(self, shape, dtype=None):
            value = -np.log((1.0 - self.probability) / self.probability)
            return tf.fill(shape, tf.cast(value, dtype or tf.float32))

        def get_config(self):
            return {"probability": self.probability}

    objects = {"UpsampleLike": UpsampleLike, "BatchNormalization": LegacyBatchNormalization,
               "PriorProbability": PriorProbability}
    return tf_keras.models.load_model(path, custom_objects=objects, compile=False)


class H5RetinaNetDetector:
    """Optional TensorFlow detector for the bundled one-class RetinaNet .h5 model."""

    def __init__(self, s: Settings, device: str, model=None):
        if model is None:
            if not os.path.isfile(s.h5_model_path):
                raise FileNotFoundError(f"H5 detector model not found: {s.h5_model_path}")
            model = _load_keras_retinanet(s.h5_model_path)
        self.model, self.s, self.device = model, s, device
        log.info("H5 RetinaNet loaded: %s device=%s", s.h5_model_path, device)

    def _predict(self, arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h, w = arr.shape[:2]
        scale = min(self.s.h5_min_side / min(h, w), self.s.h5_max_side / max(h, w))
        rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
        # keras-retinanet ResNet models use Caffe preprocessing: RGB -> BGR, then ImageNet mean subtraction.
        resized = np.asarray(Image.fromarray(arr).resize((rw, rh), Image.Resampling.BILINEAR), dtype=np.float32)
        batch = resized[..., ::-1].copy() - np.array([103.939, 116.779, 123.68], dtype=np.float32)
        outputs = self.model(np.expand_dims(batch, axis=0), training=False)
        if isinstance(outputs, dict):
            regression, classification = outputs["regression"], outputs["classification"]
        else:
            if len(outputs) < 2:
                raise ValueError("H5 RetinaNet must return regression and classification heads")
            regression, classification = outputs[:2]  # the optional third output is the IoU auxiliary head
        regression = np.asarray(regression)[0]
        classification = np.asarray(classification)[0]
        anchors = retinanet_anchors(rh, rw)
        boxes, scores = decode_retinanet(regression, classification, anchors, w, h, scale,
                                         self.s.det_conf, self.s.det_iou, self.s.det_max)
        return boxes, scores

    def detect(self, img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
        s, (w, h) = self.s, img.size
        arr = np.asarray(img.convert("RGB"))
        passes = [self._predict(arr)]
        if max(w, h) > s.tile_trigger_px:
            for tile in tile_grid(w, h, s.tile_size, s.tile_overlap):
                x1, y1, x2, y2 = tile
                boxes, scores = self._predict(arr[y1:y2, x1:x2])
                if len(boxes):
                    boxes += np.array([x1, y1, x1, y1], dtype=np.float32)
                    passes.append(drop_border_cut(boxes, scores, tile, (w, h)))
        boxes, scores = merge_passes([(clip_boxes(b, w, h), sc) for b, sc in passes], s.det_iou, s.det_max)
        order = reading_order(boxes)
        return boxes[order], scores[order]


def build_detector(s: Settings, device: str) -> Detector:
    """Select only the configured detector; H5 errors must not silently change model behavior."""
    backend = s.detector_backend
    if backend == "yolo":
        return YoloDetector(s, device)
    if backend != "h5":
        raise ValueError("DETECTOR_BACKEND must be 'h5' or 'yolo'")
    return H5RetinaNetDetector(s, device)
