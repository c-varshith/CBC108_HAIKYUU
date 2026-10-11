"""Download and replace the default organization's catalog from a local OFF TSV dump.

Preview:  python -I scripts/refresh_off_catalog.py --dry-run
Apply:    python -I scripts/refresh_off_catalog.py --replace-catalog

The apply command stages 2,000 valid front images before it changes catalog data. It downloads
only from images.openfoodfacts.org and never calls the Open Food Facts product API.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import html
import io
import json
import logging
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Running as `python -I scripts/refresh_off_catalog.py` deliberately excludes cwd from sys.path.
BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain import catalog as cat  # noqa: E402
from app.domain.common import Actor, audit  # noqa: E402
from app.infra.imaging import ImageInfo, ImageRejected, ainspect_image  # noqa: E402
from app.infra.inference import InferenceClient, make_inference  # noqa: E402
from app.infra.storage import Storage, make_storage  # noqa: E402
from app.models import ExpectedStock, Organization, Product, ReferenceImage  # noqa: E402
from scripts.import_catalog import ImportOptions, Importer, OffClient  # noqa: E402

log = logging.getLogger("calibrex.refresh")
BARCODE_MIN, BARCODE_MAX = 8, 14
IMAGE_HOST = "images.openfoodfacts.org"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MIN_IMAGE_SIDE = 200
DOWNLOAD_CONCURRENCY = 4
DOWNLOAD_BATCH_SIZE = 12
DOWNLOAD_RETRIES = 3
LICENSE = "CC BY-SA 3.0"

# Explicit category allow terms: the OFF packaging columns are absent for most products, so
# category membership is a conservative proxy. Fresh raw produce and non-food classifications
# take precedence over a positive category match.
PACKAGED_FOOD_HINTS = (
    "beverage", "drink", "snack", "cereal", "breakfast", "biscuit", "cake", "bakery",
    "bread", "confection", "chocolate", "candy", "sweet", "dairy", "milk", "cheese", "yogurt",
    "pasta", "noodle", "sauce", "condiment", "canned", "prepared-meal", "ready-meal", "spread",
    "coffee", "tea", "juice", "oil", "spice", "flour", "rice", "pulse", "legume", "nut", "seed",
    "frozen-food", "plant-based", "potato-products", "preserve", "jam", "honey",
)
FRESH_PRODUCE_TAGS = {
    "fruit", "fruits", "vegetable", "vegetables", "fresh-fruit", "fresh-fruits",
    "fresh-vegetable", "fresh-vegetables", "fresh-food", "fresh-foods", "raw-produce",
    "fresh-produce", "herb", "herbs", "culinary-plants",
    "banana", "bananas", "apple", "apples", "orange", "oranges", "mango", "mangoes",
    "tomato", "tomatoes", "potato", "potatoes", "onion", "onions", "carrot", "carrots",
}
NON_FOOD_HINTS = (
    "cosmetic", "body-care", "pet-food", "supplement", "household", "cleaning", "hygiene",
    "medical", "beauty", "toiletries", "food-contact-material", "alcoholic-beverage", "spirits",
    "brandy", "wine", "beer", "liquor",
)
NON_FOOD_NAME_HINTS = ("herbalife", "dietary supplement", "food supplement", "protein supplement")
RAW_PRODUCE_NAMES = {
    "ginger", "coriander", "cilantro", "coriander leaves", "curry leaves", "garlic", "onion", "tomato",
    "potato", "carrot", "cucumber", "spinach", "lettuce", "cabbage", "cauliflower", "broccoli",
    "banana", "apple", "orange", "mango", "lemon", "lime", "grapes", "watermelon",
}
PLACEHOLDER_IMAGE_PATHS = {"invalid", "unknown", "placeholder", "no-image"}
IMAGE_EXTENSIONS = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp", "HEIF": ".heic"}


class RefreshError(RuntimeError):
    """A safe-to-report refresh failure."""


@dataclass(frozen=True)
class Candidate:
    barcode: str
    name: str
    brand: str
    quantity: str
    image_url: str


@dataclass
class SelectionReport:
    selected: list[Candidate]
    counts: Counter[str]
    rows_read: int


def open_dump(path: Path):
    return gzip.open(path, "rt", encoding="utf-8-sig", newline="") if path.suffix.lower() == ".gz" else path.open(
        "r", encoding="utf-8-sig", newline="")


def _tags(row: dict[str, str]) -> list[str]:
    raw = ",".join((row.get("categories_tags") or "", row.get("categories_en") or "",
                    row.get("food_groups_tags") or "", row.get("food_groups_en") or ""))
    out = []
    for value in raw.split(","):
        tag = value.strip().casefold()
        if not tag:
            continue
        if ":" in tag:
            tag = tag.split(":", 1)[1]
        out.append(tag.replace("_", "-").replace(" ", "-"))
    return out


def country_matches(row: dict[str, str], country: str = "India") -> bool:
    expected = country.strip().casefold().replace("_", "-")
    tags = {tag.strip().casefold() for tag in (row.get("countries_tags") or "").split(",")}
    names = {name.strip().casefold().replace("_", "-") for name in (row.get("countries_en") or "").split(",")}
    return expected in tags or f"en:{expected}" in tags or expected.replace("-", " ") in names


def is_packaged_food(row: dict[str, str]) -> tuple[bool, str]:
    """Return category eligibility and a stable reason useful in the preview report."""
    name = html.unescape((row.get("product_name") or row.get("product_name_en") or "").strip()).casefold()
    brand = (row.get("brands") or "").casefold()
    if name in RAW_PRODUCE_NAMES:
        return False, "raw_produce_name"
    if any(hint in f"{name} {brand}" for hint in NON_FOOD_NAME_HINTS):
        return False, "non_food_or_supplement_name"
    tags = _tags(row)
    if not tags:
        return False, "no_food_category"
    if any(tag in FRESH_PRODUCE_TAGS for tag in tags):
        return False, "fresh_or_raw_produce"
    if any(hint in tag for tag in tags for hint in NON_FOOD_HINTS):
        return False, "non_food_or_supplement"
    if any(hint in tag for tag in tags for hint in PACKAGED_FOOD_HINTS):
        return True, "eligible_category"
    return False, "category_not_in_packaged_food_groups"


def candidate_from_row(row: dict[str, str]) -> tuple[Candidate | None, str]:
    code = (row.get("code") or "").strip()
    if not code.isdigit() or not BARCODE_MIN <= len(code) <= BARCODE_MAX:
        return None, "invalid_barcode"
    name = html.unescape((row.get("product_name") or row.get("product_name_en") or "").strip())
    if not name:
        return None, "missing_name"
    url = (row.get("image_front_url") or row.get("image_url") or "").strip()
    if not url:
        return None, "missing_image_url"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != IMAGE_HOST or not parts.path.startswith("/"):
        return None, "invalid_image_url"
    if PLACEHOLDER_IMAGE_PATHS.intersection(segment.casefold() for segment in parts.path.split("/")):
        return None, "placeholder_image_url"
    eligible, reason = is_packaged_food(row)
    if not eligible:
        return None, reason
    return Candidate(code, name[:200], html.unescape((row.get("brands") or "").split(",")[0].strip()),
                     (row.get("quantity") or "").strip(), url), reason


def iter_candidates(path: Path, *, country: str = "India", counts: Counter[str] | None = None) -> Iterator[Candidate]:
    """Yield first qualifying rows in dump order; retain only a bounded number of fields per row."""
    counts = counts if counts is not None else Counter()
    csv.field_size_limit(16 * 1024 * 1024)
    with open_dump(path) as fh:
        rows = csv.DictReader(fh, delimiter="\t")
        required = {"code"}
        if not rows.fieldnames or not required.issubset(rows.fieldnames):
            raise RefreshError("Expected an OFF tab-separated dump with a 'code' column.")
        if not {"countries_tags", "countries_en"}.intersection(rows.fieldnames):
            raise RefreshError("The OFF dump must include countries_tags or countries_en.")
        if not {"categories_tags", "categories_en", "food_groups_tags", "food_groups_en"}.intersection(rows.fieldnames):
            raise RefreshError("The OFF dump has no food-category columns; refusing an unfiltered catalog import.")
        seen: set[str] = set()
        for row in rows:
            counts["rows_read"] += 1
            if not country_matches(row, country):
                counts["other_country"] += 1
                continue
            code = (row.get("code") or "").strip()
            if code in seen:
                counts["duplicate_barcode"] += 1
                continue
            candidate, reason = candidate_from_row(row)
            counts[reason] += 1
            if candidate is None:
                continue
            seen.add(code)
            counts["metadata_eligible"] += 1
            yield candidate


async def _request_image(client: httpx.AsyncClient, url: str, *, max_bytes: int, retries: int,
                         sleep: Callable[[float], object] = asyncio.sleep) -> bytes:
    current = url
    for redirect in range(4):
        parts = urlsplit(current)
        if parts.scheme != "https" or parts.hostname != IMAGE_HOST:
            raise RefreshError(f"Image URL is outside the allowed OFF image host: {current}")
        for attempt in range(retries + 1):
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirect == 3:
                            raise RefreshError("Image URL redirected too many times.")
                        current = urljoin(current, location)
                        break
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt == retries:
                            raise RefreshError(f"Image server returned HTTP {response.status_code} after retries.")
                        retry_after = response.headers.get("retry-after", "")
                        delay = min(float(retry_after), 30.0) if retry_after.isdigit() else 0.5 * (2 ** attempt)
                        await sleep(delay)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type and not content_type.startswith("image/"):
                        raise RefreshError(f"Expected an image response, received {content_type}.")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise RefreshError(f"Image exceeds the {max_bytes} byte limit.")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise RefreshError(f"Image exceeds the {max_bytes} byte limit.")
                    return bytes(data)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise RefreshError(f"Image download failed after retries: {exc}") from exc
                await sleep(0.5 * (2 ** attempt))
        else:
            raise RefreshError("Image request retry loop ended unexpectedly.")
        continue
    raise RefreshError("Image URL redirected too many times.")


async def download_and_validate(candidate: Candidate, client: httpx.AsyncClient, *, max_bytes: int = MAX_IMAGE_BYTES,
                                min_side: int = MIN_IMAGE_SIDE, retries: int = DOWNLOAD_RETRIES,
                                sleep: Callable[[float], object] = asyncio.sleep) -> tuple[bytes, str, ImageInfo]:
    data = await _request_image(client, candidate.image_url, max_bytes=max_bytes, retries=retries, sleep=sleep)
    info = await ainspect_image(data)
    if min(info.width, info.height) < min_side:
        raise RefreshError(f"Image is {info.width}x{info.height}; minimum short side is {min_side}px.")
    if info.flat:
        raise RefreshError("Image is blank or a flat placeholder.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            extension = IMAGE_EXTENSIONS.get(image.format or "")
    except (OSError, ValueError) as exc:
        raise RefreshError("Image bytes could not be decoded.") from exc
    if not extension:
        raise RefreshError("Image format is not supported by the catalog importer.")
    return data, extension, info


async def stage_images(candidates: Iterator[Candidate], stage_dir: Path, *, target: int,
                       client: httpx.AsyncClient, concurrency: int = DOWNLOAD_CONCURRENCY,
                       retries: int = DOWNLOAD_RETRIES, min_side: int = MIN_IMAGE_SIDE,
                       max_bytes: int = MAX_IMAGE_BYTES,
                       sleep: Callable[[float], object] = asyncio.sleep) -> tuple[list[Candidate], Counter[str]]:
    """Download in bounded batches; return exactly target valid, unique products or a shortfall report."""
    if target < 1 or concurrency < 1:
        raise ValueError("target and concurrency must be positive")
    stage_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    chosen: list[Candidate] = []
    counts: Counter[str] = Counter()
    hashes: set[str] = set()
    phashes = cat.PhashIndex(max_distance=2)
    exhausted = False
    while len(chosen) < target and not exhausted:
        batch: list[Candidate] = []
        batch_size = min(concurrency * 3, target - len(chosen))
        for _ in range(batch_size):
            try:
                batch.append(next(candidates))
            except StopIteration:
                exhausted = True
                break
        if not batch:
            break

        async def one(item: Candidate):
            async with semaphore:
                try:
                    return await download_and_validate(item, client, max_bytes=max_bytes, min_side=min_side,
                                                        retries=retries, sleep=sleep)
                except (RefreshError, ImageRejected, httpx.HTTPError) as exc:
                    return exc

        results = await asyncio.gather(*(one(item) for item in batch))
        for item, result in zip(batch, results):
            if isinstance(result, Exception):
                counts["image_rejected"] += 1
                continue
            data, extension, info = result
            digest = info.sha256
            if digest in hashes:
                counts["duplicate_image"] += 1
                continue
            duplicate = phashes.find(digest, info.phash)
            if duplicate:
                counts["near_duplicate_image"] += 1
                continue
            path = stage_dir / f"{item.barcode}{extension}"
            path.write_bytes(data)
            chosen.append(item)
            hashes.add(digest)
            phashes.add(item.barcode, digest, info.phash)
            counts["downloaded"] += 1
            if len(chosen) == target:
                break
    return chosen, counts


async def reset_catalog(sm: async_sessionmaker[AsyncSession], storage: Storage, inference: InferenceClient,
                        org_slug: str, settings) -> dict[str, int]:
    """Remove one org's product vectors, stored photos, and catalog rows while preserving scans/users/audit."""
    async with sm() as db:
        org = await db.scalar(select(Organization).where(Organization.slug == org_slug))
        if org is None:
            raise RefreshError(f"Organization '{org_slug}' was not found.")
        org_id = org.id
        products = (await db.execute(select(Product.id, Product.sku, Product.name).where(Product.org_id == org_id))).all()
        ids = [pid for pid, _, _ in products]
        skus = [sku for _, sku, _ in products]
        for start in range(0, len(skus), 500):
            overlaps = await db.execute(select(Product.sku).where(Product.org_id != org_id,
                Product.sku_key.in_([sku.lower() for sku in skus[start:start + 500]])))
            conflict_skus = sorted({sku for (sku,) in overlaps})
            if conflict_skus:
                raise RefreshError("Catalog reset would remove worker vectors shared with another organization; overlapping SKUs: "
                                   + ", ".join(conflict_skus[:20]))
        refs = (await db.execute(select(ReferenceImage.product_id, ReferenceImage.storage_key, ReferenceImage.thumb_key)
                                 .where(ReferenceImage.product_id.in_(ids)))).all() if ids else []

    # Remove vectors before database rows, and restore any already removed vectors if a later delete fails.
    removed: list[str] = []
    for sku in skus:
        # Include the current SKU in rollback: a network timeout can happen after the worker
        # deleted it but before the HTTP response reached us.
        removed.append(sku)
        try:
            await inference.remove_product(sku)
        except Exception as exc:
            await _restore_worker_products(sm, storage, inference, settings, products, removed)
            raise RefreshError(f"Worker cleanup failed; database catalog was not deleted: {exc}") from exc

    # Keep backups on disk rather than holding an entire established catalog's photos in memory.
    with tempfile.TemporaryDirectory(prefix="calibrex-catalog-reset-") as backup_root:
        backup_dir = Path(backup_root)
        deleted_keys: dict[str, Path] = {}
        try:
            keys = list(dict.fromkeys(key for _, ref_key, thumb_key in refs for key in (ref_key, thumb_key)))
            for number, key in enumerate(keys):
                data = await storage.get(key)
                backup = backup_dir / str(number)
                backup.write_bytes(data)
                deleted_keys[key] = backup
                await storage.delete(key)
            async with sm() as db:
                # Expected-stock rows are keyed by SKU rather than product FK, so clear stale counts too.
                await db.execute(delete(ExpectedStock).where(ExpectedStock.org_id == org_id))
                if ids:
                    await db.execute(delete(Product).where(Product.id.in_(ids)))
                audit(db, Actor(None, "OFF catalog refresh", None, org_id), "CATALOG_REPLACED", org_slug,
                      after={"products_removed": len(ids), "references_removed": len(refs), "expected_stock_cleared": True})
                await db.commit()
        except Exception as exc:
            for key, backup in deleted_keys.items():
                try:
                    await storage.put(key, backup.read_bytes(), "image/jpeg")
                except Exception:
                    log.exception("Could not restore catalog image %s after reset failure", key)
            await _restore_worker_products(sm, storage, inference, settings, products, removed)
            raise RefreshError(f"Photo or database cleanup failed; database catalog was not deleted: {exc}") from exc
    return {"products_removed": len(ids), "references_removed": len(refs), "worker_skus_removed": len(skus)}


