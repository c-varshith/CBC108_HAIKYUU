# CALIBREX ai-worker (Phase 4)

Fine-tuned Calibrex YOLO (boxes only) -> crop -> DINOv2 CLS embedding -> Qdrant cosine search -> per-SKU raw scores. The backend applies the
Owner-editable threshold matrix (SURE / CHECK / UNKNOWN); this service only returns scores and crop-quality flags.
**The selected detector is `runs/detect/runs/detect/calibrex_s960/weights/best.pt`. Registering a product embeds reference photos into Qdrant; it does not retrain the detector.**

## How products are learned

The backend stores product/photo metadata in its database and image files in configured storage. On registration, DINOv2 embeds the photos and Qdrant stores vectors by SKU. A later scan searches those references without restarting the worker. Catalog photos improve SKU matching; they do not retrain the detector. Use at least three clear photos from different angles, with the full package visible, good lighting, and some background variation. A close gap between the top similarity scores means packaging may resemble another catalog product; add varied references and review the result.

Reference images can be your own photos, downloaded product images provided to the backend importer with `--images-dir`, or Open Food Facts via `scripts/import_catalog.py`. OFF product images are licensed **CC BY-SA 3.0**, so retain attribution and follow share-alike requirements; CALIBREX exposes imported image credits in the UI. No fine-tuning happens during registration.

For a large OFF catalog, use the root `select_off_barcodes.py` against a local TSV/gzip dump, then import its barcode list with `--catalog-dump` and `--images-dir`. That mode reads catalog metadata from the local dump and local photos only; it sends no OFF requests. Missing local photos are skipped because the worker needs at least one reference image to create searchable vectors. Three or more varied photos per product are recommended when collecting your own shelf references.

```
app/config.py      env settings            app/geometry.py  tiling, NMS, border-cut filter, reading order (pure numpy)
app/detector.py    H5 RetinaNet or YOLO11 + tiled pass   app/quality.py   tiny / blurry / glare flags
app/embedder.py    DINOv2 (fp16 on CUDA)   app/store.py     Qdrant + in-memory store, per-SKU aggregation
app/pipeline.py    detect -> embed -> search   app/main.py  FastAPI: /v1/detect /v1/references /v1/products/{sku} /health
train/finetune_sku110k.py   one-class fine-tune        eval/evaluate.py  MAE, auto-resolved %, P/R, latency
eval/bench_search.py        Qdrant speed vs catalog size   tests/            detector, geometry, pipeline and API unit tests
```

## Wire contract (matches backend/app/infra/inference.py)
- `POST /v1/detect` multipart `file, scan_id, top_k` -> `{image:{width,height}, detections:[{bbox, det_confidence, matches:[{sku,score}], flags:[]}], timings_ms}`
  (boxes in pixels of the image sent, reading order; `matches` = distinct SKUs, best first).
- `POST /v1/references` multipart `sku, name, ref_ids (JSON list), files...` -> `{added}`. `DELETE /v1/products/{sku}` -> 204.
- `DELETE /v1/products/{sku}/references/{ref_id}` -> 204; safe to repeat, removes all augmented vectors for that stored reference photo.
- `GET /health` -> status plus `model` (the loaded `DINO_MODEL`), `collection` (the active `QDRANT_COLLECTION`), and `detector` (`backend` and loaded weight filename), alongside the existing fields such as `calibrated`.
  The backend compares `model@collection` with the `indexed_with` value it recorded when the worker acknowledged each product's photos, and warns on a mismatch.
