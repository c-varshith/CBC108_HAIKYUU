"""Step A: extract image links from the OFF dump, then download them as <barcode>.jpg.

  python tools/fetch_off_images.py extract en.openfoodfacts.org.products.csv.gz --country India -o image_urls.tsv
  python tools/fetch_off_images.py download image_urls.tsv /home/varshith/Downloads/images
"""
import argparse, csv, gzip, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {"User-Agent": "CALIBREX-image-fetch/1.0 (your@email.com)"}  # <-- put your own email here


def extract(dump, out_tsv, country, limit):
    csv.field_size_limit(16 * 1024 * 1024)
    opener = gzip.open if dump.suffix == ".gz" else open
    n = 0
    with opener(dump, "rt", encoding="utf-8-sig", newline="") as fh, open(out_tsv, "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out, delimiter="\t")
        w.writerow(["code", "image_url"])
        for row in csv.DictReader(fh, delimiter="\t"):
            code = (row.get("code") or "").strip()
            url = (row.get("image_url") or "").strip()
            if not (code.isdigit() and 8 <= len(code) <= 14 and url) or "/invalid/" in url:
                continue
            if country and f"en:{country.lower()}" not in (row.get("countries_tags") or "").lower().split(","):
                continue
            w.writerow([code, url])
            n += 1
            if limit and n >= limit:
                break
    print(f"saved {n} image links to {out_tsv}")


def fetch_one(args):
    code, url, folder, delay = args
    dest = folder / f"{code}.jpg"
    if dest.exists():
        return "skip"
    for attempt in range(3):
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read()
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.rename(dest)
            time.sleep(delay)
            return "ok"
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # permanent: retrying will not help
                return f"failed {code} {url} HTTP {e.code}"
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return f"failed {code} {url}"


def download(tsv, folder, workers, delay):
    folder.mkdir(parents=True, exist_ok=True)
    with open(tsv, encoding="utf-8", newline="") as fh:
        jobs = [(r["code"], r["image_url"], folder, delay) for r in csv.DictReader(fh, delimiter="\t")]
    counts = {"ok": 0, "skip": 0, "failed": 0}
    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(fetch_one, jobs), 1):
            key = res.split()[0]
            counts[key] += 1
            if key == "failed":
                print(res)
            if i % 100 == 0:
                print(f"{i}/{len(jobs)}  {counts}")
    print("done", counts)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("dump", type=Path)
    e.add_argument("-o", "--out", type=Path, default=Path("image_urls.tsv"))
    e.add_argument("--country")
    e.add_argument("--limit", type=int)
    d = sub.add_parser("download")
    d.add_argument("tsv", type=Path)
    d.add_argument("folder", type=Path)
    d.add_argument("--workers", type=int, default=4)
    d.add_argument("--delay", type=float, default=0.3)
    a = p.parse_args()
    extract(a.dump, a.out, a.country, a.limit) if a.cmd == "extract" else download(a.tsv, a.folder, a.workers, a.delay)
