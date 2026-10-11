"""Build a deterministic one-class YOLO dataset from product packshots.

Run from ``ai-worker``::

    python -m train.build_detector_dataset --products /path/to/barcode-images \
        --out /tmp/calibrex-detector --n-train 20 --n-val 8

Validation SKUs are held out from training. The frozen eval image hashes are
checked at build and check time. Generated data is synthetic and is not a
substitute for real shelf evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

IMG = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_EVAL = Path(__file__).resolve().parents[1] / "eval_set"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def eval_hashes(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {sha256(p) for p in (root / "images").glob("*") if p.is_file() and p.suffix.lower() in IMG}


def trim_near_white(im: Image.Image, threshold: int = 247, border_fraction: float = 0.97) -> Image.Image:
    """Trim only near-white outer rows/columns; preserve white package artwork inside."""
    im = im.convert("RGB")
    a = np.asarray(im)
    content = np.min(a, axis=2) < threshold
    h, w = content.shape
    row_has = content.mean(axis=1) > (1.0 - border_fraction)
    col_has = content.mean(axis=0) > (1.0 - border_fraction)
    ys, xs = np.flatnonzero(row_has), np.flatnonzero(col_has)
    if not len(xs) or not len(ys):
        return im
    x1, x2 = int(xs[0]), int(xs[-1]) + 1
    y1, y2 = int(ys[0]), int(ys[-1]) + 1
    # Avoid aggressive trims on products whose art is almost entirely white.
    if (x2 - x1) * (y2 - y1) < 0.1 * w * h:
        return im
    return im.crop((x1, y1, x2, y2))


def _background(rng: random.Random, width: int, height: int, kind: str | None = None) -> Image.Image:
    kind = kind or rng.choice(("plain", "gradient", "noise", "wood", "steel", "shelf"))
    yy, _xx = np.mgrid[0:height, 0:width]
    base = rng.randint(150, 245)
    if kind == "plain":
        arr = np.full((height, width, 3), base, np.uint8)
    elif kind == "gradient":
        g = np.linspace(base - 30, min(255, base + 20), height, dtype=np.float32)[:, None]
        arr = np.repeat(g, width, axis=1)
        arr = np.stack((arr, arr * rng.uniform(.94, 1), arr * rng.uniform(.88, 1)), axis=2).clip(0, 255).astype(np.uint8)
    elif kind == "noise":
        arr = np.clip(base + np.random.default_rng(rng.randrange(2**32)).normal(0, 18, (height, width, 1)), 0, 255)
        arr = np.repeat(arr, 3, axis=2).astype(np.uint8)
    elif kind == "wood":
        grain = 10 * np.sin(yy / rng.uniform(8, 24)) + np.random.default_rng(rng.randrange(2**32)).normal(0, 6, (height, width))
        arr = np.stack((base + grain + 10, base * .76 + grain, base * .52 + grain), axis=2).clip(0, 255).astype(np.uint8)
    else:
        shade = np.full((height, width), base, np.float32)
        if kind == "shelf":
            shelf_h = max(30, height // 8)
            for y in range(shelf_h, height, shelf_h * 3):
                shade[y:min(y + 5, height), :] = max(0, base - 55)
                shade[y + 5:min(y + 10, height), :] = min(255, base + 20)
        else:  # brushed steel
            shade += np.random.default_rng(rng.randrange(2**32)).normal(0, 4, (height, 1))
        arr = np.repeat(shade[:, :, None], 3, axis=2).clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.extend(([x, y, 1, 0, 0, 0, -u * x, -u * y, u],
                     [0, 0, 0, x, y, 1, -v * x, -v * y, v]))
    return np.linalg.solve(np.asarray(rows, np.float64)[:, :8], np.asarray(rows, np.float64)[:, 8]).tolist()


def _jitter_item(im: Image.Image, rng: random.Random, background: tuple[int, int, int]) -> Image.Image:
    im = trim_near_white(im)
    angle = rng.uniform(-5, 5)
    im = im.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=background)
    im = ImageEnhance.Brightness(im).enhance(rng.uniform(.72, 1.25))
    im = ImageEnhance.Contrast(im).enhance(rng.uniform(.78, 1.22))
    if rng.random() < .25:
        im = im.filter(ImageFilter.GaussianBlur(rng.uniform(.2, 1.2)))
    if rng.random() < .18:
        im = ImageEnhance.Color(im).enhance(rng.uniform(.65, 1.35))
    if rng.random() < .25 and min(im.size) >= 20:
        w, h = im.size
        src = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float64)
        jx, jy = w * .035, h * .035
        dst = np.array([[rng.uniform(0, jx), rng.uniform(0, jy)],
                        [w - 1 - rng.uniform(0, jx), rng.uniform(0, jy)],
                        [w - 1 - rng.uniform(0, jx), h - 1 - rng.uniform(0, jy)],
                        [rng.uniform(0, jx), h - 1 - rng.uniform(0, jy)]], dtype=np.float64)
        coeff = _homography(dst, src)  # Pillow samples output coordinates back into the source.
        im = im.transform((w, h), Image.Transform.PERSPECTIVE, coeff, Image.Resampling.BICUBIC, fillcolor=background)
    return im


def _item_size(im: Image.Image, scene: str, rng: random.Random, width: int, height: int) -> tuple[int, int]:
    ratio = im.width / max(im.height, 1)
    if scene == "large":
        min_long_for_area = math.sqrt(.10 * width * height * max(ratio, 1 / ratio))
        long_side = max(rng.uniform(.35, .95) * max(width, height), min_long_for_area)
        max_side = max(width, height) * .99
    elif scene == "medium":
        long_side = rng.uniform(.12, .42) * max(width, height)
        max_side = max(width, height) * .48
    else:
        long_side = rng.uniform(.07, .18) * max(width, height)
        max_side = max(width, height) * .22
    long_side = min(long_side, max_side)
    if ratio >= 1:
        return max(8, int(long_side)), max(8, int(long_side / ratio))
    return max(8, int(long_side * ratio)), max(8, int(long_side))


def _scene(rng: random.Random, pool: dict[str, Image.Image], scene: str, width: int, height: int,
           max_items: int | None = None, forced_skus: list[str] | None = None):
    bg = _background(rng, width, height)
    draw = ImageDraw.Draw(bg)
    # Non-product clutter is deliberately unlabelled in negative scenes.
    if scene == "negative":
        for _ in range(rng.randint(3, 15)):
            x, y = rng.randrange(width), rng.randrange(height)
            rw, rh = rng.randint(10, width // 8), rng.randint(5, height // 8)
            color = tuple(rng.randint(25, 225) for _ in range(3))
            draw.rectangle((x, y, min(width, x + rw), min(height, y + rh)), fill=color)
        return bg, [], []

    if scene == "large":
        count = rng.randint(1, 3)
    elif scene == "medium":
        count = rng.randint(4, 20)
    else:
        count = rng.randint(25, 120)
    if max_items is not None:
        count = min(count, max_items)
    skus = forced_skus or list(pool)
    placements = []
    # Fill a few horizontal facings; large scenes are allowed to crop at the frame edge.
    if scene == "large":
        for i in range(count):
            sku = rng.choice(skus); im = pool[sku]
            iw, ih = _item_size(im, scene, rng, width, height)
            item = _jitter_item(im.resize((iw, ih), Image.Resampling.LANCZOS), rng, tuple(bg.getpixel((width // 2, height // 2))))
            x = rng.randint(-item.width // 3, max(0, width - item.width // 2))
            y = rng.randint(-item.height // 3, max(0, height - item.height // 2))
            placements.append((sku, item, x, y))
    else:
        target_rows = 2 if scene == "medium" else rng.randint(2, 5)
        row_h = max(48, height // target_rows)
        for i in range(count):
            sku = rng.choice(skus); im = pool[sku]
            iw, ih = _item_size(im, scene, rng, width, row_h)
            item = _jitter_item(im.resize((iw, ih), Image.Resampling.LANCZOS), rng, tuple(bg.getpixel((width // 2, height // 2))))
            row = i % target_rows
            # Pack facings left-to-right with controlled touching/overlap.
            x = int((i // target_rows) * width / max(1, math.ceil(count / target_rows)) + rng.uniform(-.02, .02) * width)
            y = int(row * height / target_rows + (height / target_rows - item.height) / 2 + rng.uniform(-.08, .08) * row_h)
            if scene == "dense" and i and rng.random() < .35:
                x -= int(item.width * rng.uniform(0, .08))
            placements.append((sku, item, x, y))
    # Later products occlude earlier ones. The owner map counts visible pixels for the 40% rule.
    owner = np.full((height, width), -1, np.int32)
    meta = []
    for idx, (sku, item, x, y) in enumerate(placements):
        x1, y1, x2, y2 = max(0, x), max(0, y), min(width, x + item.width), min(height, y + item.height)
        if x2 <= x1 or y2 <= y1:
            continue
        bg.paste(item, (x, y))
        owner[y1:y2, x1:x2] = idx
        meta.append((idx, sku, (x1, y1, x2, y2)))
    boxes = visible_boxes(owner, meta)
    # mild blur, compression and glare at scene level
    if rng.random() < .45:
        bg = bg.filter(ImageFilter.GaussianBlur(rng.uniform(.1, .8)))
    if rng.random() < .25:
        overlay = Image.new("RGB", bg.size, (255, 255, 255))
        mask = Image.new("L", bg.size, 0)
        x1, x2 = sorted((rng.randint(-width // 4, width), rng.randint(width // 3, width * 2)))
        y1, y2 = sorted((rng.randint(-height // 4, height), rng.randint(height // 3, height * 2)))
        ImageDraw.Draw(mask).ellipse((x1, y1, x2, y2), fill=rng.randint(15, 65))
        bg = Image.composite(overlay, bg, mask)
    return bg, boxes, [p[0] for p in placements if p[0] in skus]


def _scene_name(rng: random.Random, mix: dict[str, float]) -> str:
    keys, weights = list(mix), list(mix.values())
    return rng.choices(keys, weights=weights, k=1)[0]


def visible_boxes(owner: np.ndarray, meta: list[tuple[int, str, tuple[int, int, int, int]]], threshold: float = .40) -> list[dict]:
    """Keep full clipped boxes for items with enough visible area after later objects occlude them."""
    boxes = []
    for idx, sku, (x1, y1, x2, y2) in meta:
        area = (x2 - x1) * (y2 - y1)
        visible = int(np.count_nonzero(owner[y1:y2, x1:x2] == idx))
        if area and visible / area >= threshold:
            boxes.append({"bbox": [x1, y1, x2, y2], "sku": sku})
    return boxes


def _write_yolo_label(path: Path, boxes: list[dict], width: int, height: int) -> None:
    rows = []
    for b in boxes:
        x1, y1, x2, y2 = b["bbox"]
        x1, x2 = max(0, min(width, x1)), max(0, min(width, x2))
        y1, y2 = max(0, min(height, y1)), max(0, min(height, y2))
        if x2 <= x1 or y2 <= y1:
            continue
        rows.append(f"0 {(x1+x2)/(2*width):.8f} {(y1+y2)/(2*height):.8f} {(x2-x1)/width:.8f} {(y2-y1)/height:.8f}")
    path.write_text("\n".join(rows) + ("\n" if rows else ""))


def check_dataset(root: Path, eval_dir: Path | None = DEFAULT_EVAL) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads((root / "MANIFEST.json").read_text())
    except Exception as e:
        return [f"cannot read MANIFEST.json: {e}"]
    train_skus = set(manifest.get("sku_split", {}).get("train", []))
    val_skus = set(manifest.get("sku_split", {}).get("val", []))
    if train_skus & val_skus:
        errors.append(f"train/val SKU leakage: {sorted(train_skus & val_skus)[:10]}")
    if not val_skus:
        errors.append("validation SKU split is empty")
    for item in manifest.get("items", {}).get("train", []):
        leaked = set(item.get("skus", [])) & val_skus
        if leaked:
            errors.append(f"validation SKU(s) present in training image {item.get('image')}: {sorted(leaked)}")
    eval_set = eval_hashes(eval_dir) if eval_dir else set()
    train_hashes: set[str] = set()
    n_images = Counter()
    for split in ("train", "val"):
        image_root, label_root = root / "images" / split, root / "labels" / split
        images = sorted(p for p in image_root.glob("*") if p.is_file() and p.suffix.lower() in IMG)
        if not images:
            errors.append(f"{split} has no images")
        for image in images:
            label = label_root / f"{image.stem}.txt"
            if not label.is_file():
                errors.append(f"missing label file: {label}"); continue
            if split == "train":
                digest = sha256(image)
                if digest in eval_set:
                    errors.append(f"evaluation image hash leaked into training: {image}")
                if digest in train_hashes:
                    errors.append(f"duplicate training image hash: {image}")
                train_hashes.add(digest)
            try:
                w, h = Image.open(image).size
            except Exception as e:
                errors.append(f"unreadable image {image}: {e}"); continue
            n_images[split] += 1
            for line_no, line in enumerate(label.read_text().splitlines(), 1):
                parts = line.split()
                if len(parts) != 5:
                    errors.append(f"{label}:{line_no}: expected class cx cy w h"); continue
                try:
                    cls, cx, cy, bw, bh = int(parts[0]), *map(float, parts[1:])
                except ValueError:
                    errors.append(f"{label}:{line_no}: invalid numeric label"); continue
                if cls != 0 or bw <= 0 or bh <= 0 or not (0 <= cx <= 1 and 0 <= cy <= 1 and 0 < bw <= 1 and 0 < bh <= 1):
                    errors.append(f"{label}:{line_no}: invalid normalized box {line}")
                if cx - bw / 2 < -1e-5 or cy - bh / 2 < -1e-5 or cx + bw / 2 > 1 + 1e-5 or cy + bh / 2 > 1 + 1e-5:
                    errors.append(f"{label}:{line_no}: box outside image {line} ({w}x{h})")
        expected = manifest.get("images", {}).get(split)
        if expected is not None and n_images[split] != expected:
            errors.append(f"{split} image count mismatch: manifest={expected}, files={n_images[split]} (stale output files may exist)")
    if not manifest.get("synthetic", False):
        errors.append("manifest must mark this generated dataset as synthetic")
    return errors


def build_dataset(products: Path, out: Path, n_train: int = 1000, n_val: int = 200, val_sku_frac: float = .2,
                  seed: int = 1, width: int = 1280, height: int = 720,
                  mix: dict[str, float] | None = None, eval_dir: Path | None = DEFAULT_EVAL) -> dict:
    if n_train < 1 or n_val < 1 or not 0 < val_sku_frac < 1:
        raise ValueError("n_train/n_val must be positive and val_sku_frac must be between 0 and 1")
    files = sorted(p for p in products.iterdir() if p.is_file() and p.suffix.lower() in IMG)
    if len(files) < 2:
        raise ValueError(f"need at least 2 product images, found {len(files)} in {products}")
    frozen_hashes = eval_hashes(eval_dir) if eval_dir else set()
    same = [p for p in files if sha256(p) in frozen_hashes]
    if same:
        raise ValueError(f"product source contains frozen eval image(s): {same[:3]}")
    rng = random.Random(seed)
    skus = [p.stem for p in files]
    rng.shuffle(skus)
    val_n = min(len(skus) - 1, max(1, round(len(skus) * val_sku_frac)))
    val_skus, train_skus = sorted(skus[:val_n]), sorted(skus[val_n:])
    pool = {p.stem: trim_near_white(Image.open(p).convert("RGB")) for p in files}
    scene_mix = mix or {"large": .25, "medium": .30, "dense": .35, "negative": .10}
    if set(scene_mix) != {"large", "medium", "dense", "negative"} or any(v < 0 for v in scene_mix.values()) or sum(scene_mix.values()) <= 0:
        raise ValueError("scene mix must define non-negative large, medium, dense and negative weights")
    out.mkdir(parents=True, exist_ok=True)
    metadata = {"train": [], "val": []}
    counts = {"train": Counter(), "val": Counter()}
    for split, count, split_skus in (("train", n_train, train_skus), ("val", n_val, val_skus)):
        (out / "images" / split).mkdir(parents=True, exist_ok=True)
        (out / "labels" / split).mkdir(parents=True, exist_ok=True)
        for idx in range(count):
            scene = _scene_name(rng, scene_mix)
            image, boxes, _ = _scene(rng, pool, scene, width, height, forced_skus=split_skus)
            stem = f"{split}_{idx:06d}"
            image_path = out / "images" / split / f"{stem}.jpg"
            label_path = out / "labels" / split / f"{stem}.txt"
            image.save(image_path, "JPEG", quality=rng.randint(82, 96), optimize=True)
            _write_yolo_label(label_path, boxes, width, height)
            counts[split][scene] += 1
            metadata[split].append({"image": str(image_path.relative_to(out)), "scene": scene,
                                    "skus": sorted({b["sku"] for b in boxes})})
    manifest = {"synthetic": True, "generator": "train.build_detector_dataset", "seed": seed,
                "images": {"train": n_train, "val": n_val}, "scene_counts": {k: dict(v) for k, v in counts.items()},
                "sku_split": {"train": train_skus, "val": val_skus}, "val_sku_frac": val_sku_frac,
                "scene_mix": scene_mix, "size": [width, height], "items": metadata,
                "note": "Synthetic training data only; never use synthetic metrics as real-shelf accuracy."}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    errors = check_dataset(out, eval_dir)
    if errors:
        raise ValueError("generated dataset failed validation:\n- " + "\n- ".join(errors[:20]))
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--products", type=Path, help="folder of <SKU>.(jpg|png|webp) product photos")
    p.add_argument("--out", type=Path, default=Path("detector_synthetic"))
    p.add_argument("--n-train", type=int, default=1000); p.add_argument("--n-val", type=int, default=200)
    p.add_argument("--val-sku-frac", type=float, default=.2); p.add_argument("--seed", type=int, default=1)
    p.add_argument("--width", type=int, default=1280); p.add_argument("--height", type=int, default=720)
    p.add_argument("--large-frac", type=float, default=.25); p.add_argument("--medium-frac", type=float, default=.30)
    p.add_argument("--dense-frac", type=float, default=.35); p.add_argument("--negative-frac", type=float, default=.10)
    p.add_argument("--eval-data", type=Path, default=DEFAULT_EVAL)
    p.add_argument("--check", type=Path, help="validate an existing generated dataset and exit")
    a = p.parse_args()
    if a.check:
        errors = check_dataset(a.check, a.eval_data)
        if errors:
            print("FAIL:\n- " + "\n- ".join(errors)); raise SystemExit(1)
        print("OK: dataset labels, splits and evaluation-image leak checks passed")
        return
    if not a.products:
        p.error("--products is required unless --check is used")
    mix = {"large": a.large_frac, "medium": a.medium_frac, "dense": a.dense_frac, "negative": a.negative_frac}
    try:
        m = build_dataset(a.products, a.out, a.n_train, a.n_val, a.val_sku_frac, a.seed, a.width, a.height, mix, a.eval_data)
    except Exception as e:
        p.error(str(e))
    print(f"wrote synthetic one-class detector dataset: {m['images']} images -> {a.out.resolve()}")
    print(f"SKU split: {len(m['sku_split']['train'])} train / {len(m['sku_split']['val'])} held-out val")


if __name__ == "__main__":
    main()
