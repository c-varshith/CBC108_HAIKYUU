"""Build a source-balanced YOLO data config or fine-tune a retail-item detector.

The `--dry-run` mode has no Ultralytics dependency and never downloads datasets
or weights. Provide a locally obtained SKU-110K directory explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

IMG = {".jpg", ".jpeg", ".png", ".webp"}
FROZEN_EVAL = Path(__file__).resolve().parents[1] / "eval_set"


def _images(root: Path, split: str) -> list[Path]:
    p = root / "images" / split
    if not p.is_dir():
        raise ValueError(f"missing {split} images folder: {p}")
    files = sorted(x.resolve() for x in p.rglob("*") if x.is_file() and x.suffix.lower() in IMG)
    if not files:
        raise ValueError(f"no images under {p}")
    for image in files:
        label = root / "labels" / split / f"{image.stem}.txt"
        if not label.is_file():
            raise ValueError(f"missing YOLO label file {label}")
    return files


def _write_list(path: Path, images: list[Path]) -> None:
    path.write_text("\n".join(str(p) for p in images) + "\n")


def _hashes(root: Path) -> set[str]:
    return {hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMG}


def _yaml_quote(value: str) -> str:
    return json.dumps(value)


def validate_config(path: Path) -> list[Path]:
    """Validate generated/simple YOLO YAML and every referenced image/label without downloads."""
    if not path.is_file():
        raise ValueError(f"data YAML does not exist: {path}")
    fields = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"train", "val"}:
            raw = value.strip()
            try: raw = json.loads(raw)
            except Exception: raw = raw.strip("'\"")
            fields[key.strip()] = raw
    if set(fields) != {"train", "val"}:
        raise ValueError("dry-run requires train and val path entries in the YOLO data YAML")
    images = []
    for split, raw in fields.items():
        source = Path(raw)
        if not source.is_absolute(): source = (path.parent / source).resolve()
        if source.is_dir():
            files = sorted(p.resolve() for p in source.rglob("*") if p.is_file() and p.suffix.lower() in IMG)
        elif source.is_file() and source.suffix.lower() == ".txt":
            files = [Path(x.strip()).resolve() if Path(x.strip()).is_absolute() else (source.parent / x.strip()).resolve()
                     for x in source.read_text().splitlines() if x.strip()]
        else:
            raise ValueError(f"{split} must point to an image directory or image-list .txt: {source}")
        if not files:
            raise ValueError(f"{split} contains no image paths")
        for image in files:
            if not image.is_file(): raise ValueError(f"missing {split} image: {image}")
            label_candidates = [image.parent.parent.parent / "labels" / image.parent.name / f"{image.stem}.txt",
                                image.parent.parent / "labels" / f"{image.stem}.txt"]
            if not any(x.is_file() for x in label_candidates):
                raise ValueError(f"missing YOLO label file for {image}")
        images.extend(files)
    return images


def make_config(out: Path, sku110k: Path | None, synthetic: Path | None, real: Path | None,
                ratios: dict[str, float] | None = None, samples_per_epoch: int = 10000, seed: int = 17,
                eval_data: list[Path] | None = None) -> dict:
    roots = {"sku110k": sku110k, "synthetic": synthetic, "real": real}
    roots = {k: v.resolve() for k, v in roots.items() if v is not None}
    if not roots:
        raise ValueError("provide at least one of --sku110k, --synthetic or --real")
    splits = {name: {"train": _images(root, "train"), "val": _images(root, "val")} for name, root in roots.items()}
    eval_roots = [FROZEN_EVAL, *(p.resolve() for p in (eval_data or []))]
    eval_hashes = set().union(*(_hashes(p) for p in eval_roots if p.is_dir())) if eval_roots else set()
    overlaps = sorted({str(image) for source in splits.values() for image in source["train"]
                       if hashlib.sha256(image.read_bytes()).hexdigest() in eval_hashes})
    if overlaps:
        raise ValueError(f"training images share content hashes with an evaluation set: {overlaps[:5]}")
    weights = ratios or {"sku110k": .50, "synthetic": .35, "real": .15}
    active = {k: max(0.0, float(weights.get(k, 0))) for k in roots}
    if sum(active.values()) <= 0:
        raise ValueError("active source weights must sum to a positive number")
    total = max(samples_per_epoch, len(roots))
    rng = random.Random(seed)
    sources = list(roots)
    portions = {k: active[k] / sum(active.values()) for k in sources}
    # Largest-remainder allocation gives a stable source ratio; sampling with replacement makes
    # the mix independent of the raw source sizes and supports small real pilot sets.
    exact = {k: portions[k] * total for k in sources}
    counts = {k: int(exact[k]) for k in sources}
    for k in sorted(sources, key=lambda x: exact[x] - counts[x], reverse=True)[:total - sum(counts.values())]:
        counts[k] += 1
    train_list = []
    for source in sources:
        pool = splits[source]["train"]
        if counts[source]:
            train_list.extend(rng.choices(pool, k=counts[source]))
    rng.shuffle(train_list)
    val_list = [p for source in sources for p in splits[source]["val"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    train_txt, val_txt = out.with_name(out.stem + ".train.txt"), out.with_name(out.stem + ".val.txt")
    _write_list(train_txt, train_list); _write_list(val_txt, val_list)
    out.write_text(f"train: {_yaml_quote(str(train_txt.resolve()))}\n"
                   f"val: {_yaml_quote(str(val_txt.resolve()))}\n"
                   "nc: 1\nnames: ['retail_item']\n")
    source_configs = {}
    for source in sources:
        src_yaml = out.with_name(f"{out.stem}.{source}.val.yaml")
        src_val = out.with_name(f"{out.stem}.{source}.val.txt")
        _write_list(src_val, splits[source]["val"])
        src_yaml.write_text(f"val: {_yaml_quote(str(src_val.resolve()))}\nnc: 1\nnames: ['retail_item']\n")
        source_configs[source] = str(src_yaml.resolve())
    manifest = {"config": str(out.resolve()), "sources": {k: str(v) for k, v in roots.items()},
                "source_weights": portions, "train_images_per_epoch": counts, "train_image_list": str(train_txt.resolve()),
                "val_image_list": str(val_txt.resolve()), "source_val_configs": source_configs,
                "unique_images": {k: {sp: len(splits[k][sp]) for sp in ("train", "val")} for k in sources},
                "seed": seed, "notes": "Training list sampling repeats paths; source validation files preserve each source independently."}
    out.with_name(out.stem + ".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _parse_ratios(text: str) -> dict[str, float]:
    out = {}
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if not sep or key not in {"sku110k", "synthetic", "real"}:
            raise argparse.ArgumentTypeError("ratios format: sku110k=.5,synthetic=.35,real=.15")
        out[key] = float(value)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sku110k", type=Path, help="local YOLO-format SKU-110K root; does not download")
    p.add_argument("--synthetic", type=Path); p.add_argument("--real", type=Path)
    p.add_argument("--config", "--data", dest="config", type=Path, default=Path("calibrex.yaml"),
                   help="generated YOLO data YAML; use --dry-run to validate source configs without training")
    p.add_argument("--ratios", type=_parse_ratios, default=_parse_ratios("sku110k=.5,synthetic=.35,real=.15"))
    p.add_argument("--samples-per-epoch", type=int, default=10000); p.add_argument("--seed", type=int, default=17)
    p.add_argument("--eval-data", type=Path, action="append", default=[],
                   help="additional held-out evaluation directory for training-image hash protection; repeatable")
    p.add_argument("--model", default="yolo11m.pt"); p.add_argument("--epochs", type=int, default=100)
    p.add_argument("--imgsz", type=int, default=1280); p.add_argument("--batch", type=int, default=8)
    p.add_argument("--device", default="0"); p.add_argument("--resume", action="store_true")
    p.add_argument("--name", default="calibrex-retail-item"); p.add_argument("--project", type=Path, default=Path("runs/detect"))
    p.add_argument("--low-vram", action="store_true", help="use yolo11s, 1024px, batch 4")
    p.add_argument("--dry-run", action="store_true", help="validate config/data only; no model download or training")
    a = p.parse_args()
    if a.low_vram:
        if a.model == "yolo11m.pt": a.model = "yolo11s.pt"
        if a.imgsz == 1280: a.imgsz = 1024
        if a.batch == 8: a.batch = 4
    if a.resume and not Path(a.model).is_file():
        p.error("--resume requires --model to name an existing YOLO checkpoint such as weights/last.pt")
    sources_given = any((a.sku110k, a.synthetic, a.real))
    manifest_path = a.config.with_name(a.config.stem + ".manifest.json")
    if sources_given:
        try:
            manifest = make_config(a.config, a.sku110k, a.synthetic, a.real, a.ratios,
                                   a.samples_per_epoch, a.seed, a.eval_data)
        except Exception as e:
            p.error(str(e))
    elif not a.config.is_file():
        p.error("provide at least one source directory or an existing --data/--config YAML")
    else:
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    if a.dry_run:
        try: checked = validate_config(a.config)
        except Exception as e: p.error(str(e))
        print(f"data config ready: {a.config.resolve()}")
        print(f"dry run: validated {len(checked)} image entries and associated labels; training and downloads were not started")
        if manifest.get("train_images_per_epoch"):
            print("source mix:", json.dumps(manifest["train_images_per_epoch"], sort_keys=True))
        return
    print(f"data config ready: {a.config.resolve()}")
    try:
        from ultralytics import YOLO
    except ImportError as e:
        p.error(f"install ai-worker/requirements.txt on the training host first: {e}")
    if a.resume:
        model = YOLO(a.model)
        model.train(resume=True, device=a.device, project=str(a.project), name=a.name)
    else:
        model = YOLO(a.model)
        model.train(data=str(a.config.resolve()), epochs=a.epochs, imgsz=a.imgsz, batch=a.batch,
                    device=a.device, project=str(a.project), name=a.name, single_cls=True,
                    max_det=1000, mosaic=1.0, close_mosaic=10, scale=.9, flipud=0.0, fliplr=.5,
                    degrees=3.0, perspective=.0005, hsv_h=.015, patience=20, cos_lr=True)
    best = a.project / a.name / "weights" / "best.pt"
    if not best.is_file():
        raise SystemExit(f"training completed without expected best weights: {best}")
    print(f"candidate weights: {best.resolve()}")
    metrics = {}
    validation_sources = manifest.get("source_val_configs") or {"combined": str(a.config.resolve())}
    for source, yaml_path in validation_sources.items():
        val_model = YOLO(str(best))
        result = val_model.val(data=yaml_path, imgsz=a.imgsz, max_det=1000, device=a.device, conf=.001)
        metrics[source] = {"precision": float(result.box.mp), "recall": float(result.box.mr),
                           "map50": float(result.box.map50), "map50_95": float(result.box.map)}
        print(f"{source}: precision={metrics[source]['precision']:.4f} recall={metrics[source]['recall']:.4f} "
              f"mAP50={metrics[source]['map50']:.4f} mAP50-95={metrics[source]['map50_95']:.4f}")
    a.project.mkdir(parents=True, exist_ok=True)
    (a.project / a.name / "source-validation.json").write_text(json.dumps(metrics, indent=2) + "\n")


if __name__ == "__main__":
    main()
