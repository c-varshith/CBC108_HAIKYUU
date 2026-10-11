import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from eval.phase_a import (
    cmd_report,
    cmd_to_yolo,
    fragment_metrics,
    image_band,
    run_eval,
    whole_item_iou,
)


def test_large_band_uses_median_box_area_at_ten_percent():
    assert image_band([{"bbox": [0, 0, 100, 100]}], 1, 300, 300) == "large"
    assert image_band([{"bbox": [0, 0, 80, 100]}], 1, 300, 300) == "sparse"
    # For an even number of labels the mathematical median averages the middle pair.
    assert image_band([{"bbox": [0, 0, 100, 100]}, {"bbox": [0, 0, 300, 500]}], 2, 1000, 1000) == "sparse"


def test_fragment_metric_counts_prediction_mostly_inside_item_with_low_iou():
    truth = np.array([[0, 0, 100, 100], [120, 0, 220, 100]], np.float32)
    predictions = np.array([[0, 0, 40, 100], [0, 0, 100, 100], [120, 0, 220, 100]], np.float32)
    count, rate = fragment_metrics(predictions, truth)
    assert count == 1 and rate == 1 / 3


def test_whole_item_iou_gives_missed_items_zero():
    truth = np.array([[0, 0, 100, 100], [120, 0, 220, 100]], np.float32)
    predictions = np.array([[0, 0, 100, 100]], np.float32)
    assert whole_item_iou(predictions, truth) == .5


def test_fragment_metric_empty_predictions_has_no_rate():
    assert fragment_metrics(np.zeros((0, 4)), np.array([[0, 0, 10, 10]])) == (0, None)


def test_real_labels_convert_to_yolo_by_capture_group(tmp_path):
    source = tmp_path / "source"; (source / "images").mkdir(parents=True)
    labels = {}
    for i, group in enumerate(("shelf-a", "shelf-a", "shelf-b", "shelf-b")):
        name = f"frame-{i}.jpg"
        Image.new("RGB", (100, 100), (20 + i, 40, 60)).save(source / "images" / name)
        labels[name] = {"capture_group": group, "counts": {"SKU": 1},
                        "boxes": [{"bbox": [10, 10, 90, 90], "sku": "SKU"}]}
    (source / "labels.json").write_text(json.dumps(labels))
    out = tmp_path / "yolo"
    cmd_to_yolo(Namespace(data=source, out=out, val_frac=.5, seed=4, allow_eval_source=False))
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert len(manifest["items"]["train"]) == 2 and len(manifest["items"]["val"]) == 2
    assert set(manifest["group_split"]["train"]).isdisjoint(manifest["group_split"]["val"])
    for label in (out / "labels").rglob("*.txt"):
        cls, cx, cy, w, h = map(float, label.read_text().split())
        assert (cls, cx, cy, w, h) == (0, .5, .5, .8, .8)


def test_frozen_eval_source_refused_without_override():
    import pytest
    from eval.phase_a import cmd_to_yolo
    frozen = Path(__file__).resolve().parents[1] / "eval_set"
    with pytest.raises(SystemExit, match="refusing to convert frozen eval_set"):
        cmd_to_yolo(Namespace(data=frozen, out=Path("unused"), val_frac=.2, seed=1, allow_eval_source=False))


def test_to_yolo_rejects_copied_frozen_eval_image_hash(tmp_path):
    import pytest
    from eval.phase_a import cmd_to_yolo
    frozen = Path(__file__).resolve().parents[1] / "eval_set"
    eval_image = next((frozen / "images").glob("*.jpg"))
    source = tmp_path / "copy-source"; (source / "images").mkdir(parents=True)
    (source / "images" / "copied.jpg").write_bytes(eval_image.read_bytes())
    Image.new("RGB", (80, 60), (1, 2, 3)).save(source / "images" / "other.jpg")
    labels = {"copied.jpg": {"capture_group": "a", "counts": {}, "boxes": []},
              "other.jpg": {"capture_group": "b", "counts": {}, "boxes": []}}
    (source / "labels.json").write_text(json.dumps(labels))
    with pytest.raises(SystemExit, match="evaluation-image hash"):
        cmd_to_yolo(Namespace(data=source, out=tmp_path / "out", val_frac=.5, seed=1, allow_eval_source=False))


def test_run_eval_reports_fragment_and_large_item_metrics(tmp_path):
    data = tmp_path / "eval"
    (data / "images").mkdir(parents=True); (data / "catalog" / "SKU").mkdir(parents=True)
    Image.new("RGB", (100, 100), "white").save(data / "images" / "one.jpg")
    Image.new("RGB", (10, 10), "red").save(data / "catalog" / "SKU" / "ref.jpg")
    (data / "labels.json").write_text(json.dumps({"one.jpg": {"counts": {"SKU": 1}, "boxes": [{"bbox": [0, 0, 100, 100], "sku": "SKU"}]}}))
    (data / "MANIFEST.json").write_text(json.dumps({"synthetic": True}))

    class Pipeline:
        store = SimpleNamespace(skus=lambda: [])
        def add_references(self, *args): pass
        def detect(self, img, k):
            return ([{"bbox": [0, 0, 40, 100], "det_confidence": .8,
                      "matches": [{"sku": "SKU", "score": .9}]}], SimpleNamespace())

    report = run_eval(Pipeline(), data)
    assert report["bands"]["large"]["det_recall"] == 0
    assert report["bands"]["large"]["fragment_rate"] == 1
    assert report["bands"]["large"]["boxes_per_item"] == 1
    assert report["bands"]["large"]["whole_item_iou"] == .4
    assert report["per_image"][0]["fragment_rate"] == 1


def test_report_merges_reports_and_labels_synthetic_results(tmp_path):
    report = {"meta": {"backend": "yolo", "label": "fine-tuned-yolo", "synthetic": True},
              "bands": {b: {"images": 1, "det_recall": .5, "det_precision": .5,
                            "total_count_mae": 1, "boxes_per_item": 2, "fragment_rate": .5,
                            "whole_item_iou": .4 if b == "large" else None} for b in ("sparse", "medium", "dense", "large")},
              "matching": {"top1_acc_on_matched": None, "sure_precision": None, "auto_resolved_pct": None,
                           "review_pct": None, "states": {}},
              "count_mae_per_sku_raw_top1": None, "latency_ms": None, "calibration": {}}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    out = tmp_path / "RESULTS.md"
    cmd_report(Namespace(reports=str(report_path), data="eval_set", bench=str(tmp_path / "missing-bench"), out=str(out)))
    text = out.read_text()
    assert "SYNTHETIC DATA" in text and "large: fragment rate" in text and "fine-tuned-yolo" in text
