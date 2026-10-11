"""/analyze: per-SKU quantity, honest similarity-vs-probability fields, per-unit boxes, live-frame rate bucket."""
from __future__ import annotations

import pytest

from app.infra.inference import RawDetection, RawMatch
from app.routers.analyze import reset_analyze_limiters
from tests.helpers import make_jpeg


class FixedInference:
    """Two boxes of SKU A, one of SKU B, one low-confidence unknown; optionally with calibrated probabilities."""

    def __init__(self, calibrated: bool):
        p = (lambda v: v) if calibrated else (lambda v: None)
        # image is 800x600; boxes in pixels
        self.dets = [
            RawDetection((0, 0, 100, 150), 0.9, [RawMatch("A", 0.95), RawMatch("B", 0.5)], [], p(0.97)),
            RawDetection((110, 0, 210, 150), 0.9, [RawMatch("A", 0.90), RawMatch("B", 0.5)], [], p(0.90)),
            RawDetection((300, 300, 400, 450), 0.8, [RawMatch("B", 0.70)], ["blurry"], p(0.55)),
            RawDetection((500, 0, 600, 100), 0.2, [RawMatch("A", 0.99)], [], p(0.99)),  # detector too unsure: unknown
        ]

    async def detect(self, image, **kw):
        return self.dets


@pytest.fixture(autouse=True)
def _fresh_limiters():
    reset_analyze_limiters()


def post(client, params=None):
    return client.post("/analyze", params=params, files={"image": ("f.jpg", make_jpeg(3, (800, 600)), "image/jpeg")})


async def test_counts_boxes_per_sku_and_keeps_every_unit_box(client):
    client.app.state.inference = FixedInference(calibrated=False)
    r = await post(client)
    assert r.status_code == 200
    g = {x["label"]: x for x in r.json()}
    assert g["A"]["quantity"] == 2 and g["B"]["quantity"] == 1 and g[None]["quantity"] == 1
    assert len(g["A"]["boxes"]) == 2 and g["A"]["boxes"][0] == pytest.approx({"x": 0.0, "y": 0.0, "w": 0.125, "h": 0.25, "confidence": g["A"]["boxes"][0]["confidence"]})
    assert "unsupported" in g[None]["flags"]


async def test_uncalibrated_scores_are_similarity_never_a_probability(client):
    client.app.state.inference = FixedInference(calibrated=False)
    g = {x["label"]: x for x in (await post(client)).json()}
    assert g["A"]["calibrated"] is False and g["A"]["match_probability"] is None
    assert g["A"]["match_score"] == pytest.approx(0.925, abs=1e-3)
    assert g["A"]["detection_confidence"] == pytest.approx(0.9, abs=1e-3)
    assert g["A"]["confidence"] == pytest.approx((0.95 * 0.9 + 0.90 * 0.9) / 2, abs=1e-3)  # legacy formula unchanged


async def test_calibrated_probability_replaces_raw_score_in_confidence(client):
    client.app.state.inference = FixedInference(calibrated=True)
    g = {x["label"]: x for x in (await post(client)).json()}
    assert g["B"]["calibrated"] is True and g["B"]["match_probability"] == pytest.approx(0.55)
    assert g["B"]["confidence"] == pytest.approx(0.55 * 0.8, abs=1e-3)  # probability x detector confidence
    assert g["B"]["uncertain_units"] == 1 and "uncertain" in g["B"]["flags"] and "blurry" in g["B"]["flags"]
    assert g["A"]["uncertain_units"] == 0
    assert g[None]["calibrated"] is False  # the unknown group is never presented as calibrated


async def test_live_frames_have_their_own_rate_limit_bucket(client):
    client.app.state.inference = FixedInference(calibrated=False)
    s = client.app.state.settings
    assert s.analyze_live_per_minute > s.analyze_per_minute
    for _ in range(s.analyze_per_minute):
        assert (await post(client)).status_code == 200
    assert (await post(client)).status_code == 429  # single-upload bucket is full ...
    assert (await post(client, {"live": "1"})).status_code == 200  # ... live frames are not throttled by it
