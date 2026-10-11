"""CALIBREX Phase A: evidence tooling in one file (run from ai-worker/).

  python -m eval.phase_a build-synthetic --products DIR --out eval_set      # offline stand-in set (NOT real evidence)
  python -m eval.phase_a convert --coco export.json --images DIR --catalog DIR --out real_eval_set   # labels (CVAT / Label Studio -> COCO)
  python -m eval.phase_a to-yolo --data real_train_source --out detector_real
  python -m eval.phase_a check  --data eval_set                              # lint the set, show density bands
  python -m eval.phase_a run    --data real_eval_set --backend h5 --out report-h5.json --calibration-out /models/calibration.json
  python -m eval.phase_a run    --data eval_set --backend yolo --out report-yolo.json
  python -m eval.phase_a bench  [--out bench_search.txt]                     # Qdrant speed at 100 / 1k / 10k SKUs
  python -m eval.phase_a report --data eval_set --out ../docs/RESULTS.md     # merges report-*.json + bench into one table

labels.json layout is the one eval/evaluate.py already reads: {"img.jpg": {"counts": {SKU: n}, "boxes": [{"bbox": [x1,y1,x2,y2], "sku": SKU}]}}.
Labelling: draw one box per visible item and set the class name to the product's SKU (barcode); export as COCO (CVAT: "COCO 1.0",
Label Studio: "COCO"). File names in the export must match the files in --images. Include a `capture_group` (shelf or capture session)
for each image before fitting calibration. Calibration is refused for synthetic data and uses disjoint capture groups for held-out ECE.
Density bands use large-item area first (median GT box >=10% image area), then item count: sparse 1-10, medium 11-40, dense 41+.
Synthetic sets are flagged in MANIFEST.json and in the report:
shelf art == reference art, so they only prove the plumbing works and give optimistic numbers; decide the detector and thresholds on REAL photos only."""
from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import random
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

IMG = (".jpg", ".jpeg", ".png", ".webp")
BANDS = (("sparse", 1, 10), ("medium", 11, 40), ("dense", 41, 10**9), ("large", 1, 10**9))
COUNT_BANDS = BANDS[:3]
TARGETS = {"large_recall": 0.90, "fragment_rate": 0.10, "sure_precision": 0.98}


def band_of(n: int) -> str:
    """Existing count band; kept for compatibility with older evaluation callers."""
    return next(b for b, lo, hi in COUNT_BANDS if lo <= n <= hi)


def image_band(boxes: list[dict], n_items: int, width: int, height: int) -> str:
    if boxes:
        areas = sorted(max(0.0, (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])) / max(width * height, 1)
                       for b in boxes)
        median = statistics.median(areas)
        if median >= .10:
            return "large"
    return band_of(max(n_items, 1))


def fragment_metrics(pred: np.ndarray, truth: np.ndarray, inside_thr: float = .80, iou_thr: float = .50) -> tuple[int, float | None]:
    """Count predictions mostly inside a truth item but with too little IoU to be a whole-item box."""
    pred = np.asarray(pred, np.float32).reshape(-1, 4)
    truth = np.asarray(truth, np.float32).reshape(-1, 4)
    if not len(pred):
        return 0, None
    if not len(truth):
        return 0, 0.0
    from app.geometry import iou_matrix
    iou = iou_matrix(pred, truth)
    pa = np.maximum(pred[:, 2] - pred[:, 0], 0) * np.maximum(pred[:, 3] - pred[:, 1], 0)
    count = 0
    for i, box in enumerate(pred):
        if pa[i] <= 0:
            continue
        inter = np.maximum(0, np.minimum(box[2], truth[:, 2]) - np.maximum(box[0], truth[:, 0])) * \
                np.maximum(0, np.minimum(box[3], truth[:, 3]) - np.maximum(box[1], truth[:, 1]))
        if np.any((inter / max(float(pa[i]), 1e-9) >= inside_thr) & (iou[i] < iou_thr)):
            count += 1
    return count, count / len(pred)


def whole_item_iou(pred: np.ndarray, truth: np.ndarray) -> float | None:
    """Mean best IoU for each labelled physical item; missed items contribute zero."""
    pred = np.asarray(pred, np.float32).reshape(-1, 4)
    truth = np.asarray(truth, np.float32).reshape(-1, 4)
    if not len(truth):
        return None
    if not len(pred):
        return 0.0
    from app.geometry import iou_matrix
    return float(iou_matrix(pred, truth).max(axis=0).mean())


