import numpy as np
import pytest
from PIL import Image

from app.config import Settings
from app.detector import H5RetinaNetDetector, build_detector, decode_retinanet, retinanet_anchors


def test_default_retinanet_anchors_match_p3_to_p7_shapes():
    # Four locations on P3, then one location on each of P4-P7; nine anchors per location.
    anchors = retinanet_anchors(16, 16)
    assert anchors.shape == (72, 4)
    np.testing.assert_allclose(anchors[0], [-18.6274, -7.3137, 26.6274, 15.3137], atol=1e-3)


def test_decode_retinanet_uses_class_score_and_maps_boxes_back_to_original():
    anchors = np.array([[0, 0, 10, 20], [10, 10, 30, 30]], dtype=np.float32)
    regression = np.zeros((2, 4), dtype=np.float32)
    classes = np.array([[0.9], [0.1]], dtype=np.float32)
    boxes, scores = decode_retinanet(regression, classes, anchors, 20, 20, 2.0, 0.25, 0.5, 10)
    np.testing.assert_allclose(boxes, [[0, 0, 5, 10]])
    np.testing.assert_allclose(scores, [0.9])


class FakeH5Model:
    def __init__(self):
        self.input = None

    def __call__(self, batch, training=False):
        self.input = np.asarray(batch)
        height, width = self.input.shape[1:3]
        anchors = retinanet_anchors(height, width)
        regression = np.zeros((1, len(anchors), 4), dtype=np.float32)
        classification = np.full((1, len(anchors), 1), 0.01, dtype=np.float32)
        classification[0, 0, 0] = 0.95
        iou = np.full((1, len(anchors), 1), 0.5, dtype=np.float32)
        return regression, classification, iou


def test_h5_detector_preprocesses_and_returns_detector_protocol_outputs():
    model = FakeH5Model()
    settings = Settings(h5_min_side=24, h5_max_side=64, det_conf=0.25, det_max=20,
                        tile_trigger_px=1000, h5_model_path="unused.h5")
    detector = H5RetinaNetDetector(settings, "cpu", model=model)
    image = Image.new("RGB", (48, 32), (10, 20, 30))
    boxes, scores = detector.detect(image)

    assert model.input.shape == (1, 24, 36, 3)
    np.testing.assert_allclose(model.input[0, 0, 0], [-73.939, -96.779, -113.68], atol=1e-3)
    assert boxes.shape == (1, 4) and scores.shape == (1,)
    assert np.all(boxes[0, [0, 2]] <= 48) and np.all(boxes[0, [1, 3]] <= 32)
    assert scores[0] == pytest.approx(0.95)


def test_h5_failure_does_not_silently_select_yolo(monkeypatch):
    class MissingH5:
        def __init__(self, *_args):
            raise RuntimeError("no TensorFlow")

    monkeypatch.setattr("app.detector.H5RetinaNetDetector", MissingH5)
    settings = Settings(detector_backend="h5")
    with pytest.raises(RuntimeError, match="no TensorFlow"):
        build_detector(settings, "cpu")


def test_finetuned_calibrex_yolo_is_the_default_detector(monkeypatch):
    monkeypatch.delenv("DETECTOR_BACKEND", raising=False)
    monkeypatch.delenv("YOLO_WEIGHTS", raising=False)
    settings = Settings()
    assert settings.detector_backend == "yolo"
    assert settings.yolo_weights.endswith("calibrex_s960/weights/best.pt")


def test_yolo_loads_the_configured_checkpoint_without_substitution(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    checkpoint = tmp_path / "best.pt"
    checkpoint.write_bytes(b"checkpoint")
    loaded = []

    class FakeYOLO:
        def __init__(self, path):
            loaded.append(path)

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=FakeYOLO))
    settings = Settings(detector_backend="yolo", yolo_weights=str(checkpoint))
    build_detector(settings, "cpu")
    assert loaded == [str(checkpoint)]


def test_unloadable_yolo_checkpoint_fails_clearly_without_substitution(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    checkpoint = tmp_path / "invalid.pt"
    checkpoint.write_bytes(b"not a model")

    class InvalidYOLO:
        def __init__(self, _path):
            raise ValueError("invalid checkpoint")

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=InvalidYOLO))
    settings = Settings(detector_backend="yolo", yolo_weights=str(checkpoint))
    with pytest.raises(RuntimeError, match="Could not load configured YOLO detector weights"):
        build_detector(settings, "cpu")


def test_missing_yolo_weights_fail_without_generic_fallback(tmp_path):
    settings = Settings(detector_backend="yolo", yolo_weights=str(tmp_path / "missing.pt"))
    with pytest.raises(FileNotFoundError, match="YOLO detector weights are missing or empty"):
        build_detector(settings, "cpu")


@pytest.mark.parametrize("empty", [False, True])
def test_missing_or_empty_h5_model_fails_pipeline_startup(tmp_path, empty):
    from app.main import build_pipeline

    model = tmp_path / "model.h5"
    if empty:
        model.touch()
    settings = Settings(detector_backend="h5", h5_model_path=str(model))
    with pytest.raises(RuntimeError, match="missing or empty"):
        build_pipeline(settings)
