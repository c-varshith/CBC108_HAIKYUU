# CALIBREX detector training runbook

This trains a **one-class retail-item box detector**. It learns package boundaries, not barcodes or product identities. CALIBREX identifies each crop with DINOv2 and Qdrant. The Scan workflow currently uses the fine-tuned Calibrex YOLO checkpoint; model selection does not establish accuracy, so evaluate it on a separate, real held-out shelf set before making performance claims or replacing it.

## Data and license rules

- Keep `ai-worker/eval_set/` frozen and evaluation-only. Do not train on its images or labels. The builder checks image hashes; synthetic training validation SKUs are held out from training.
- Put packshots in an external folder, one image per SKU, named `<SKU>.jpg` (PNG/WebP also accepted). This repository does not contain local product photos for the full pilot catalog.
- Label real training photos with one box per visible physical package. Add a stable `capture_group`, `shelf_id`, or `session_id` to each image annotation. Keep held-out real evaluation shelves in a separate directory and never pass them to training conversion.
- SKU-110K is restricted by its project to academic, non-commercial use. CALIBREX's current assumption is non-commercial research; do not use that source for commercial training without permission. [SKU-110K project terms](https://github.com/eg4000/SKU110K_CVPR19)
- Ultralytics code/models use AGPL-3.0, with an Enterprise license option for production integration. Review the applicable terms before distributing or commercially deploying a model trained with this stack. [Ultralytics licensing](https://docs.ultralytics.com/)
- Keep Open Food Facts image attribution and CC BY-SA 3.0 notices with any derived training data that uses those images. Check each source image's rights before combining it with other datasets.

## 1. Prepare packshots and build synthetic scenes

Run from `ai-worker/`. The builder trims near-white borders and creates large close-ups, medium scenes, dense scenes, and unlabelled negative scenes. Its default mix is 25/30/35/10 percent.

```bash
python -m train.build_detector_dataset \
  --products /data/calibrex/product-packshots \
  --out /data/calibrex/synthetic \
  --n-train 2000 --n-val 500 --val-sku-frac 0.2 --seed 11

python -m train.build_detector_dataset --check /data/calibrex/synthetic
```

For a quick plumbing check, the requested small build is:

```bash
python -m train.build_detector_dataset \
  --products /data/calibrex/product-packshots \
  --out /tmp/calibrex-detector-smoke \
  --n-train 20 --n-val 8 --seed 11
python -m train.build_detector_dataset --check /tmp/calibrex-detector-smoke
```

You can change the scene proportions with `--large-frac`, `--medium-frac`, `--dense-frac`, and `--negative-frac`. Keep them non-negative with a positive sum. `--eval-data` selects the frozen evaluation directory used by the hash guard; the normal default is `eval_set/`.

## 2. Convert separately labeled real training photos

Create a COCO export from CVAT or Label Studio, with capture-group metadata on every image, then convert it to CALIBREX labels:

```bash
python -m eval.phase_a convert \
  --coco /data/calibrex/real-train-coco.json \
  --images /data/calibrex/real-train-images \
  --out /data/calibrex/real-train-source --allow-missing-catalog

python -m eval.phase_a check --data /data/calibrex/real-train-source --allow-missing-catalog
python -m eval.phase_a to-yolo \
  --data /data/calibrex/real-train-source \
  --out /data/calibrex/real-yolo --val-frac 0.2 --seed 11 \
  --eval-data ./real_eval_set
```

`to-yolo` splits by capture group, so images from the same shelf/session cannot land on both sides. It refuses the frozen `eval_set` and any source containing one of its image hashes. Pass each separate held-out evaluation set with `--eval-data`; the option can be repeated. `--allow-eval-source` is a loud, explicit override for debugging only; never use it for a training run.

## 3. Prepare SKU-110K and build the combined config

Obtain SKU-110K under its terms and prepare it in YOLO format with `images/{train,val}` and `labels/{train,val}`. Its download is large (about 13 GB, before caches and outputs). The runner does not download datasets automatically.

Generate a mixed data config. Source weights default to SKU-110K 50%, synthetic 35%, real 15%; omitted sources are removed and the remaining weights are normalized. Repeated paths in the training list provide the requested mix. Validation remains unweighted and separate per source.

```bash
python -m train.train_calibrex_detector \
  --sku110k /data/SKU-110K \
  --synthetic /data/calibrex/synthetic \
  --real /data/calibrex/real-yolo \
  --eval-data /data/calibrex/real-eval \
  --data /data/calibrex/calibrex.yaml \
  --samples-per-epoch 10000 --seed 11 --dry-run
```

The runner always checks training image hashes against the frozen `eval_set/` and checks additional held-out directories passed with repeatable `--eval-data`. The dry run validates the generated YAML, image lists, associated label files, and these leakage guards. It does not download model weights, download data, or train. Run it again without `--dry-run` on a CUDA training host to start training.

For a 12–16 GB GPU starting point, try `--low-vram` (YOLO11s, 1024 pixels, batch 4). Actual memory use depends on image composition and GPU; lower batch size if CUDA reports out-of-memory.

## 4. Train or resume on a GPU host

The project machine currently has no working CUDA device and insufficient free disk for SKU-110K, so run this on a suitable local GPU, Colab, or Kaggle session. Install the AI worker training dependencies in that environment and place the repository plus datasets on attached storage.

```bash
cd ai-worker
python -m pip install -r requirements.txt
python -m train.train_calibrex_detector \
  --sku110k /data/SKU-110K \
  --synthetic /data/calibrex/synthetic \
  --real /data/calibrex/real-yolo \
  --eval-data /data/calibrex/real-eval \
  --data /data/calibrex/calibrex.yaml \
  --model yolo11m.pt --epochs 100 --imgsz 1280 --batch 8 --device 0 \
  --name calibrex-retail-item
```

The training settings use one class, up to 1,000 detections, mosaic with the final 10 epochs closed, scale 0.9, small rotation, horizontal flips, cosine learning rate, and patience 20. It reports precision, recall, mAP50, and mAP50-95 separately for each available source validation set.

### Google Colab

Select a GPU runtime, attach Drive, and place or unzip the repository and prepared datasets under Drive. Replace the paths below with the folder names you uploaded. Keep training output on Drive so a disconnected session does not lose checkpoints.

```bash
!git clone <YOUR_REPOSITORY_URL> /content/calibrex
from google.colab import drive
drive.mount('/content/drive')
%cd /content/calibrex/ai-worker
!python -m pip install -r requirements.txt
!python -m train.train_calibrex_detector \
  --sku110k /content/drive/MyDrive/calibrex/SKU-110K \
  --synthetic /content/drive/MyDrive/calibrex/synthetic \
  --real /content/drive/MyDrive/calibrex/real-yolo \
  --eval-data /content/drive/MyDrive/calibrex/real-eval \
  --data /content/drive/MyDrive/calibrex/calibrex.yaml \
  --samples-per-epoch 10000 --device 0 --low-vram \
  --project /content/drive/MyDrive/calibrex/runs --name calibrex-retail-item
```

Omit any source option whose data is not available. Run once with `--dry-run` before training. Colab runtimes are temporary; copy `weights/last.pt` and `weights/best.pt` to Drive between sessions. To resume, use `--model <Drive path to last.pt> --resume` and retain the same run name and project.

### Kaggle notebook

Attach the prepared datasets as Kaggle notebook inputs and enable a GPU accelerator. Kaggle input folders are read-only; write checkpoints to `/kaggle/working` and download or save them as notebook output before the session ends.

```bash
!git clone <YOUR_REPOSITORY_URL> /kaggle/working/calibrex
%cd /kaggle/working/calibrex/ai-worker
!python -m pip install -r requirements.txt
!python -m train.train_calibrex_detector \
  --sku110k /kaggle/input/sku110k \
  --synthetic /kaggle/input/calibrex-synthetic \
  --real /kaggle/input/calibrex-real-yolo \
  --eval-data /kaggle/input/calibrex-real-eval \
  --data /kaggle/working/calibrex.yaml \
  --samples-per-epoch 10000 --device 0 --low-vram \
  --project /kaggle/working/runs --name calibrex-retail-item
```

Replace or omit input paths to match the datasets attached to the notebook. Run `--dry-run` first. For a resumed session, upload the saved `last.pt` as a notebook input and pass it with `--model /kaggle/input/<checkpoint-folder>/last.pt --resume`; use a fresh writable project path if the previous run directory is unavailable.

To resume from a YOLO checkpoint, use the checkpoint as `--model` with `--resume` and the same project/run configuration:

```bash
python -m train.train_calibrex_detector \
  --data /data/calibrex/calibrex.yaml \
  --model runs/detect/calibrex-retail-item/weights/last.pt \
  --resume --device 0 --name calibrex-retail-item
```

No full training or GPU smoke run has been performed as part of this change. If your training environment has enough GPU memory and a tiny dataset, you may first run one epoch there; it is a plumbing check, not a quality result.

## 5. Evaluate and generate results

Run the same evaluation set with the H5 alternative and any comparison checkpoints. `--weights` selects the exact YOLO file to evaluate; the worker does not substitute weights when a path is missing.

```bash
cd ai-worker
python -m eval.phase_a check --data ./eval_set

DETECTOR_BACKEND=h5 python -m eval.phase_a run \
  --data ./eval_set --backend h5 --label h5 --out report-h5.json

python -m eval.phase_a run --data ./eval_set --backend yolo \
  --weights runs/detect/runs/detect/calibrex_s960/weights/best.pt \
  --label calibrex-yolo --out report-yolo-calibrex.json

python -m eval.phase_a report --data ./eval_set \
  --reports 'report-h5.json,report-yolo-calibrex.json' \
  --out ../docs/RESULTS.md
```

The bundled set is synthetic; its results are pipeline checks only and cannot establish shelf accuracy or authorize a model switch. Also evaluate a separately labeled, real held-out shelf set that was not used for training or validation:

```bash
python -m eval.phase_a check --data ./real_eval_set
python -m eval.phase_a run --data ./real_eval_set --backend h5 --label h5-real --out report-h5-real.json
python -m eval.phase_a run --data ./real_eval_set --backend yolo --weights \
  runs/detect/runs/detect/calibrex_s960/weights/best.pt --label calibrex-yolo --out report-yolo-real.json
python -m eval.phase_a report --data ./real_eval_set --reports 'report-*-real.json' --out ../docs/RESULTS.md
```

The report includes the large-item band (median labeled box area at least 10% of the image), detection precision/recall, boxes per item, fragment rate, and mean best IoU for large items. The comparison checks large recall >=0.90, large fragment rate <=0.10, dense recall within two percentage points of H5, and SURE precision no worse than H5. These are suggestions only and do not change the configured detector. The currently selected Calibrex checkpoint is not an accuracy guarantee.

## 6. Export, verify, and roll back

Keep the trained checkpoint separate from `iou_resnet50_csv_06.h5` and any comparison weights. The current Scan workflow checkpoint is `runs/detect/runs/detect/calibrex_s960/weights/best.pt`:

```bash
mkdir -p models
test -s runs/detect/runs/detect/calibrex_s960/weights/best.pt
sha256sum runs/detect/runs/detect/calibrex_s960/weights/best.pt
```

The worker image includes this checkpoint at `/srv/calibrex-best.pt`. To use a different YOLO checkpoint, make it available to the worker and set `YOLO_WEIGHTS` to its path; missing or invalid weights stop worker startup.

```bash
docker compose cp /path/to/other-best.pt ai-worker:/models/other-best.pt
```

Set `YOLO_WEIGHTS=/models/other-best.pt` in the project-root `.env` and recreate the AI worker to load that checkpoint. To select H5 instead, set `DETECTOR_BACKEND=h5` and recreate the worker; H5 remains an explicit alternative.

## Current environment and limitations

- No local NVIDIA driver/CUDA device is available; no long training job was run.
- The current project filesystem had about 7 GB free when checked, less than the SKU-110K download alone.
- There is no local full-catalog packshot folder or real shelf training/evaluation set in the repository. Supply those external folders before training and before making quality claims.
- No real held-out shelf metrics are included until the evaluation commands above produce reports from such a dataset.