async def _restore_worker_products(sm, storage, inference, settings, products, skus: list[str]) -> None:
    for sku in skus:
        product_row = next((row for row in products if row[1] == sku), None)
        if product_row is None:
            continue
        try:
            async with sm() as db:
                product = await db.get(Product, product_row[0])
                items = await cat.product_items(db, storage, product)
            if items:
                await cat.send_references(inference, settings, sku, product_row[2], items)
        except Exception:
            log.exception("Could not restore vector references for %s after reset failure", sku)


async def apply_refresh(args) -> int:
    settings = get_settings()
    if settings.inference_backend != "http":
        raise RefreshError("INFERENCE_BACKEND is set to 'stub'. Start the AI worker, then set INFERENCE_BACKEND=http, "
                           "AI_WORKER_URL, DATABASE_URL, and STORAGE_DIR to the same values used by the API before applying.")
    if args.org != "default":
        raise RefreshError("This refresh tool is intentionally scoped to the default organization.")
    dump = args.dump.resolve()
    if not dump.is_file():
        raise RefreshError(f"OFF dump not found: {dump}")
    image_dir = args.images_dir.resolve()
    image_dir.mkdir(parents=True, exist_ok=True)
    stage_parent = image_dir.parent
    stage_path = Path(tempfile.mkdtemp(prefix=".off-catalog-stage-", dir=stage_parent))
    try:
        counts: Counter[str] = Counter()
        candidates = iter_candidates(dump, country="India", counts=counts)
        timeout = httpx.Timeout(30.0, connect=10.0)
        limits = httpx.Limits(max_connections=DOWNLOAD_CONCURRENCY, max_keepalive_connections=DOWNLOAD_CONCURRENCY)
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
            selected, download_counts = await stage_images(candidates, stage_path, target=args.limit, client=client)
        counts.update(download_counts)
        if len(selected) != args.limit:
            raise RefreshError(f"Only {len(selected)} products had valid, distinct images; required {args.limit}. Catalog was not changed.")

        # Keep a machine-readable import list beside the downloaded front images for recovery/resume.
        manifest_path = stage_path / "stocked.csv"
        with manifest_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["barcode"])
            writer.writerows((item.barcode,) for item in selected)
        (stage_path / "refresh.json").write_text(json.dumps({"org": args.org, "dump": str(dump), "count": len(selected),
            "license": LICENSE, "image_host": IMAGE_HOST}, indent=2), encoding="utf-8")

        dbmod.init_engine(settings)
        inference = make_inference(settings)
        storage = make_storage(settings)
        try:
            info = await inference.info()
            if not await inference.ping() or not info.get("collection"):
                raise RefreshError("The AI worker is not ready to receive product references.")
            if not await storage.ping():
                raise RefreshError("Catalog storage is not reachable; existing catalog was not changed.")
            # Validate org and cross-org SKU safety before moving staged files or deleting catalog rows.
            await _validate_reset_scope(dbmod.sessionmaker(), args.org, [item.barcode for item in selected])
            # Publish staged images in the dedicated cache. Existing unrelated files are left alone.
            for item in selected:
                matches = list(stage_path.glob(f"{item.barcode}.*"))
                if not matches:
                    raise RefreshError(f"Staged image disappeared for {item.barcode}.")
                shutil.copy2(matches[0], image_dir / matches[0].name)
            shutil.copy2(manifest_path, image_dir / manifest_path.name)
            shutil.copy2(stage_path / "refresh.json", image_dir / "refresh.json")

            removed = await reset_catalog(dbmod.sessionmaker(), storage, inference, args.org, settings)
            log.info("Removed old catalog: %s", removed)
            options = ImportOptions(org_slug=args.org, barcodes=[item.barcode for item in selected],
                                    catalog_dump=dump, images_dir=image_dir, local_only=True, min_side=MIN_IMAGE_SIDE)
            async with httpx.AsyncClient(timeout=30) as no_off_client:
                importer = Importer(options, dbmod.sessionmaker(), storage, inference, settings, OffClient(no_off_client))
                report = await importer.run()
            print("Image stage:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            print("Catalog reset:", ", ".join(f"{k}={v}" for k, v in sorted(removed.items())))
            print(f"Import batch {report.batch_id}: " + ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items())))
            if report.counts.get("created", 0) != args.limit or report.counts.get("indexed", 0) != args.limit:
                print("Catalog refresh completed with indexing/import failures; inspect the batch errors before retrying.", file=sys.stderr)
                return 1
            return 0
        finally:
            await dbmod.dispose_engine()
    finally:
        shutil.rmtree(stage_path, ignore_errors=True)


