import json

import numpy as np
import pytest

from app.calibration import (Calibrator, expected_calibration_error, fit_isotonic, load_calibrator, reliability_table)
from app.config import Settings
from app.pipeline import Pipeline
from app.store import InMemoryStore
from tests.fakes import DIM, FakeDetector, FakeEmbedder, shelf, swatch


def synthetic(n=4000, seed=0):
    """Scores are over-confident on purpose: true P(correct) = score**3, so raw scores are NOT probabilities."""
    r = np.random.default_rng(seed)
    s = r.uniform(0.4, 1.0, n)
    return s, (r.uniform(size=n) < s ** 3).astype(float)


def test_fit_is_monotone_and_fixes_overconfident_scores():
    s, c = synthetic()
    tr, te = slice(0, 2000), slice(2000, None)
    cal = fit_isotonic(s[tr], c[tr])
    assert np.all(np.diff(cal.y) >= -1e-9)
    raw_ece = expected_calibration_error(s[te], c[te])
    cal_ece = expected_calibration_error(cal.predict(s[te]), c[te])
    assert raw_ece > 0.15 and cal_ece < 0.05  # raw similarity is badly miscalibrated, calibrated output is not
    assert 0.0 <= cal.one(0.0) <= cal.one(1.0) <= 1.0  # clamps outside the fitted range


def test_refuses_to_fit_on_too_little_or_one_sided_data():
    with pytest.raises(ValueError, match="at least"):
        fit_isotonic([0.9] * 10, [1, 0] * 5)
    with pytest.raises(ValueError, match="both outcomes"):
        fit_isotonic(np.linspace(0.5, 1, 100), np.ones(100))


def test_identical_scores_do_not_break_interp():
    cal = fit_isotonic([0.8] * 60, [1, 0] * 30)
    assert cal.one(0.8) == pytest.approx(0.5, abs=1e-3)


def test_json_roundtrip_and_validation(tmp_path):
    s, c = synthetic(500)
    cal = fit_isotonic(s, c)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(cal.to_json()))
    back = load_calibrator(str(path))
    assert back is not None and back.one(0.7) == pytest.approx(cal.one(0.7), abs=1e-5)
    assert load_calibrator(str(tmp_path / "missing.json")) is None
    path.write_text("{not json")
    assert load_calibrator(str(path)) is None  # corrupt file: fall back to similarity-only, never crash or fake it
    with pytest.raises(ValueError):
        Calibrator.from_json({"version": 1, "method": "isotonic", "x": [0.9, 0.5], "y": [0.1, 0.2]})


def test_reliability_table_bins():
    rows = reliability_table([0.05, 0.06, 0.95, 0.96], [0, 0, 1, 1])
    assert [r["n"] for r in rows] == [2, 2] and rows[1]["observed_accuracy"] == 1.0


def _pipeline(calibrator):
    img, boxes = shelf([(200, 30, 30), (30, 190, 60)])
    p = Pipeline(Settings(), FakeDetector(boxes), FakeEmbedder(), InMemoryStore(DIM), calibrator)
    p.add_references("R", "Red", [("r1", swatch((200, 30, 30)))])
    return p, img


def test_pipeline_probability_only_when_calibrated():
    p, img = _pipeline(None)
    dets, _ = p.detect(img, 3)
    assert all(d["match_probability"] is None for d in dets)  # similarity only: never pretend it is a probability

    cal = Calibrator(np.array([0.0, 1.0]), np.array([0.0, 0.5]))
    p, img = _pipeline(cal)
    dets, _ = p.detect(img, 3)
    top = dets[0]["matches"][0]["score"]
    assert dets[0]["match_probability"] == pytest.approx(round(top * 0.5, 4), abs=1e-3)


def test_health_reports_calibration(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app.main import create_app
    cal = fit_isotonic(*synthetic(300))
    p, _ = _pipeline(cal)
    with TestClient(create_app(Settings(), p)) as c:
        h = c.get("/health").json()
    assert h["calibrated"] is True and h["calibration_samples"] == 300
