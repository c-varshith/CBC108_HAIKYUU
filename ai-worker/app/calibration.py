"""Turn a raw DINOv2 cosine similarity into a probability that the top-1 SKU is right.

The raw match score is a similarity, NOT a probability: 0.85 does not mean "85% likely correct". To make the
percentages shown in the UI meaningful, fit this mapping on labelled real shelf validation boxes
(`python -m eval.evaluate --data ./real_eval_set --calibration-out /models/calibration.json`) and point
CALIBRATION_PATH at the result. Without a fitted file the worker returns `match_probability: null` and the UI labels
the number "similarity", never "probability".

Method: isotonic regression (pool-adjacent-violators) on top-1 score -> was-the-top-1-SKU-correct. It is monotone,
needs no distribution assumption and is implemented in numpy so the worker gains no dependency. It is a 1-D map on the
score only; the backend's separate margin rule (best vs best-different SKU) is untouched.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("ai.calibration")

MIN_SAMPLES = 50  # below this an isotonic fit is mostly noise: refuse instead of shipping fake precision


@dataclass(frozen=True)
class Calibrator:
    x: np.ndarray  # increasing score knots
    y: np.ndarray  # non-decreasing probabilities at those knots
    n: int = 0
    ece: float | None = None  # held-out expected calibration error recorded at fit time
    fitted_at: str | None = None

    def predict(self, scores) -> np.ndarray:
        s = np.asarray(scores, dtype=np.float64)
        return np.clip(np.interp(s, self.x, self.y), 0.0, 1.0)

    def one(self, score: float) -> float:
        return float(self.predict([score])[0])

    def to_json(self) -> dict:
        return {"version": 1, "method": "isotonic", "x": [round(float(v), 6) for v in self.x],
                "y": [round(float(v), 6) for v in self.y], "n": int(self.n), "ece": self.ece, "fitted_at": self.fitted_at}

    @staticmethod
    def from_json(d: dict) -> "Calibrator":
        if d.get("version") != 1 or d.get("method") != "isotonic":
            raise ValueError("unsupported calibration file")
        x, y = np.asarray(d["x"], dtype=np.float64), np.asarray(d["y"], dtype=np.float64)
        if x.ndim != 1 or x.shape != y.shape or len(x) < 2 or np.any(np.diff(x) <= 0) or np.any(np.diff(y) < -1e-9):
            raise ValueError("calibration knots must be increasing in x and non-decreasing in y")
        return Calibrator(x, y, int(d.get("n", 0)), d.get("ece"), d.get("fitted_at"))


def fit_isotonic(scores, correct, min_samples: int = MIN_SAMPLES) -> Calibrator:
    """Pool-adjacent-violators fit. `correct` is 0/1 (top-1 SKU matched the label)."""
    s = np.asarray(scores, dtype=np.float64).ravel()
    c = np.asarray(correct, dtype=np.float64).ravel()
    if len(s) != len(c):
        raise ValueError("scores and correct must have the same length")
    if len(s) < min_samples:
        raise ValueError(f"need at least {min_samples} labelled boxes to calibrate, got {len(s)}")
    if c.min() == c.max():
        raise ValueError("labels are all correct or all wrong: cannot calibrate (need both outcomes)")
    order = np.argsort(s, kind="stable")
    s, c = s[order], c[order]
    # blocks of [sum_y, weight, x_min, x_max]; merge while the mean decreases
    blocks: list[list[float]] = []
    for xi, yi in zip(s, c):
        blocks.append([yi, 1.0, xi, xi])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            b = blocks.pop()
            blocks[-1][0] += b[0]; blocks[-1][1] += b[1]; blocks[-1][3] = b[3]
    xs, ys = [], []
    for sy, w, lo, hi in blocks:
        m = sy / w
        xs += [lo, hi] if hi > lo else [lo]
        ys += [m, m] if hi > lo else [m]
    x, y = np.asarray(xs), np.asarray(ys)
    keep = np.concatenate([[True], np.diff(x) > 0])  # np.interp needs strictly increasing x
    x, y = x[keep], y[keep]
    if len(x) < 2:  # degenerate: every score identical
        x, y = np.array([x[0] - 1e-6, x[0] + 1e-6]), np.array([y[0], y[0]])
    return Calibrator(x, y, n=len(s), fitted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def reliability_table(probs, correct, bins: int = 10) -> list[dict]:
    p = np.asarray(probs, dtype=np.float64).ravel()
    c = np.asarray(correct, dtype=np.float64).ravel()
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int(m.sum()),
                         "mean_predicted": round(float(p[m].mean()), 4), "observed_accuracy": round(float(c[m].mean()), 4)})
    return rows


def expected_calibration_error(probs, correct, bins: int = 10) -> float:
    """Weighted mean |predicted - observed| over equal-width bins. 0 = perfectly calibrated."""
    p = np.asarray(probs, dtype=np.float64).ravel()
    if len(p) == 0:
        return float("nan")
    return float(sum(r["n"] * abs(r["mean_predicted"] - r["observed_accuracy"]) for r in reliability_table(p, correct, bins)) / len(p))


def load_calibrator(path: str | None) -> Calibrator | None:
    """None (not an error) when no file exists: the worker then reports similarity only."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        cal = Calibrator.from_json(json.loads(p.read_text()))
    except Exception as e:  # a corrupt file must not take the worker down or fake probabilities
        log.warning("ignoring calibration file %s: %s", p, e)
        return None
    log.info("loaded calibration %s (n=%d, held-out ECE=%s)", p, cal.n, cal.ece)
    return cal