async def _validate_reset_scope(sm: async_sessionmaker[AsyncSession], org_slug: str, new_skus: list[str]) -> None:
    async with sm() as db:
        org = await db.scalar(select(Organization).where(Organization.slug == org_slug))
        if org is None:
            raise RefreshError(f"Organization '{org_slug}' was not found.")
        for start in range(0, len(new_skus), 500):
            chunk = [sku.lower() for sku in new_skus[start:start + 500]]
            overlap = await db.execute(select(Product.sku).where(Product.org_id != org.id, Product.sku_key.in_(chunk)))
            conflicts = sorted({sku for (sku,) in overlap})
            if conflicts:
                raise RefreshError("New catalog SKUs overlap another organization and worker references are not tenant-scoped: "
                                   + ", ".join(conflicts[:20]))


async def preview(args) -> int:
    counts: Counter[str] = Counter()
    selected: list[Candidate] = []
    for candidate in iter_candidates(args.dump, country="India", counts=counts):
        selected.append(candidate)
        if len(selected) >= args.limit:
            break
    print(f"Preview: {len(selected)} category-eligible India products found in first qualifying rows (target {args.limit}).")
    print("Selection counts:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for item in selected[:10]:
        print(f"{item.barcode}\t{item.name}\t{item.brand}\t{item.quantity}\t{item.image_url}")
    print("Preview does not download images or change the database. Apply with --replace-catalog after reviewing the sample.")
    return 0 if len(selected) == args.limit else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, default=ROOT_DIR / "en.openfoodfacts.org.products.csv", help="local OFF TSV or .gz dump")
    parser.add_argument("--images-dir", type=Path, default=BACKEND_DIR / "data" / "off-images", help="download cache shared with backend storage")
    parser.add_argument("--org", default="default", help="organization to replace (only default is supported for safety)")
    parser.add_argument("--limit", type=int, default=2000, help="exact number of indexed products required")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true", help="preview selection; no downloads or database changes")
    action.add_argument("--replace-catalog", action="store_true", help="download, purge default catalog, and import fresh products")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        return asyncio.run(preview(args) if args.dry_run else apply_refresh(args))
    except (RefreshError, OSError, ValueError) as exc:
        print(f"Refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