- `POST /v1/references` accepts at most `MAX_REFS_PER_CALL` photos per request and answers 422 above that (the backend's bulk senders use `min(10, AI_WORKER_MAX_REFS_PER_CALL)`).
- `Authorization: Bearer $AI_WORKER_TOKEN` on everything except `/health`. `ENV=production` refuses to start without a token.

## Run
```bash
# 1. Build and start the standard stack from the project root. It includes the fine-tuned checkpoint and Qdrant.
docker compose up --build                                                  # CPU
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build  # CUDA 12.1
# 2. Without Docker, run from ai-worker/; the default checkpoint path is resolved from this directory.
pip install torch torchvision && pip install -r requirements.txt
DETECTOR_BACKEND=yolo QDRANT_URL=:memory: DEVICE=cpu uvicorn app.main:app_factory --factory --port 8001
# 3. For local H5 inference, install requirements-h5.txt, provide the ignored project-root model file, and set DETECTOR_BACKEND=h5.
```
Auto device: CUDA if available (FP16), else CPU (FP32, same code path; expect roughly 10-30x slower per image).
The worker fails startup if the configured checkpoint is missing, empty, or unloadable; it does not substitute generic weights.
Qdrant data lives in the `qdrantdata` volume. **Changing `DINO_MODEL` changes the vector size: use a new `QDRANT_COLLECTION`
and re-upload reference photos** (the worker refuses to start on a size mismatch instead of returning garbage). The backend's resync (see below) is the way to repopulate the new collection from the stored photos.

## Train / evaluate / benchmark
```bash
python -m train.build_detector_dataset --products /path/to/sku-packshots --out /data/calibrex/synthetic --n-train 2000 --n-val 500
python -m train.build_detector_dataset --check /data/calibrex/synthetic
python -m eval.phase_a to-yolo --data /path/to/real-train-source --out /data/calibrex/real-yolo
python -m train.train_calibrex_detector --sku110k /data/SKU-110K --synthetic /data/calibrex/synthetic \
  --real /data/calibrex/real-yolo --data /data/calibrex/calibrex.yaml --dry-run
python -m eval.phase_a run --data ./eval_set --backend h5 --label h5 --out report-h5.json
python -m eval.phase_a run --data ./eval_set --backend yolo --label configured-yolo --out report-yolo.json
python -m eval.phase_a run --data ./eval_set --backend yolo --weights /path/to/best.pt --label comparison-yolo --out report-yolo-comparison.json
python -m eval.phase_a report --data ./eval_set --reports 'report-h5.json,report-yolo.json,report-yolo-comparison.json' --out ../docs/RESULTS.md
python -m eval.bench_search                                      # 100 / 1k / 10k SKUs
python -m pytest -q                                               # from ai-worker/
```

The fine-tuned Calibrex checkpoint is selected for the Scan workflow. This selection does not establish accuracy: evaluate it on a separate, real held-out shelf set before making performance claims. The frozen `eval_set` is synthetic and is only a plumbing fixture. Read [docs/TRAINING.md](../docs/TRAINING.md) for the GPU runbook, data leakage guards, metrics, license notes, and rollback steps. The legacy `train.finetune_sku110k` command remains available for single-source experiments; it does not combine CALIBREX synthetic or real data.

## Confidence: similarity vs probability
`matches[].score` is a DINOv2 cosine **similarity**, not a probability. To get meaningful percentages, fit a calibration on labelled shelf photos (boxes with SKUs in `labels.json`):
```bash
python -m eval.evaluate --data ./real_eval_set --calibration-out /models/calibration.json   # real grouped captures only; >= 50 matched boxes
```
On a real, non-synthetic dataset with capture groups, it reports held-out expected calibration error and writes a calibration file fitted only on the training groups. Calibration output is refused for the bundled synthetic fixture. Set `CALIBRATION_PATH` (default `/models/calibration.json`). With a file, each detection also carries `match_probability` (P(top-1 SKU correct)) and `/health` reports `calibrated: true`; without one it is `null` and the UI says "similarity (not calibrated)". Re-fit after changing `DINO_MODEL`, the detector, or the catalog substantially.

The bundled `eval_set/MANIFEST.json` is marked `synthetic: true`; it is a pipeline smoke fixture and is not evidence of real shelf accuracy. Its 30 label SKUs match its 30 reference-photo folders, but only 7 overlap the 300-barcode `stocked.csv`. That CSV is a SKU list, not detector-training data or a set of reference photos. Do not use the synthetic reports to tune thresholds or claim accuracy. For a pilot evaluation, use real hand-labeled shelf captures and grouped shelf/capture sessions; keep a separate hand-counted set for release decisions.

Each real shelf label entry should include `counts` and preferably per-object `boxes` with pixel `bbox` and `sku`, for example:

```json
{
  "aisle1_shelf2_run3.jpg": {
    "capture_group": "aisle1-shelf2-run3",
    "counts": {"SKU-001": 4},
    "boxes": [{"bbox": [25, 40, 180, 310], "sku": "SKU-001"}]
  }
}
```

Compare H5 and YOLO on the same grouped dataset and review detection precision/recall, count error, per-SKU match accuracy, auto-resolved/review rates, and p95 latency before choosing the pilot model. Do not treat generated reports as production benchmarks while their dataset metadata says `synthetic: true`.

## Swapping the detector

`DETECTOR_BACKEND=h5` selects the project-root `iou_resnet50_csv_06.h5` full RetinaNet model. Its dynamic RGB input is resized with aspect ratio preserved (short side 800, long side at most 1333), converted to BGR, then ImageNet Caffe mean-subtracted. The model returns box regression deltas, one objectness class score per anchor, and an auxiliary IoU estimate. The worker decodes the regression head with the model's standard P3-P7 anchors and uses the single class score as `det_confidence`; the auxiliary IoU head is not used as a product score.

This is a **one-class object detector**, not a product classifier. It localizes the one object category represented during training; product names and SKUs still come from DINOv2 reference-photo vectors and Qdrant. It does not impose a fixed product label list, so adding a SKU still needs only reference photos. If the training class was not generic shelf items, recall on other object types may be poor; validate it against labeled shelf photos.

The standard worker image omits TensorFlow/Keras and the H5 weights. The weights are excluded from GitHub; provide `iou_resnet50_csv_06.h5` in the project root to use this alternative. For Docker, start with `docker-compose.h5.yml`, which installs the optional H5 dependencies and mounts the model. For local inference, install `requirements-h5.txt` and set `DETECTOR_BACKEND=h5`. YOLO is the default; set `YOLO_WEIGHTS` to a different checkpoint only when intentionally changing detectors. A missing or unloadable configured model fails startup.

## Assumptions and honest limits
- The H5 alternative uses legacy Keras compatibility and requires labeled shelf photos for meaningful accuracy evaluation.
- No accuracy numbers are claimed: mAP/recall, count MAE, % auto-resolved and CPU-vs-GPU latency must come from `train/` and `eval/`
  on a real GPU with your catalog. Thresholds 0.85 / 0.60 / 0.08 are the spec's starting values; calibrate per SKU on validation data.
- Reference photos are embedded as given plus one 85% centre crop. Photograph products on a shelf, not a white background, if you can.
- Qdrant stores the vectors; the relational catalog and stored photos are the source of truth.
  repopulate it with the backend's resync: `cd backend && python -m scripts.resync_qdrant [--org SLUG] [--sku X ...]`, or the Owner endpoint
  `POST /catalog/resync` (202 + `batch_id`, poll `GET /catalog/batches/{id}`; 409 if one is already running, 503 if this worker is unreachable).
  Resync re-embeds references stored in the catalog. Individual reference deletion removes that reference's vectors; both operations can be retried.
  Resync is additive and cannot discover unknown orphan vectors left by manual Qdrant edits or older deployments. Not measured here: embedding time for a large catalog on your hardware.
- Single-process worker with a concurrency semaphore (GPU-safe). Scale by adding containers behind the backend, not `--workers`.