# ------------------------------------------------------------------ synthetic set
def _degrade(im: Image.Image, rng: random.Random) -> Image.Image:
    from PIL import ImageEnhance, ImageFilter
    im = ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(rng.uniform(0.75, 1.2))).enhance(rng.uniform(0.85, 1.15))
    small = im.resize((max(1, im.width // 2), max(1, im.height // 2)))
    im = small.resize(im.size).filter(ImageFilter.GaussianBlur(rng.uniform(0, 1.2)))
    if rng.random() < 0.15:  # glare blob
        yy, xx = np.mgrid[0:im.height, 0:im.width]
        cx, cy, sg = rng.uniform(0, im.width), rng.uniform(0, im.height), max(im.width, im.height) * rng.uniform(0.12, 0.3)
        mask = Image.fromarray((np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sg ** 2)) * 170).astype(np.uint8), "L")
        im = Image.composite(Image.new("RGB", im.size, (255, 255, 255)), im, mask)
    return im


def _compose(rng, pool, n, h, gap, overlap, width, path):
    runs, left = [], n
    skus = rng.sample(list(pool), min(len(pool), max(2, n // 4)))
    for i, s in enumerate(skus):  # facings: runs of the same product side by side
        k = left if i == len(skus) - 1 else rng.randint(1, max(1, left - (len(skus) - i - 1)))
        runs += [(s, k)]; left -= k
        if left <= 0:
            break
    rng.shuffle(runs)
    items, m = [], 30
    for sku, k in runs:
        for _ in range(k):
            im = Image.open(pool[sku]).convert("RGB")
            ih = int(h * rng.uniform(0.9, 1.1))
            items.append((sku, _degrade(im.resize((max(8, int(im.width * ih / im.height)), ih)), rng)))
    x, row, placed = m, 0, []
    for sku, im in items:
        if x + im.width > width - m and x > m:
            row, x = row + 1, m
        placed.append((sku, im, x, row)); x += im.width - int(im.width * overlap * rng.random()) + gap
    shelf, rh = 26, int(h * 1.15)
    canvas = Image.new("RGB", (width, (row + 1) * (rh + shelf) + 20), (232, 232, 232))
    from PIL import ImageDraw
    d, boxes, counts = ImageDraw.Draw(canvas), [], Counter()
    for r in range(row + 1):
        d.rectangle([0, 20 + r * (rh + shelf) + rh, width, 20 + r * (rh + shelf) + rh + shelf], fill=(150, 110, 70))
    for sku, im, x, r in placed:
        base = 20 + r * (rh + shelf) + rh
        canvas.paste(im, (x, base - im.height))
        boxes.append({"bbox": [x, base - im.height, x + im.width, base], "sku": sku}); counts[sku] += 1
    canvas.save(path, quality=92)
    return {"counts": dict(counts), "boxes": boxes}


def cmd_build(a):
    rng = random.Random(a.seed)
    files = sorted(p for p in Path(a.products).iterdir() if p.suffix.lower() in IMG)
    if len(files) < max(a.skus, 2):
        raise SystemExit(f"need at least {max(a.skus, 2)} product images in {a.products}, found {len(files)}")
    pool = {f.stem: f for f in rng.sample(files, a.skus)}
    out = Path(a.out); (out / "images").mkdir(parents=True, exist_ok=True)
    for sku, f in pool.items():
        (out / "catalog" / sku).mkdir(parents=True, exist_ok=True)
        Image.open(f).convert("RGB").save(out / "catalog" / sku / "ref1.jpg", quality=92)
    spec = {"sparse": (360, 14, 0.0, (3, 10)), "medium": (220, 6, 0.0, (15, 40)), "dense": (120, 1, 0.08, (45, 90))}
    labels = {}
    for band, (h, gap, ov, (lo, hi)) in spec.items():
        for i in range(a.shelves):
            name = f"{band}_{i + 1:02d}.jpg"
            labels[name] = _compose(rng, pool, rng.randint(lo, hi), h, gap, ov, a.width, out / "images" / name)
    (out / "labels.json").write_text(json.dumps(labels, indent=1))
    (out / "MANIFEST.json").write_text(json.dumps({"synthetic": True, "seed": a.seed, "skus": a.skus, "images": len(labels),
        "note": "Shelf art equals reference art; boxes are exact. Optimistic: validates the pipeline, not real-world accuracy."}, indent=1))
    print(f"synthetic set: {len(labels)} shelves, {a.skus} SKUs -> {out}  (flagged synthetic)")


# ------------------------------------------------------------------ real labels
def cmd_convert(a):
    coco = json.loads(Path(a.coco).read_text())
    cats = {c["id"]: c["name"] for c in coco["categories"]}
    image_records = {i["id"]: i for i in coco["images"]}
    imgs = {image_id: Path(i["file_name"]).name for image_id, i in image_records.items()}
    out = Path(a.out); (out / "images").mkdir(parents=True, exist_ok=True)
    labels = {}
    for image_id, name in imgs.items():
        src = Path(a.images) / name
        if src.exists():
            shutil.copy2(src, out / "images" / name); labels[name] = {"counts": {}, "boxes": []}
            meta = image_records[image_id]
            group = meta.get("capture_group") or meta.get("shelf_id") or meta.get("session_id")
            if group:
                labels[name]["capture_group"] = str(group)
        else:
            print("  missing image, skipped:", name)
    for an in coco["annotations"]:
        name, sku = imgs[an["image_id"]], cats[an["category_id"]]
        if name in labels and sku not in a.skip:
            x, y, w, h = an["bbox"]
            labels[name]["boxes"].append({"bbox": [round(x, 1), round(y, 1), round(x + w, 1), round(y + h, 1)], "sku": sku})
    for v in labels.values():
        v["counts"] = dict(Counter(b["sku"] for b in v["boxes"]))
    if a.catalog:
        for d in Path(a.catalog).iterdir():
            if d.is_dir():
                shutil.copytree(d, out / "catalog" / d.name, dirs_exist_ok=True)
    (out / "labels.json").write_text(json.dumps(labels, indent=1))
    (out / "MANIFEST.json").write_text(json.dumps({"synthetic": False, "source": str(a.coco), "images": len(labels)}, indent=1))
    print(f"{len(labels)} images, {sum(len(v['boxes']) for v in labels.values())} boxes -> {out}")
    cmd_check(argparse.Namespace(data=a.out, allow_missing_catalog=getattr(a, "allow_missing_catalog", False)))


def _image_hash(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _labels_yolo(boxes: list[dict], width: int, height: int) -> str:
    rows = []
    for b in boxes:
        x1, y1, x2, y2 = map(float, b["bbox"])
        x1, x2 = max(0, min(width, x1)), max(0, min(width, x2))
        y1, y2 = max(0, min(height, y1)), max(0, min(height, y2))
        if x2 <= x1 or y2 <= y1:
            raise ValueError(f"degenerate bbox {b['bbox']}")
        rows.append(f"0 {(x1+x2)/(2*width):.8f} {(y1+y2)/(2*height):.8f} {(x2-x1)/width:.8f} {(y2-y1)/height:.8f}")
    return "\n".join(rows) + ("\n" if rows else "")


def cmd_to_yolo(a):
    root = Path(a.data).resolve()
    if not 0 < a.val_frac < 1:
        raise SystemExit("--val-frac must be strictly between 0 and 1")
    frozen = (Path(__file__).resolve().parents[1] / "eval_set").resolve()
    if root == frozen and not a.allow_eval_source:
        raise SystemExit("refusing to convert frozen eval_set into training data; pass --allow-eval-source only for an explicit override")
    if root == frozen:
        print("\n!!! WARNING: EXPLICITLY USING THE FROZEN EVALUATION SET AS TRAINING INPUT !!!\n", file=sys.stderr)
    labels_path = root / "labels.json"
    labels = json.loads(labels_path.read_text())
    image_root = root / "images"
    names, groups = [], {}
    eval_roots = [frozen] + [Path(x).resolve() for x in getattr(a, "eval_data", [])]
    eval_images = {_image_hash(p) for eroot in eval_roots for p in (eroot / "images").glob("*") if p.is_file()}
    explicit_eval_hash_seen = False
    for name, lab in labels.items():
        src = image_root / name
        if not src.is_file():
            raise SystemExit(f"source image is missing: {src}")
        if _image_hash(src) in eval_images:
            if not a.allow_eval_source:
                raise SystemExit(f"refusing evaluation-image hash in training input: {src}")
            explicit_eval_hash_seen = True
        group = lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id")
        if not group:
            raise SystemExit(f"{name}: add capture_group/shelf_id/session_id before group-splitting real images")
        names.append(name); groups[name] = str(group)
    if explicit_eval_hash_seen:
        print("\n!!! WARNING: EVALUATION IMAGE HASHES ARE BEING CONVERTED INTO TRAINING DATA !!!\n", file=sys.stderr)
    unique = sorted(set(groups.values()))
    if len(unique) < 2:
        raise SystemExit("to-yolo needs at least two capture groups for train/validation separation")
    rng = random.Random(a.seed); rng.shuffle(unique)
    n_val = min(len(unique) - 1, max(1, round(len(unique) * a.val_frac)))
    val_groups, train_groups = set(unique[:n_val]), set(unique[n_val:])
    if train_groups & val_groups:
        raise SystemExit("capture-group leakage in split")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rows = {"train": [], "val": []}
    for i, name in enumerate(sorted(names)):
        lab = labels[name]; split = "val" if groups[name] in val_groups else "train"
        src = image_root / name
        stem = f"{i:06d}_{Path(name).stem}"
        dst = out / "images" / split / f"{stem}{src.suffix.lower()}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        label_dst = out / "labels" / split / f"{stem}.txt"
        label_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        w, h = Image.open(src).size
        boxes = lab.get("boxes") or []
        label_dst.write_text(_labels_yolo(boxes, w, h))
        rows[split].append({"image": str(dst.relative_to(out)), "capture_group": groups[name], "boxes": len(boxes)})
    manifest = {"synthetic": False, "source": str(root), "images": {k: len(v) for k, v in rows.items()},
                "group_split": {"train": sorted(train_groups), "val": sorted(val_groups)}, "seed": a.seed,
                "items": rows, "allow_eval_source": bool(a.allow_eval_source)}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    leaked = [r["image"] for r in rows["train"] if _image_hash(out / r["image"]) in eval_images]
    if leaked and not a.allow_eval_source:
        raise SystemExit(f"evaluation image hash leaked to training: {leaked[:3]}")
    print(f"wrote YOLO data: {len(rows['train'])} train / {len(rows['val'])} val images; groups disjoint -> {out}")


def cmd_check(a) -> int:
    root, errs, warns = Path(a.data), [], []
    try:
        labels = json.loads((root / "labels.json").read_text())
    except Exception as e:
        print("cannot read labels.json:", e); return 1
    mf = root / "MANIFEST.json"
    manifest = json.loads(mf.read_text()) if mf.exists() else {}
    synth = manifest.get("synthetic") is True
    cat = {d.name for d in (root / "catalog").glob("*") if d.is_dir() and any(f.suffix.lower() in IMG for f in d.iterdir())}
    bands, seen, nbox = Counter(), set(), 0
    for name, lab in labels.items():
        p = root / "images" / name
        if not p.exists():
            errs.append(f"{name}: image file missing"); continue
        w, h = Image.open(p).size
        boxes = lab.get("boxes") or []
        nbox += len(boxes); bands[image_band(boxes, sum(lab["counts"].values()), w, h)] += 1
        if boxes and Counter(b["sku"] for b in boxes) != Counter(lab["counts"]):
            errs.append(f"{name}: counts do not match boxes")
        if not boxes:
            warns.append(f"{name}: no boxes (counts only; detection P/R and calibration skip it)")
        if not synth and not (lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id")):
            warns.append(f"{name}: no shelf/session capture group; excluded from calibration")
        for b in boxes:
            x1, y1, x2, y2 = b["bbox"]; seen.add(b["sku"])
            if x2 - x1 < 2 or y2 - y1 < 2 or x1 < -1 or y1 < -1 or x2 > w + 1 or y2 > h + 1:
                errs.append(f"{name}: box {b['bbox']} outside {w}x{h} or degenerate")
        seen |= set(lab["counts"])
    for s in sorted(seen - cat) if not getattr(a, "allow_missing_catalog", False) else []:
        errs.append(f"SKU {s}: labelled on a shelf but has no catalog/{s}/ reference photo")
    if len(labels) < 40: warns.append(f"only {len(labels)} images (target 40+)")
    if nbox < 50: warns.append(f"only {nbox} labelled boxes; calibration needs >= 50 matched boxes")
    for b, _, _ in COUNT_BANDS:
        if bands[b] < 5: warns.append(f"band '{b}' has {bands[b]} images (aim for 10+)")
    if bands["large"] < 10:
        warns.append(f"large-item band has {bands['large']} images (need 10+ real phone close-ups with single/few packs)")
    print(f"images {len(labels)} | boxes {nbox} | SKUs on shelves {len(seen)} | catalog SKUs {len(cat)} | bands {dict(bands)} | synthetic={manifest.get('synthetic')}")
    for m in errs[:40]: print("ERROR  ", m)
    for m in warns[:40]: print("warning", m)
    if len(warns) > 40: print(f"warning ... {len(warns) - 40} additional warnings")
    print("RESULT:", "FAIL" if errs else "OK")
    return 1 if errs else 0


# ------------------------------------------------------------------ run
def _register(p, root: Path):
    t0 = time.perf_counter()
    for d in sorted((root / "catalog").iterdir()):
        if d.is_dir():
            items = [(f.stem, Image.open(f).convert("RGB")) for f in sorted(d.glob("*")) if f.suffix.lower() in IMG]
            if items: p.add_references(d.name, d.name, items)
    print(f"registered {len(p.store.skus())} SKUs in {time.perf_counter() - t0:.1f}s (no training)")


def _pct(x): return None if x is None else round(100 * x, 1)
def _div(a, b): return round(a / b, 4) if b else None


def run_eval(p, root: Path, sure=0.85, check=0.60, margin=0.08, iou_thr=0.5, calibration_out=None) -> dict:
    """p: any object with .detect(img, k) -> (dets, timings), .add_references, .store (the worker Pipeline)."""
    from app.geometry import iou_matrix
    from app.pipeline import load_image
    from eval.evaluate import calibrate, classify
    _register(p, root)
    labels = json.loads((root / "labels.json").read_text())
    B = {b: Counter() for b, _, _ in BANDS}; cnt_err = {b: [] for b, _, _ in BANDS}; cnt_pct = {b: [] for b, _, _ in BANDS}
    sku_mae, lat, per_img, states = [], [], [], Counter()
    sure_ok = sure_n = top1_ok = top1_n = 0
    cs, cc, ci = [], [], []
    mf = root / "MANIFEST.json"
    manifest = json.loads(mf.read_text()) if mf.exists() else {}
    capture_groups = manifest.get("capture_groups", {})
    for ii, (name, lab) in enumerate(labels.items()):
        img = load_image((root / "images" / name).read_bytes())
        t0 = time.perf_counter(); dets, _tm = p.detect(img, 5); lat.append((time.perf_counter() - t0) * 1000)
        truth = Counter(lab["counts"]); n_true = sum(truth.values())
        gt_boxes = lab.get("boxes") or []
        band = image_band(gt_boxes, n_true, img.width, img.height)
        raw = Counter(d["matches"][0]["sku"] for d in dets if d["matches"])
        sku_mae += [abs(raw[k] - truth[k]) for k in set(truth) | set(raw)]
        err = len(dets) - n_true; cnt_err[band].append(abs(err)); cnt_pct[band].append(abs(err) / max(n_true, 1))
        row = {"image": name, "band": band, "true": n_true, "pred": len(dets), "err": err,
               "boxes_per_item": _div(len(dets), n_true), "latency_ms": round(lat[-1])}
        B[band]["images"] += 1
        B[band]["items"] += n_true
        B[band]["box_pred"] += len(dets)
        boxes = gt_boxes
        if boxes:
            gt = np.array([b["bbox"] for b in boxes], np.float32); gs = [b["sku"] for b in boxes]
            pr = np.array([d["bbox"] for d in dets], np.float32).reshape(-1, 4)
            iou = iou_matrix(pr, gt); used, tp = set(), 0
            fragments, fragment_rate = fragment_metrics(pr, gt)
            B[band]["fragments"] += fragments; B[band]["fragment_preds"] += len(pr)
            best_iou = iou.max(axis=0) if len(pr) else np.zeros(len(gt), np.float32)
            if band == "large":
                B[band]["whole_iou_sum"] += float(best_iou.sum()); B[band]["whole_iou_n"] += len(gt)
            row["fragment_rate"] = fragment_rate
            row["whole_item_iou"] = round(float(best_iou.mean()), 4) if band == "large" and len(best_iou) else (0.0 if band == "large" else None)
            for i in sorted(range(len(dets)), key=lambda k: -dets[k]["det_confidence"]):
                c = [(iou[i, j], j) for j in range(len(gt)) if j not in used and iou[i, j] >= iou_thr]
                if not c: continue
                j = max(c)[1]; used.add(j); tp += 1
                m = dets[i]["matches"]; st = classify(m, sure, check, margin); states[st] += 1
                ok = bool(m) and m[0]["sku"] == gs[j]
                top1_n += 1; top1_ok += ok
                if st == "SURE": sure_n += 1; sure_ok += ok
                group = lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id") or capture_groups.get(name)
                if m and group: cs.append(m[0]["score"]); cc.append(float(ok)); ci.append(str(group))
            B[band].update(tp=tp, pred=len(dets), gt=len(gt)); row.update(tp=tp, recall=_div(tp, len(gt)), precision=_div(tp, len(dets)))
        else:
            B[band]["pred"] += len(dets)
            row["fragment_rate"] = None; row["whole_item_iou"] = None
        per_img.append(row)
    bands = {b: {"images": B[b]["images"], "det_recall": _div(B[b]["tp"], B[b]["gt"]), "det_precision": _div(B[b]["tp"], B[b]["pred"]),
                 "total_count_mae": round(float(np.mean(cnt_err[b])), 2) if cnt_err[b] else None,
                 "total_count_mape_pct": _pct(float(np.mean(cnt_pct[b]))) if cnt_pct[b] else None,
                 "boxes_per_item": _div(B[b]["box_pred"], B[b]["items"]),
                 "fragment_rate": _div(B[b]["fragments"], B[b]["fragment_preds"]),
                 "whole_item_iou": _div(B[b]["whole_iou_sum"], B[b]["whole_iou_n"]) if b == "large" else None} for b, _, _ in BANDS}
    n = sum(states.values())
    return {"bands": bands,
            "matching": {"matched_boxes": top1_n, "top1_acc_on_matched": _div(top1_ok, top1_n), "sure_precision": _div(sure_ok, sure_n),
                         "auto_resolved_pct": _pct(_div(states["SURE"], n)) if n else None,
                         "review_pct": _pct(_div(states["CHECK"] + states["UNKNOWN"], n)) if n else None, "states": dict(states)},
            "count_mae_per_sku_raw_top1": round(float(np.mean(sku_mae)), 3) if sku_mae else None,
            "latency_ms": {"p50": round(statistics.median(lat)), "p95": round(float(np.percentile(lat, 95)))} if lat else None,
            "calibration": calibrate(cs, cc, ci, calibration_out), "per_image": per_img}


def cmd_run(a):
    from app.config import Settings
    from eval.evaluate import build
    root = Path(a.data)
    manifest_path = root / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    labels = json.loads((root / "labels.json").read_text())
    if a.calibration_out:
        if manifest.get("synthetic") is True:
            raise SystemExit("refusing to write calibration from a dataset marked synthetic")
        groups = manifest.get("capture_groups", {})
        missing = [name for name, lab in labels.items() if not (lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id") or groups.get(name))]
        unique = {str(lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id") or groups.get(name))
                  for name, lab in labels.items() if name not in missing}
        if missing or len(unique) < 2:
            raise SystemExit("calibration needs capture_group, shelf_id, or session_id on every image and at least two groups")
    if a.weights and a.backend != "yolo":
        raise SystemExit("--weights is only valid with --backend yolo")
    if a.weights and not Path(a.weights).is_file():
        raise SystemExit(f"YOLO weights file does not exist: {a.weights}")
    s = dataclasses.replace(Settings(), detector_backend=a.backend,
                            yolo_weights=str(Path(a.weights).resolve()) if a.weights else Settings().yolo_weights)
    p = build(s)
    rep = run_eval(p, Path(a.data), a.sure, a.check, a.margin, a.iou, a.calibration_out)
    mf = Path(a.data) / "MANIFEST.json"
    weights_path = Path(s.yolo_weights).resolve() if a.backend == "yolo" else Path(s.h5_model_path).resolve()
    label = a.label or ("h5" if a.backend == "h5" else ("custom-yolo" if a.weights else "configured-yolo"))
    rep["meta"] = {"backend": a.backend, "label": label, "weights": str(weights_path),
                   "device": str(getattr(p.embedder, "device", "n/a")), "dino_model": s.dino_model,
                   "thresholds": {"sure": a.sure, "check": a.check, "margin": a.margin}, "iou": a.iou,
                   "synthetic": json.loads(mf.read_text()).get("synthetic") if mf.exists() else None,
                   "ran_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: v for k, v in rep.items() if k != "per_image"}, indent=1))


# ------------------------------------------------------------------ bench + report
def cmd_bench(a):
    lines = []  # stream live: the 10k-SKU row can take minutes
    pr = subprocess.Popen([sys.executable, "-u", "-m", "eval.bench_search"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for ln in pr.stdout:
        print(ln, end=""); lines.append(ln)
    pr.wait(); Path(a.out).write_text("".join(lines))


def _f(v, suf=""): return "n/a" if v is None else f"{v}{suf}"


def cmd_report(a):
    reps = {}
    files = []
    for pattern in a.reports.split(","):
        files.extend(glob.glob(pattern.strip()))
    for f in sorted(set(files)):
        r = json.loads(Path(f).read_text()); meta = r.get("meta", {})
        key = meta.get("label") or meta.get("backend") or Path(f).stem
        if key in reps:
            key = f"{key}:{Path(f).stem}"
        reps[key] = r
    if not reps: raise SystemExit(f"no report files match {a.reports}; run `run` first")
    synth = any(r.get("meta", {}).get("synthetic") for r in reps.values())
    L = ["# CALIBREX results", "", f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{a.reports}`.", ""]
    if synth:
        L += ["> **SYNTHETIC DATA.** Shelf art equals reference art, so these numbers are optimistic and only show the pipeline works.",
              "> Do not quote them as accuracy or use them to pick the detector. Re-run on real labelled shelf photos.", ""]
    names = list(reps)
    L += ["| Metric | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    def row(label, fn): L.append(f"| {label} | " + " | ".join(fn(reps[n]) for n in names) + " |")
    for b, _, _ in BANDS:
        row(f"{b}: detection recall @IoU0.5", lambda r, b=b: _f(r["bands"].get(b, {}).get("det_recall")))
        row(f"{b}: detection precision", lambda r, b=b: _f(r["bands"].get(b, {}).get("det_precision")))
        row(f"{b}: total-count MAE (items/shelf)", lambda r, b=b: _f(r["bands"].get(b, {}).get("total_count_mae")))
        row(f"{b}: boxes per labelled item (ideal 1.0)", lambda r, b=b: _f(r["bands"].get(b, {}).get("boxes_per_item")))
        row(f"{b}: fragment rate", lambda r, b=b: _f(r["bands"].get(b, {}).get("fragment_rate")))
        if b == "large":
            row("large: whole-item mean best IoU", lambda r, b=b: _f(r["bands"][b].get("whole_item_iou")))
    row("top-1 SKU accuracy (matched boxes)", lambda r: _f(r["matching"]["top1_acc_on_matched"]))
    row("SURE-bucket precision", lambda r: _f(r["matching"]["sure_precision"]))
    row("auto-resolved (SURE) %", lambda r: _f(r["matching"]["auto_resolved_pct"]))
    row("sent to review %", lambda r: _f(r["matching"]["review_pct"]))
    row("per-SKU count MAE (raw top-1)", lambda r: _f(r["count_mae_per_sku_raw_top1"]))
    row("held-out calibration ECE", lambda r: _f(r["calibration"].get("held_out_ece")))
    row("latency p50 / p95 (ms)", lambda r: "n/a" if not r["latency_ms"] else f"{r['latency_ms']['p50']} / {r['latency_ms']['p95']}")
    row("device", lambda r: r.get("meta", {}).get("device", "n/a"))
    L += ["", "## Proposed targets vs measured", "", "| Target | Goal | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    def chk(v, goal, ge=True):
        return "n/a" if v is None else ("PASS" if (v >= goal if ge else v <= goal) else "FAIL") + f" ({v})"
    L.append("| large-item recall | >= 0.90 | " + " | ".join(chk(reps[n]["bands"].get("large", {}).get("det_recall"), .90) for n in names) + " |")
    L.append("| large-item fragment rate | <= 0.10 | " + " | ".join(chk(reps[n]["bands"].get("large", {}).get("fragment_rate"), .10, False) for n in names) + " |")
    L.append("| dense recall | >= 0.90 | " + " | ".join(chk(reps[n]["bands"]["dense"]["det_recall"], .90) for n in names) + " |")
    L.append("| SURE precision | >= 0.98 | " + " | ".join(chk(reps[n]["matching"]["sure_precision"], .98) for n in names) + " |")
    L.append("| auto-resolved % | >= 60 | " + " | ".join(chk(reps[n]["matching"]["auto_resolved_pct"], 60.0) for n in names) + " |")
    L.append("| calibration ECE | <= 0.05 | " + " | ".join(chk(reps[n]["calibration"].get("held_out_ece"), .05, False) for n in names) + " |")
    h5_name = next((n for n in names if n.lower() in ("h5", "h5-baseline")), None)
    new_name = next((n for n in names if n.lower() in ("calibrex-yolo", "configured-yolo", "fine-tuned-yolo", "finetuned-yolo", "yolo-finetuned")), None)
    if h5_name and new_name:
        h5, new = reps[h5_name], reps[new_name]
        large_recall = new["bands"].get("large", {}).get("det_recall")
        frag = new["bands"].get("large", {}).get("fragment_rate")
        dense, h5_dense = new["bands"].get("dense", {}).get("det_recall"), h5["bands"].get("dense", {}).get("det_recall")
        sure, h5_sure = new["matching"].get("sure_precision"), h5["matching"].get("sure_precision")
        cond = [large_recall is not None and large_recall >= .90,
                frag is not None and frag <= .10,
                dense is not None and h5_dense is not None and dense >= h5_dense - .02,
                sure is not None and h5_sure is not None and sure >= h5_sure]
        L += ["", "## Detector decision (recommendation only)", ""]
        if synth:
            L.append("Synthetic reports cannot select a production detector. Re-run this comparison on held-out real shelf photos.")
        else:
            for label, ok, value in zip(("large recall >= 0.90", "large fragment rate <= 0.10",
                                          "dense recall within 2 points of H5", "SURE precision >= H5"), cond,
                                         (large_recall, frag, dense, sure)):
                L.append(f"- {'PASS' if ok else 'FAIL'} — {label} (fine-tuned={value})")
            L.append("- Recommendation: use these checks to guide a detector choice; they do not change the configured worker model automatically.")
    elif len(names) > 1 and not synth:
        L += ["", "## Detector decision (recommendation only)", "", "Include reports labelled `h5` and `calibrex-yolo` to evaluate the supplied decision rule."]
    bf = Path(a.bench)
    if bf.exists(): L += ["", "## Catalog search speed (synthetic vectors, ms per crop)", "", "```", bf.read_text().strip(), "```"]
    L += ["", "## Reproduce", "", "```bash", "cd ai-worker", "python -m eval.phase_a check --data ./eval_set",
          "python -m eval.phase_a run --data ./eval_set --backend h5 --label h5 --out report-h5.json  # synthetic plumbing fixture only",
          "python -m eval.phase_a run --data ./eval_set --backend yolo --weights runs/detect/runs/detect/calibrex_s960/weights/best.pt --label calibrex-yolo --out report-yolo.json  # synthetic plumbing fixture only",
          "python -m eval.phase_a run --data ./eval_set --backend yolo --weights ./models/calibrex-retail-item.pt --label fine-tuned-yolo --out report-yolo-finetuned.json",
          "python -m eval.phase_a run --data ./real_eval_set --backend h5 --out report-h5-real.json --calibration-out /models/calibration.json",
          "python -m eval.phase_a bench", "python -m eval.phase_a report --data ./eval_set --out ../docs/RESULTS.md", "```",
          "", "Limits: detection recall is class-agnostic (any box on a labelled item); count MAE is predicted boxes vs labelled items, so it",
          "ignores fully hidden items. Thresholds 0.85 / 0.60 / 0.08 are starting values, not tuned."]
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True); out.write_text("\n".join(L) + "\n"); print("wrote", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("build-synthetic"); s.add_argument("--products", required=True); s.add_argument("--out", default="eval_set")
    s.add_argument("--skus", type=int, default=30); s.add_argument("--shelves", type=int, default=5, help="per band"); s.add_argument("--width", type=int, default=2400)
    s.add_argument("--seed", type=int, default=1); s.set_defaults(fn=cmd_build)
    s = sp.add_parser("convert"); s.add_argument("--coco", required=True); s.add_argument("--images", required=True); s.add_argument("--catalog")
    s.add_argument("--out", default="eval_set"); s.add_argument("--skip", nargs="*", default=[], help="class names to ignore")
    s.add_argument("--allow-missing-catalog", action="store_true", help="labels are for class-agnostic detection training; SKU reference folders are not required")
    s.set_defaults(fn=cmd_convert)
    s = sp.add_parser("to-yolo"); s.add_argument("--data", required=True); s.add_argument("--out", required=True)
    s.add_argument("--val-frac", type=float, default=.2); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--eval-data", action="append", default=[], help="additional held-out image set to hash-check; may be repeated")
    s.add_argument("--allow-eval-source", action="store_true", help="explicitly override frozen eval-set/hash protection")
    s.set_defaults(fn=cmd_to_yolo)
    s = sp.add_parser("check"); s.add_argument("--data", default="eval_set"); s.add_argument("--allow-missing-catalog", action="store_true")
    s.set_defaults(fn=lambda a: sys.exit(cmd_check(a)))
    s = sp.add_parser("run"); s.add_argument("--data", default="eval_set"); s.add_argument("--backend", choices=("h5", "yolo"), required=True)
    s.add_argument("--out", default=None); s.add_argument("--calibration-out"); s.add_argument("--iou", type=float, default=0.5)
    s.add_argument("--weights", help="explicit YOLO weights file; no generic fallback is used when this is supplied")
    s.add_argument("--label", help="report identifier, e.g. h5 or calibrex-yolo")
    s.add_argument("--sure", type=float, default=0.85); s.add_argument("--check", type=float, default=0.60); s.add_argument("--margin", type=float, default=0.08)
    s.set_defaults(fn=cmd_run)
    s = sp.add_parser("bench"); s.add_argument("--out", default="bench_search.txt"); s.set_defaults(fn=cmd_bench)
    s = sp.add_parser("report"); s.add_argument("--data", default="eval_set"); s.add_argument("--reports", default="report-*.json")
    s.add_argument("--bench", default="bench_search.txt"); s.add_argument("--out", default="../docs/RESULTS.md"); s.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    if a.cmd == "run" and not a.out: a.out = f"report-{a.backend}.json"
    a.fn(a)


if __name__ == "__main__":
    main()
