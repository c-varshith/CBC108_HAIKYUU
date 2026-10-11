from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from train.build_detector_dataset import build_dataset, check_dataset, visible_boxes


def product_images(root: Path, n=6):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        im = Image.new("RGB", (80 + i * 3, 120), "white")
        ImageDraw.Draw(im).rounded_rectangle((8, 8, im.width - 9, 111), radius=7, fill=(220 - i * 10, 30 + i * 20, 80 + i * 10))
        im.save(root / f"SKU-{i:02}.jpg")
    return root


def test_small_dataset_has_valid_boxes_deterministic_and_sku_disjoint(tmp_path):
    products = product_images(tmp_path / "products")
    first, second = tmp_path / "first", tmp_path / "second"
    m1 = build_dataset(products, first, n_train=20, n_val=8, val_sku_frac=.34, seed=7, width=256, height=192,
                       eval_dir=tmp_path / "missing-eval")
    build_dataset(products, second, n_train=20, n_val=8, val_sku_frac=.34, seed=7, width=256, height=192,
                  eval_dir=tmp_path / "missing-eval")
    assert m1["sku_split"]["train"] and m1["sku_split"]["val"]
    assert set(m1["sku_split"]["train"]).isdisjoint(m1["sku_split"]["val"])
    assert (first / "MANIFEST.json").read_bytes() == (second / "MANIFEST.json").read_bytes()
    for p in (first / "images").rglob("*.jpg"):
        assert p.read_bytes() == (second / p.relative_to(first)).read_bytes()
    assert not check_dataset(first, tmp_path / "missing-eval")
    for image in (first / "images").rglob("*.jpg"):
        label = first / "labels" / image.parent.name / f"{image.stem}.txt"
        width, height = Image.open(image).size
        for line in label.read_text().splitlines():
            cls, cx, cy, bw, bh = map(float, line.split())
            assert cls == 0 and 0 <= cx <= 1 and 0 <= cy <= 1 and 0 < bw <= 1 and 0 < bh <= 1
            assert (cx - bw / 2) * width >= -1e-3 and (cy - bh / 2) * height >= -1e-3
            assert (cx + bw / 2) * width <= width + 1e-3 and (cy + bh / 2) * height <= height + 1e-3


def test_negative_scene_has_empty_label_file(tmp_path):
    from train.build_detector_dataset import _scene
    image, boxes, _ = _scene(__import__("random").Random(3), {}, "negative", 128, 96)
    assert image.size == (128, 96) and boxes == []


def test_negative_dataset_writes_empty_yolo_labels(tmp_path):
    products = product_images(tmp_path / "products")
    out = tmp_path / "negative-only"
    build_dataset(products, out, n_train=2, n_val=2, seed=5, width=128, height=96,
                  mix={"large": 0, "medium": 0, "dense": 0, "negative": 1}, eval_dir=tmp_path / "missing-eval")
    assert all(not p.read_text().strip() for p in (out / "labels").rglob("*.txt"))


def test_occlusion_rule_drops_item_below_forty_percent_visibility():
    owner = __import__("numpy").full((10, 10), 0, dtype="int32")
    owner[:, :7] = 1
    meta = [(0, "hidden", (0, 0, 10, 10)), (1, "visible", (0, 0, 7, 10))]
    assert visible_boxes(owner, meta) == [{"bbox": [0, 0, 7, 10], "sku": "visible"}]


def test_checker_rejects_validation_sku_in_training_manifest(tmp_path):
    products = product_images(tmp_path / "products")
    out = tmp_path / "generated"
    build_dataset(products, out, n_train=3, n_val=2, seed=6, width=128, height=96, eval_dir=tmp_path / "missing-eval")
    manifest_path = out / "MANIFEST.json"
    manifest = __import__("json").loads(manifest_path.read_text())
    held_out = manifest["sku_split"]["val"][0]
    manifest["items"]["train"][0]["skus"].append(held_out)
    manifest_path.write_text(__import__("json").dumps(manifest))
    assert any("validation SKU(s) present in training" in e for e in check_dataset(out, tmp_path / "missing-eval"))


def test_checker_rejects_frozen_evaluation_image_hash_in_training(tmp_path):
    products = product_images(tmp_path / "products")
    frozen = tmp_path / "eval_set"
    (frozen / "images").mkdir(parents=True)
    eval_image = Image.new("RGB", (96, 64), (12, 34, 56))
    eval_image.save(frozen / "images/eval.jpg")
    out = tmp_path / "generated"
    build_dataset(products, out, n_train=3, n_val=2, seed=4, width=128, height=96, eval_dir=frozen)
    target = next((out / "images/train").glob("*.jpg"))
    target.write_bytes((frozen / "images/eval.jpg").read_bytes())
    assert any("evaluation image hash leaked" in e for e in check_dataset(out, frozen))


def test_builder_refuses_source_packshot_that_is_frozen_eval_image(tmp_path):
    products = product_images(tmp_path / "products")
    frozen = tmp_path / "eval_set"
    (frozen / "images").mkdir(parents=True)
    source = next(products.glob("*.jpg"))
    (frozen / "images" / "copied.jpg").write_bytes(source.read_bytes())
    with pytest.raises(ValueError, match="frozen eval image"):
        build_dataset(products, tmp_path / "out", n_train=2, n_val=1, eval_dir=frozen)
