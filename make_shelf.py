"""Make a fake shelf photo from your product images, plus an answer key (<name>.truth.csv).

  python tools/make_shelf.py /home/varshith/Downloads/images --count 40 --copies 2 --degrade -o shelf_1.jpg
  Tip: --only import/barcodes.csv  keeps products to the ones you imported.
"""
import argparse, csv, random
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument("folder", type=Path)
ap.add_argument("-o", "--out", type=Path, default=Path("shelf.jpg"))
ap.add_argument("--count", type=int, default=40, help="how many different products")
ap.add_argument("--copies", type=int, default=1, help="copies of each product (tests counting)")
ap.add_argument("--width", type=int, default=2400)
ap.add_argument("--height", type=int, default=320, help="height of each product")
ap.add_argument("--degrade", action="store_true", help="make photos a bit worse, like a real camera")
ap.add_argument("--only", type=Path, help="CSV of barcodes: only use these products")
ap.add_argument("--seed", type=int, default=1)
a = ap.parse_args()
random.seed(a.seed)

files = sorted(p for p in a.folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
if a.only:
    keep = {r[0].strip() for r in csv.reader(a.only.open()) if r}
    files = [f for f in files if f.stem in keep]
print(f"found {len(files)} usable image files in {a.folder.resolve()}")
if not files:
    raise SystemExit("No images found. Check the folder path.")
random.shuffle(files)
items = [f for f in files[:a.count] for _ in range(a.copies)]
random.shuffle(items)


def prepare(f):
    try:
        im = Image.open(f).convert("RGB")
    except Exception as e:
        print("skipping unreadable file", f.name, e)
        return None
    h = int(a.height * (random.uniform(0.9, 1.1) if a.degrade else 1))
    im = im.resize((max(1, int(im.width * h / im.height)), h))
    if a.degrade:
        im = ImageEnhance.Brightness(im).enhance(random.uniform(0.8, 1.2))
        im = ImageEnhance.Contrast(im).enhance(random.uniform(0.85, 1.15))
        small = im.resize((max(1, im.width // 2), max(1, im.height // 2)))
        im = small.resize(im.size).filter(ImageFilter.GaussianBlur(random.uniform(0, 1)))
    return f.stem, im


M, GAP, ROW_H, SHELF = 30, 14, int(a.height * 1.15), 26
rows, row, x = [], [], M
for f in items:
    p = prepare(f)
    if p is None:
        continue
    if x + p[1].width > a.width - M and row:
        rows.append(row); row, x = [], M
    row.append(p); x += p[1].width + GAP
if row:
    rows.append(row)
if not rows:
    raise SystemExit("None of the selected files could be opened as images.")

canvas = Image.new("RGB", (a.width, len(rows) * (ROW_H + SHELF) + 20), (232, 232, 232))
draw = ImageDraw.Draw(canvas)
truth = []
for r, row in enumerate(rows):
    base = 20 + r * (ROW_H + SHELF) + ROW_H
    draw.rectangle([0, base, a.width, base + SHELF], fill=(150, 110, 70))
    x = M
    for code, im in row:
        canvas.paste(im, (x, base - im.height))
        truth.append((code, x, base - im.height, x + im.width, base))
        x += im.width + GAP

canvas.save(a.out, quality=92)
key = a.out.with_suffix(".truth.csv")
with open(key, "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["barcode", "x1", "y1", "x2", "y2"]); w.writerows(truth)
print(f"{len(truth)} products on {len(rows)} shelves -> {a.out} (+ {key})")
