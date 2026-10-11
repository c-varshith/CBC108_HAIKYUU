"""Pure-numpy box maths: tiling, NMS, merge of full-image and tiled passes. No torch, fully unit-testable."""
from __future__ import annotations

import numpy as np


def tile_grid(width: int, height: int, tile: int, overlap: float) -> list[tuple[int, int, int, int]]:
    """Overlapping tiles (x1, y1, x2, y2) covering the image. The last row/column is shifted back so every
    tile is full-size, except when the image is smaller than one tile."""
    if width <= tile and height <= tile:
        return [(0, 0, width, height)]
    stride = max(1, int(tile * (1 - overlap)))

    def starts(n: int) -> list[int]:
        if n <= tile:
            return [0]
        s = list(range(0, n - tile, stride))
        s.append(n - tile)
        return s

    return [(x, y, min(x + tile, width), min(y + tile, height)) for y in starts(height) for x in starts(width)]


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU and intersection-over-smaller-area between every pair (rows of a x rows of b)."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    ix1 = np.maximum(a[:, None, 0], b[None, :, 0]); iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix2 = np.minimum(a[:, None, 2], b[None, :, 2]); iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]); ab = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(aa[:, None] + ab[None, :] - inter, 1e-9)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float, contain_thr: float = 0.85) -> np.ndarray:
    """Greedy NMS. Also drops a box that is mostly (>= contain_thr) inside a higher-scoring one, which is the
    typical artefact of an item cut by a tile border (half a box next to the full box from the next tile).
    Returns kept indices, best score first."""
    if len(boxes) == 0:
        return np.zeros((0,), dtype=int)
    order = np.argsort(-scores, kind="stable")
    keep: list[int] = []
    area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    for i in order:
        ok = True
        for j in keep:
            ix1, iy1 = max(boxes[i, 0], boxes[j, 0]), max(boxes[i, 1], boxes[j, 1])
            ix2, iy2 = min(boxes[i, 2], boxes[j, 2]), min(boxes[i, 3], boxes[j, 3])
            inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
            if inter <= 0:
                continue
            iou = inter / max(area[i] + area[j] - inter, 1e-9)
            inside = inter / max(min(area[i], area[j]), 1e-9)
            if iou > iou_thr or inside >= contain_thr:
                ok = False
                break
        if ok:
            keep.append(int(i))
    return np.array(keep, dtype=int)


def drop_border_cut(boxes: np.ndarray, scores: np.ndarray, tile: tuple[int, int, int, int], image_wh: tuple[int, int],
                    margin: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """In a tiled pass a box touching an *internal* tile border was cut in half: discard it (the neighbouring
    tile sees the whole item thanks to the overlap). Borders that coincide with the image edge are kept."""
    if len(boxes) == 0:
        return boxes, scores
    x1, y1, x2, y2 = tile
    w, h = image_wh
    cut = np.zeros(len(boxes), dtype=bool)
    if x1 > 0: cut |= boxes[:, 0] <= x1 + margin
    if y1 > 0: cut |= boxes[:, 1] <= y1 + margin
    if x2 < w: cut |= boxes[:, 2] >= x2 - margin
    if y2 < h: cut |= boxes[:, 3] >= y2 - margin
    return boxes[~cut], scores[~cut]


def clip_boxes(boxes: np.ndarray, width: int, height: int) -> np.ndarray:
    out = boxes.copy()
    out[:, [0, 2]] = np.clip(out[:, [0, 2]], 0, width)
    out[:, [1, 3]] = np.clip(out[:, [1, 3]], 0, height)
    return out


def merge_passes(passes: list[tuple[np.ndarray, np.ndarray]], iou_thr: float, max_det: int) -> tuple[np.ndarray, np.ndarray]:
    boxes = np.concatenate([p[0] for p in passes]) if passes else np.zeros((0, 4), np.float32)
    scores = np.concatenate([p[1] for p in passes]) if passes else np.zeros((0,), np.float32)
    keep = nms(boxes, scores, iou_thr)[:max_det]
    return boxes[keep], scores[keep]


def reading_order(boxes: np.ndarray) -> np.ndarray:
    """Indices sorted top-to-bottom, left-to-right with row clustering (stable ids across runs)."""
    if len(boxes) == 0:
        return np.zeros((0,), dtype=int)
    cy = (boxes[:, 1] + boxes[:, 3]) / 2
    med_h = float(np.median(boxes[:, 3] - boxes[:, 1])) or 1.0
    row = np.round(cy / (med_h * 0.8)).astype(int)
    return np.lexsort((boxes[:, 0], row))
