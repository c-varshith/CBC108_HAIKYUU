import json
import shutil
from pathlib import Path

from PIL import Image

from train.train_calibrex_detector import make_config, validate_config


def _source(root: Path, prefix: str, groups=("s1", "s2")):
    for split in ("train", "val"):
        (root / "images" / split).mkdir(parents=True)
        (root / "labels" / split).mkdir(parents=True)
        for i in range(2):
            name = f"{prefix}-{split}-{i}.jpg"
            Image.new("RGB", (32, 24), (i * 40, 70, 100)).save(root / "images" / split / name)
            (root / "labels" / split / f"{Path(name).stem}.txt").write_text("0 0.5 0.5 0.5 0.5\n")


def test_make_config_builds_weighted_lists_and_source_validation_files(tmp_path):
    syn, real = tmp_path / "syn", tmp_path / "real"
    _source(syn, "syn"); _source(real, "real")
    config = tmp_path / "calibrex.yaml"
    manifest = make_config(config, None, syn, real, {"synthetic": .7, "real": .3}, samples_per_epoch=20, seed=5)
    assert config.is_file()
    assert manifest["train_images_per_epoch"] == {"synthetic": 14, "real": 6}
    train_rows = Path(manifest["train_image_list"]).read_text().splitlines()
    assert len(train_rows) == 20
    assert len(Path(manifest["source_val_configs"]["synthetic"]).read_text()) > 0
    assert json.loads(config.with_name("calibrex.manifest.json").read_text())["sources"]["real"] == str(real.resolve())
    assert len(validate_config(config)) == 24  # repeated balanced train entries plus four source val images


def test_make_config_rejects_train_image_hashes_found_in_held_out_eval(tmp_path):
    syn, held_out = tmp_path / "syn", tmp_path / "held-out"
    _source(syn, "syn")
    held_out.mkdir()
    shutil.copy2(next((syn / "images" / "train").glob("*.jpg")), held_out / "same-content.jpg")
    try:
        make_config(tmp_path / "blocked.yaml", None, syn, None, eval_data=[held_out])
    except ValueError as exc:
        assert "evaluation set" in str(exc)
    else:
        raise AssertionError("expected the evaluation image hash leak to be rejected")
