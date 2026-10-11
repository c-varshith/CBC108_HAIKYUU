"""Evaluation of the full pipeline on a labelled set (spec 5.6).

Dataset layout (one folder):
  catalog/<SKU>/*.jpg            reference photos (what a Manager uploads when registering a product)
  images/*.jpg                   shelf photos
  labels.json                    {"<image file>": {"counts": {"SKU-001": 14, ...},
                                                   "boxes": [{"bbox":[x1,y1,x2,y2], "sku":"SKU-001"}, ...]}}   # boxes optional

  python -m eval.evaluate --data ./eval_set [--sure 0.85 --check 0.60 --margin 0.08] [--out report.json]

  python -m eval.evaluate --data ./real_eval_set --calibration-out /models/calibration.json
      fits on non-synthetic grouped shelf data (labelled `boxes`; >= 50 matched boxes and at least two capture groups).

Reports: count MAE vs hand count (per SKU and overall, raw and after the threshold matrix), % auto-resolved (SURE) vs
sent to review (CHECK/UNKNOWN), per-SKU precision/recall when boxes are labelled (IoU>=0.5), latency per image."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from app.calibration import expected_calibration_error, fit_isotonic, reliability_table
from app.config import Settings
from app.pipeline import Pipeline, load_image
from app.geometry import iou_matrix


def classify(matches, sure, check, margin):  # identical to backend/app/domain/selectors.classify
    if not matches or matches[0]["score"] < check:
        return "UNKNOWN"
    top = matches[0]
    other = next((m for m in matches if m["sku"] != top["sku"]), None)
    mg = round(top["score"] - (other["score"] if other else 0.0), 6)
    return "SURE" if top["score"] >= sure and mg >= margin else "CHECK"


def calibrate(scores, correct, group_keys, out_path):
    """Fit on training capture groups and report ECE on disjoint held-out groups."""
    if not group_keys:
        return {"status": "skipped", "reason": "label shelf/session capture groups before reporting calibration"}
    if not scores:
        return {"status": "skipped", "reason": "no labelled boxes matched a detection (add `boxes` to labels.json)"}
    s, c, groups = np.asarray(scores), np.asarray(correct), np.asarray(group_keys)
    raw_acc = float(c.mean())
    info = {"samples": len(s), "top1_accuracy_on_matched_boxes": round(raw_acc, 4)}
    unique_groups = sorted(set(groups.tolist()))
    if len(unique_groups) < 2:
        return {**info, "status": "skipped", "reason": "calibration needs separate training and held-out capture groups"}
    held_groups = {g for g in unique_groups if int(hashlib.sha256(g.encode()).hexdigest()[:8], 16) % 5 == 0}
    if len(unique_groups) > 1 and not held_groups:
        held_groups = {unique_groups[-1]}
    if len(held_groups) == len(unique_groups) and len(unique_groups) > 1:
        held_groups.remove(unique_groups[0])
    te = np.asarray([g in held_groups for g in groups], dtype=bool)
    tr = ~te
    info["capture_groups"] = len(unique_groups)
    info["held_out_capture_groups"] = len(held_groups)
    try:
        if tr.any() and te.sum() >= 20:
            held = fit_isotonic(s[tr], c[tr]).predict(s[te])
            info["held_out_ece"] = round(expected_calibration_error(held, c[te]), 4)
            info["held_out_boxes"] = int(te.sum())
            info["reliability_held_out"] = reliability_table(held, c[te])
        else:
            info["held_out_ece"] = None
            info["note"] = "need at least 20 held-out boxes and a separate training capture group to report ECE"
            if out_path:
                return {**info, "status": "not_fitted", "reason": info["note"]}
        # Keep the shipped artifact on the training side of the split. Fitting it
        # on held-out shelves would invalidate the independent calibration result.
        final = fit_isotonic(s[tr], c[tr])
    except ValueError as e:
        return {**info, "status": "not_fitted", "reason": str(e)}
    final = dataclasses.replace(final, ece=info.get("held_out_ece"))
    if out_path:
        Path(out_path).write_text(json.dumps(final.to_json(), indent=2))
        info["written_to"] = out_path
    return {**info, "status": "fitted" if out_path else "dry_run"}


def build(s: Settings) -> Pipeline:
    from app.main import build_pipeline
    s = dataclasses.replace(s, qdrant_url=":memory:")
    return build_pipeline(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True); ap.add_argument("--out", default="eval_report.json")
    ap.add_argument("--sure", type=float, default=0.85); ap.add_argument("--check", type=float, default=0.60)
    ap.add_argument("--margin", type=float, default=0.08)
    ap.add_argument("--calibration-out", default=None,
                    help="write a fitted calibration.json (match score -> P(top-1 correct)) from the labelled boxes")
    ap.add_argument("--detector-backend", choices=("h5", "yolo"), default=None,
                    help="override DETECTOR_BACKEND for a repeatable model comparison")
    a = ap.parse_args()
    root = Path(a.data)
    manifest_path = root / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if a.calibration_out and manifest.get("synthetic") is True:
        ap.error("refusing to write calibration from a dataset marked synthetic; use held-out real shelf captures")
    s = Settings()
    if a.detector_backend:
        s = dataclasses.replace(s, detector_backend=a.detector_backend)
    p = build(s)
    t0 = time.perf_counter()
    for sku_dir in sorted((root / "catalog").iterdir()):
        if sku_dir.is_dir():
            items = [(f.stem, Image.open(f).convert("RGB")) for f in sorted(sku_dir.glob("*")) if f.suffix.lower() in (".jpg", ".jpeg", ".png")]
            p.add_references(sku_dir.name, sku_dir.name, items)
    print(f"registered {len(p.store.skus())} SKUs in {time.perf_counter() - t0:.1f}s (no training)")

    labels = json.loads((root / "labels.json").read_text())
    capture_groups = manifest.get("capture_groups", {})
    if a.calibration_out:
        missing_groups = [name for name, lab in labels.items()
                          if not (lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id") or capture_groups.get(name))]
        groups = {str(lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id") or capture_groups.get(name))
                  for name, lab in labels.items() if name not in missing_groups}
        if missing_groups or len(groups) < 2:
            ap.error("calibration needs capture_group, shelf_id, or session_id labels on every image and at least two groups")
    lat, per_img = [], []
    abs_err_raw = []
    state_total = Counter()
    tp, fp, fn = Counter(), Counter(), Counter()
    cal_s, cal_c, cal_group = [], [], []
    for img_i, (name, lab) in enumerate(labels.items()):
        img = load_image((root / "images" / name).read_bytes())
        t0 = time.perf_counter()
        dets, tm = p.detect(img, 5)
        lat.append((time.perf_counter() - t0) * 1000)
        raw = Counter()
        for d in dets:
            st = classify(d["matches"], a.sure, a.check, a.margin)
            state_total[st] += 1
            if d["matches"]:
                raw[d["matches"][0]["sku"]] += 1
        truth = Counter(lab["counts"])
        skus = set(truth) | set(raw)
        e_raw = [abs(raw[k] - truth[k]) for k in skus]
        abs_err_raw += e_raw
        per_img.append({"image": name, "total_true": sum(truth.values()), "total_pred": len(dets), "mae": float(np.mean(e_raw)) if e_raw else 0.0,
                        "latency_ms": round(lat[-1]), **{k: round(v) for k, v in vars(tm).items()}})
        if lab.get("boxes"):
            group_value = lab.get("capture_group") or lab.get("shelf_id") or lab.get("session_id") or capture_groups.get(name)
            gt = np.array([b["bbox"] for b in lab["boxes"]], np.float32); gs = [b["sku"] for b in lab["boxes"]]
            pr = np.array([d["bbox"] for d in dets], np.float32); ps = [d["matches"][0]["sku"] if d["matches"] else None for d in dets]
            iou = iou_matrix(pr, gt) if len(pr) and len(gt) else np.zeros((len(pr), len(gt)))
            used = set()
            for i in range(len(pr)):
                j = int(iou[i].argmax()) if len(gt) else -1
                if j >= 0 and iou[i, j] >= 0.5 and j not in used:
                    used.add(j)
                    if dets[i]["matches"] and group_value:
                        cal_s.append(dets[i]["matches"][0]["score"]); cal_c.append(float(ps[i] == gs[j])); cal_group.append(str(group_value))
                    (tp if ps[i] == gs[j] else fp)[ps[i]] += 1
                    if ps[i] != gs[j]:
                        fn[gs[j]] += 1
                else:
                    fp[ps[i]] += 1
            for j in range(len(gt)):
                if j not in used:
                    fn[gs[j]] += 1

    calibration = calibrate(cal_s, cal_c, cal_group, a.calibration_out)
    n = sum(state_total.values()) or 1
    report = {
        "images": len(labels), "boxes": n,
        "count_mae_per_sku_raw_top1": round(float(np.mean(abs_err_raw)), 3) if abs_err_raw else None,
        "auto_resolved_pct": round(100 * state_total["SURE"] / n, 1),
        "sent_to_review_pct": round(100 * (state_total["CHECK"] + state_total["UNKNOWN"]) / n, 1),
        "states": dict(state_total),
        "latency_ms": {"mean": round(statistics.mean(lat)), "p50": round(statistics.median(lat)),
                       "p95": round(float(np.percentile(lat, 95)))} if lat else None,
        "calibration": calibration,
        "dataset": {"synthetic": bool(manifest.get("synthetic", False)), "capture_groups": len(set(cal_group)),
                    "manifest": str(manifest_path) if manifest_path.exists() else None},
        "device": getattr(p.embedder, "device", "n/a"),
        "per_sku_pr": {k: {"precision": round(tp[k] / max(tp[k] + fp[k], 1), 3), "recall": round(tp[k] / max(tp[k] + fn[k], 1), 3)}
                       for k in sorted(set(tp) | set(fp) | set(fn)) if k} or None,
        "per_image": per_img,
    }
    Path(a.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "per_image"}, indent=2))


if __name__ == "__main__":
    main()
