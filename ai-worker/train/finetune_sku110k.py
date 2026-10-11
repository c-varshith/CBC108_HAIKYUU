"""Fine-tune YOLO11 as a class-agnostic 'retail item' box detector on SKU-110K (Ultralytics ships the dataset config and
downloads ~13 GB on first use). One class, boxes only: SKU identity comes from DINOv2 + Qdrant, never from this model.

  python -m train.finetune_sku110k --model yolo11m.pt --epochs 100 --imgsz 1280 --batch 8 --device 0
Result: runs/detect/calibrex/weights/best.pt  ->  copy to ./models/yolo11-sku110k.pt

Notes: dense shelves need a high max_det and imgsz>=1280. mosaic stays on (SKU-110K is dense already), flipud off
(shelves have a fixed up direction), degrees small. Add 50-200 of YOUR phone photos (blur, angle, glare) to a second
fine-tune stage if recall on phone-quality images lags the SKU-110K validation set."""
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="yolo11m.pt"); ap.add_argument("--data", default="SKU-110K.yaml")
    ap.add_argument("--epochs", type=int, default=100); ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--batch", type=int, default=8); ap.add_argument("--device", default="0")
    ap.add_argument("--name", default="calibrex")
    a = ap.parse_args()
    from ultralytics import YOLO

    m = YOLO(a.model)
    m.train(data=a.data, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch, device=a.device, name=a.name, single_cls=True,
            max_det=1000, flipud=0.0, fliplr=0.5, degrees=3.0, perspective=0.0005, hsv_h=0.015, patience=20, cos_lr=True)
    metrics = m.val(data=a.data, imgsz=a.imgsz, max_det=1000, device=a.device, conf=0.001)
    print(f"mAP50={metrics.box.map50:.3f}  mAP50-95={metrics.box.map:.3f}  recall={metrics.box.mr:.3f}  precision={metrics.box.mp:.3f}")


if __name__ == "__main__":
    main()
