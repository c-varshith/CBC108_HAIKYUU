"""Step B: build the two small files the importer needs, for exactly the images you downloaded.

Reads the big OFF dump ONCE (streaming, low memory) and writes:
  import/barcodes.csv    barcodes of every image in your folder
  import/mini_dump.tsv   name/brand/quantity for those barcodes, in the same format as the OFF dump

  python tools/prepare_import.py /home/varshith/Downloads/images en.openfoodfacts.org.products.csv.gz
"""
import csv, gzip, sys
from pathlib import Path

EXTS = {".jpg", ".jpeg", ".png", ".webp"}
COLS = ["code", "product_name", "product_name_en", "brands", "quantity", "image_url"]

images, dump = Path(sys.argv[1]), Path(sys.argv[2])
out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("import")
out.mkdir(exist_ok=True)

wanted = sorted({p.stem for p in images.iterdir() if p.suffix.lower() in EXTS and p.stem.isdigit() and 8 <= len(p.stem) <= 14})
print(f"{len(wanted)} barcodes found in {images}")
if not wanted:
    raise SystemExit("No <barcode>.jpg files found. Check the images folder path.")
want = set(wanted)

csv.field_size_limit(16 * 1024 * 1024)
opener = gzip.open if dump.suffix == ".gz" else open
found = {}
with opener(dump, "rt", encoding="utf-8-sig", newline="") as fh:
    for i, row in enumerate(csv.DictReader(fh, delimiter="\t"), 1):
        code = (row.get("code") or "").strip()
        if code in want and code not in found:
            found[code] = [(row.get(c) or "").strip() for c in COLS]
            if len(found) == len(want):
                break
        if i % 500000 == 0:
            print(f"  read {i:,} rows, matched {len(found)}/{len(want)}")

with open(out / "barcodes.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["barcode"]); w.writerows([[b] for b in wanted])
with open(out / "mini_dump.tsv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, delimiter="\t"); w.writerow(COLS); w.writerows(found.values())
print(f"matched {len(found)} of {len(want)} -> {out}/barcodes.csv and {out}/mini_dump.tsv")
if len(found) < len(want):
    print(f"{len(want) - len(found)} images have no row in the dump; the importer will skip them.")
