"""Bulk-load a product catalog from Open Food Facts (OFF) into the database AND the AI worker's Qdrant index.

    python -m scripts.import_catalog --barcodes-file stocked.csv --contact you@example.com
    python -m scripts.import_catalog --country france --category "Orange Juice" --limit 500 --contact you@example.com
    python -m scripts.import_catalog --barcodes-file stocked.csv --images-dir /data/off-images --contact you@example.com
    python -m scripts.import_catalog --barcodes-file stocked.csv --dry-run --contact you@example.com

Registering a product = storing its reference photo + sending it to the worker, which embeds it into Qdrant. No retraining.
Uses the domain layer directly (not HTTP). Idempotent and resumable: a re-run skips SKUs that are done, retries the ones whose
image or worker call failed. SKU = barcode.

OFF etiquette (https://openfoodfacts.github.io/openfoodfacts-server/api/): send a User-Agent `AppName/Version (contact)`;
product reads are limited to 15/min and searches to 10/min per IP (the defaults here stay below that). For more than a few
hundred products OFF asks you to use its data dumps / image dataset instead: pass pre-downloaded images with --images-dir
and import only the barcodes your stores stock."""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import html
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import db as dbmod
from app.config import Settings, get_settings
from app.domain import catalog as cat
from app.domain.common import Actor, audit
from app.infra.imaging import ImageRejected, ainspect_image
from app.infra.inference import InferenceClient, InferenceError, make_inference
from app.infra.storage import Storage, make_storage
from app.models import ImportBatch, Organization, Product, ReferenceImage, utcnow

log = logging.getLogger("calibrex.import")

SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # same rule as the catalog API
BARCODE_RE = re.compile(r"^\d{8,14}$")
LICENSE = "CC BY-SA"
LICENSE_NOTE = "CC BY-SA 3.0"
API_BASE = "https://world.openfoodfacts.org"
FIELDS = "code,product_name,product_name_en,brands,quantity,selected_images,image_front_url,images"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
APP_NAME = "CALIBREX-catalog-importer/1.0"


# ---------------------------------------------------------------- OFF client
class RateLimiter:
    """At most one call per `interval` seconds (shared by everything using it)."""

    def __init__(self, interval: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval, self._clock, self._sleep, self._next = interval, clock, sleep, 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next = max(self._clock(), self._next) + self.interval


class FetchError(Exception):
    """Transient failure that survived all retries (network, 429/5xx)."""


class OffClient:
    def __init__(self, http: httpx.AsyncClient, *, api_base: str = API_BASE, product_interval: float = 4.2, search_interval: float = 6.5,
                 image_interval: float = 0.3, retries: int = 5, backoff: float = 2.0, sleep=asyncio.sleep, clock=time.monotonic):
        self.http, self.api_base, self.retries, self.backoff, self._sleep = http, api_base.rstrip("/"), retries, backoff, sleep
        self.product_rl = RateLimiter(product_interval, clock, sleep)
        self.search_rl = RateLimiter(search_interval, clock, sleep)
        self.image_rl = RateLimiter(image_interval, clock, sleep)

    async def _get(self, url: str, rl: RateLimiter, params: dict[str, Any] | None = None) -> httpx.Response | None:
        """GET with rate limiting and retry/backoff (429, 5xx, transport errors; honours Retry-After). None = 404."""
        last = "unknown error"
        for attempt in range(self.retries + 1):
            await rl.wait()
            try:
                r = await self.http.get(url, params=params)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 404:
                    return None
                if r.status_code < 400:
                    return r
                last = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise FetchError(last)
                ra = r.headers.get("retry-after", "")
                if ra.isdigit():
                    await self._sleep(min(int(ra), 300))
            if attempt < self.retries:
                await self._sleep(self.backoff * (2 ** attempt) + random.random())
        raise FetchError(f"{last} after {self.retries + 1} attempts: {url}")

    async def product(self, barcode: str) -> dict | None:
        r = await self._get(f"{self.api_base}/api/v2/product/{barcode}.json", self.product_rl, {"fields": FIELDS})
        if r is None:
            return None
        body = r.json()
        return body.get("product") if body.get("status", 1) != 0 else None

    async def by_codes(self, codes: list[str]) -> dict[str, dict]:
        """One search call for many barcodes (search?code=a,b,c). Barcodes OFF does not know are simply absent."""
        r = await self._get(f"{self.api_base}/api/v2/search", self.search_rl,
                            {"code": ",".join(codes), "fields": FIELDS, "page_size": max(24, len(codes))})
        out: dict[str, dict] = {}
        for p in (r.json().get("products", []) if r else []):
            if p.get("code"):
                out[str(p["code"])] = p
        return out

    async def search(self, country: str | None, category: str | None, page: int, page_size: int) -> list[dict]:
        params: dict[str, Any] = {"fields": FIELDS, "page": page, "page_size": page_size}
        if country:
            params["countries_tags_en"] = country
        if category:
            params["categories_tags_en"] = category
        r = await self._get(f"{self.api_base}/api/v2/search", self.search_rl, params)
        return list(r.json().get("products", [])) if r else []

    async def image(self, url: str) -> bytes | None:
        r = await self._get(url, self.image_rl)
        if r is None:
            return None
        if len(r.content) > MAX_IMAGE_BYTES:
            raise FetchError("image larger than 15 MB")
        return r.content


# ---------------------------------------------------------------- parsing
@dataclass
class OffProduct:
    barcode: str
    name: str
    brand: str | None
    quantity: str | None
    image_url: str | None
    imgid: str | None  # raw upload id of the selected front image (for the S3 mirror layout)
    uploader: str | None = None


def _url(v: Any) -> str | None:
    if isinstance(v, str) and v.startswith("http"):
        return v
    if isinstance(v, dict):
        return _url(v.get("url"))
    return None


def front_image_url(raw: dict, lang: str = "en") -> str | None:
    """selected_images.front.display.<lang> (400 px), falling back to any language, then image_front_url."""
    disp = ((raw.get("selected_images") or {}).get("front") or {}).get("display")
    if isinstance(disp, dict):
        for k in (lang, *disp.keys()):
            u = _url(disp.get(k))
            if u:
                return u
    return _url(disp) or _url(raw.get("image_front_url"))


def parse_product(raw: dict, lang: str = "en") -> OffProduct | None:
    code = str(raw.get("code") or "").strip()
    name = html.unescape((raw.get("product_name") or raw.get("product_name_en") or "").strip())
    if not code or not name:
        return None
    brand = html.unescape((raw.get("brands") or "").split(",")[0].strip()) or None
    qty = (raw.get("quantity") or "").strip() or None
    imgid = uploader = None
    for key in (f"front_{lang}", *[k for k in (raw.get("images") or {}) if str(k).startswith("front_")]):
        im = (raw.get("images") or {}).get(key)
        if isinstance(im, dict) and im.get("imgid"):
            imgid = str(im["imgid"])
            break
    if imgid:
        uploader = ((raw.get("images") or {}).get(imgid) or {}).get("uploader")
    return OffProduct(code, name, brand, qty, front_image_url(raw, lang), imgid, uploader)


def display_name(p: OffProduct) -> str:
    return (f"{p.name} ({p.quantity})" if p.quantity and p.quantity.lower() not in p.name.lower() else p.name)[:200]


def barcode_path(barcode: str) -> str:
    b = barcode.zfill(13)
    return f"{b[0:3]}/{b[3:6]}/{b[6:9]}/{b[9:]}"


_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic")


def find_local_image(images_dir: Path, barcode: str, imgid: str | None) -> Path | None:
    """Pre-downloaded images: <dir>/<barcode>.jpg | <dir>/<barcode>/front*.jpg | the OFF image dataset layout
    <dir>/[data/]<123>/<456>/<789>/<0123>/<imgid>.jpg (needs the imgid from the API)."""
    for ext in _EXTS:
        f = images_dir / f"{barcode}{ext}"
        if f.is_file():
            return f
    d = images_dir / barcode
    if d.is_dir():
        files = sorted(f for f in d.iterdir() if f.suffix.lower() in _EXTS)
        files.sort(key=lambda f: (not f.name.lower().startswith("front"), f.name))
        if files:
            return files[0]
    if imgid:
        for root in (images_dir / "data", images_dir):
            for ext in _EXTS:
                f = root / barcode_path(barcode) / f"{imgid}{ext}"
                if f.is_file():
                    return f
    return None


def read_barcodes(path: Path) -> list[str]:
    """CSV (or one barcode per line). A header row is detected: column named barcode/ean/gtin/code/sku, else the first column."""
    out: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh) if r and any(c.strip() for c in r)]
    if not rows:
        return out
    col = 0
    if not rows[0][0].strip().isdigit():
        header = [c.strip().lower() for c in rows[0]]
        col = next((header.index(n) for n in ("barcode", "ean", "gtin", "code", "sku") if n in header), 0)
        rows = rows[1:]
    seen: set[str] = set()
    for r in rows:
        v = r[col].strip() if col < len(r) else ""
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def read_dump_products(path: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
    """Read only selected rows from a large tab-separated OFF dump; memory use is bounded by selected SKUs."""
    if not wanted:
        return {}
    csv.field_size_limit(16 * 1024 * 1024)
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    found: dict[str, dict[str, str]] = {}
    with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
        rows = csv.DictReader(fh, delimiter="\t")
        required = {"code", "product_name"}
        if not rows.fieldnames or not required.issubset(rows.fieldnames):
            raise ValueError("Expected a tab-separated OFF dump with 'code' and 'product_name' columns.")
        for row in rows:
            code = (row.get("code") or "").strip()
            if code in wanted and code not in found:
                found[code] = {key: (row.get(key) or "").strip() for key in
                               ("code", "product_name", "product_name_en", "brands", "quantity", "image_url", "image_front_url")}
                if len(found) == len(wanted):
                    break
    return found


# ---------------------------------------------------------------- the import
@dataclass
class ImportOptions:
    org_slug: str = "default"
    barcodes: list[str] | None = None
    country: str | None = None
    category: str | None = None
    limit: int | None = None  # max NEW products created in this run
    dry_run: bool = False
    check_images: bool = False  # dry-run only: also download + inspect images
    images_dir: Path | None = None
    catalog_dump: Path | None = None
    local_only: bool = False
    lang: str = "en"
    min_side: int = 200
    phash_distance: int = 2
    lookup: str = "batch"  # batch (search?code=a,b,c) | single (product/<code>)
    lookup_chunk: int = 24
    page_size: int = 24
    contact: str = ""

    def params(self) -> dict[str, Any]:
        return {"country": self.country, "category": self.category, "limit": self.limit, "dry_run": self.dry_run,
                "images_dir": str(self.images_dir) if self.images_dir else None,
                "catalog_dump": str(self.catalog_dump) if self.catalog_dump else None, "lang": self.lang, "min_side": self.min_side,
                "phash_distance": self.phash_distance, "lookup": self.lookup,
                "barcodes": len(self.barcodes) if self.barcodes is not None else None}


@dataclass
class Report:
    batch_id: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)


class Importer:
    def __init__(self, opts: ImportOptions, sm: async_sessionmaker[AsyncSession], storage: Storage, inference: InferenceClient,
                 s: Settings, off: OffClient):
        self.o, self.sm, self.storage, self.inference, self.s, self.off = opts, sm, storage, inference, s, off
        self.report = Report()
        self.batch_id: str | None = None
        self.org_id = ""
        self.signature: str | None = None
        self.phash: cat.PhashIndex | None = None
        self.created = 0
        self.dump_products: dict[str, dict[str, str]] = {}

    # -- bookkeeping --------------------------------------------------------
    async def _count(self, key: str, n: int = 1, *, barcode: str | None = None, kind: str | None = None, error: str | None = None) -> None:
        self.report.counts[key] = self.report.counts.get(key, 0) + n
        if barcode and kind:
            self.report.lines.append(f"{barcode}: {kind}" + (f" ({error})" if error else ""))
        if self.batch_id:
            async with self.sm() as db:
                b = await db.get(ImportBatch, self.batch_id)
                cat.batch_count(b, **{key: n})
                if barcode and kind and key in ("failed", "index_failed", "skipped", "rejected_images", "duplicate_images"):
                    cat.batch_error(b, barcode, kind, error or kind)
                await db.commit()

    @property
    def done(self) -> bool:
        return self.o.limit is not None and self.created >= self.o.limit

    # -- main ---------------------------------------------------------------
    async def run(self) -> Report:
        async with self.sm() as db:
            org = await db.scalar(select(Organization).where(Organization.slug == self.o.org_slug))
            if org is None:
                raise SystemExit(f"Organization '{self.o.org_slug}' not found (use --org <slug>).")
            self.org_id = org.id
            self.phash = await cat.PhashIndex.load(db, self.org_id, self.o.phash_distance)
            if not self.o.dry_run:
                b = await cat.start_batch(db, self.org_id, "openfoodfacts", self.o.params())
                self.batch_id = b.id
                self.report.batch_id = b.id
        try:
            if self.o.catalog_dump:
                wanted = set(self.o.barcodes or [])
                self.dump_products = read_dump_products(self.o.catalog_dump, wanted)
            self.signature = await cat.index_signature(self.inference)
            if self.o.barcodes is not None:
                await self._run_barcodes(self.o.barcodes)
            else:
                await self._run_search()
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.report.lines.append("interrupted: re-run the same command to resume")
            await self._count("interrupted")
            raise
        finally:
            if self.batch_id:
                async with self.sm() as db:
                    b = await db.get(ImportBatch, self.batch_id)
                    b.finished_at = utcnow()
                    await db.commit()
        return self.report

    async def _existing(self, barcodes: list[str]) -> dict[str, tuple[str, str, bool, int]]:
        """sku_key -> (product id, source, indexed?, reference count) for the barcodes already in the catalog."""
        out: dict[str, tuple[str, str, bool, int]] = {}
        async with self.sm() as db:
            for i in range(0, len(barcodes), 500):
                keys = [b.lower() for b in barcodes[i:i + 500]]
                n = select(ReferenceImage.product_id, func.count().label("n")).group_by(ReferenceImage.product_id).subquery()
                q = select(Product.id, Product.sku_key, Product.source, Product.indexed_at, func.coalesce(n.c.n, 0)).outerjoin(
                    n, n.c.product_id == Product.id).where(Product.org_id == self.org_id, Product.sku_key.in_(keys))
                for pid, key, src, ia, cnt in await db.execute(q):
                    out[key] = (pid, src, ia is not None, cnt)
        return out

    async def _run_barcodes(self, barcodes: list[str]) -> None:
        valid = []
        for b in barcodes:
            if BARCODE_RE.match(b) and SKU_RE.match(b):
                valid.append(b)
            else:
                await self._count("skipped", barcode=b, kind="invalid barcode (need 8-14 digits)")
        existing = await self._existing(valid)
        todo = []
        for b in valid:
            ex = existing.get(b.lower())
            if ex is None or (ex[1] == "openfoodfacts" and (ex[3] == 0 or not ex[2])):
                todo.append(b)
            else:
                await self._count("skipped")  # routine: not listed one by one
        step = self.o.lookup_chunk if self.o.lookup == "batch" else 1
        for i in range(0, len(todo), step):
            if self.done:
                break
            chunk = todo[i:i + step]
            if self.o.catalog_dump:
                found = {code: self.dump_products[code] for code in chunk if code in self.dump_products}
            else:
                try:
                    found = await self.off.by_codes(chunk) if self.o.lookup == "batch" else (
                        {chunk[0]: p} if (p := await self.off.product(chunk[0])) else {})
                except FetchError as e:
                    for b in chunk:
                        await self._count("failed", barcode=b, kind="API error", error=str(e))
                    continue
            for b in chunk:
                if self.done:
                    break
                raw = found.get(b)
                if raw is None:
                    kind = "not found in local OFF dump" if self.o.catalog_dump else "not found on Open Food Facts"
                    await self._count("skipped", barcode=b, kind=kind)
                    continue
                raw.setdefault("code", b)
                await self._handle(raw, existing.get(b.lower()))

    async def _run_search(self) -> None:
        page = 1
        while not self.done:
            try:
                products = await self.off.search(self.o.country, self.o.category, page, self.o.page_size)
            except FetchError as e:
                await self._count("failed", barcode=f"search page {page}", kind="API error", error=str(e))
                return
            if not products:
                return
            codes = [str(p.get("code") or "") for p in products]
            existing = await self._existing([c for c in codes if c])
            for raw, code in zip(products, codes):
                if self.done:
                    return
                ex = existing.get(code.lower())
                if not (BARCODE_RE.match(code) and SKU_RE.match(code)):
                    await self._count("skipped", barcode=code or "?", kind="invalid barcode (need 8-14 digits)")
                elif ex is None or (ex[1] == "openfoodfacts" and (ex[3] == 0 or not ex[2])):
                    await self._handle(raw, ex)
                else:
                    await self._count("skipped")
            page += 1

    # -- one product --------------------------------------------------------
    async def _handle(self, raw: dict, ex: tuple[str, str, bool, int] | None) -> None:
        p = parse_product(raw, self.o.lang)
        code = str(raw.get("code"))
        if ex is not None and ex[3] > 0 and not ex[2]:  # photos stored earlier, worker never confirmed: just re-send
            await self._reindex_existing(code, ex[0])
            return
        if p is None:
            await self._count("skipped", barcode=code, kind="no product name")
            return
        if self.o.dry_run and not self.o.check_images:
            await self._count("would_create", barcode=code, kind="would create", error=display_name(p))
            return
        try:
            data = await self._image_bytes(p)
        except FetchError as e:
            await self._count("failed", barcode=code, kind="image download failed", error=str(e))
            return
        if data is None:
            await self._count("skipped", barcode=code, kind="no front image")
            return
        try:
            info = await ainspect_image(data)
        except ImageRejected as e:
            await self._count("rejected_images", barcode=code, kind="image unreadable", error=e.message)
            await self._count("skipped")
            return
        if min(info.width, info.height) < self.o.min_side:
            await self._count("rejected_images", barcode=code, kind="image too small", error=f"{info.width}x{info.height}, need {self.o.min_side}px on the short side")
            await self._count("skipped")
            return
        if info.flat:
            await self._count("rejected_images", barcode=code, kind="placeholder / blank image")
            await self._count("skipped")
            return
        dup = self.phash.find(info.sha256, info.phash)
        if dup and dup[0].lower() != code.lower():
            await self._count("duplicate_images", barcode=code, kind=f"{dup[1]} already used by {dup[0]}")
            await self._count("skipped")
            return
        if dup:  # the same product's own photo (retry after a crash): nothing new to add
            await self._count("skipped", barcode=code, kind="image already stored")
            return
        if self.o.dry_run:
            await self._count("would_create", barcode=code, kind="would create", error=display_name(p))
            return
        await self._create(p, data, ex)

    async def _image_bytes(self, p: OffProduct) -> bytes | None:
        if self.o.images_dir:
            f = find_local_image(self.o.images_dir, p.barcode, p.imgid)
            if f is not None:
                return await asyncio.to_thread(f.read_bytes)
            if self.o.local_only or self.o.catalog_dump:
                return None
        elif self.o.catalog_dump:
            return None
        return await self.off.image(p.image_url) if p.image_url else None

    async def _create(self, p: OffProduct, data: bytes, ex: tuple[str, str, bool, int] | None) -> None:
        url = f"https://world.openfoodfacts.org/product/{p.barcode}"
        attribution = "Open Food Facts contributors" + (f" (photo by {p.uploader})" if p.uploader else "") + f", {LICENSE_NOTE}, {url}"
        actor = Actor(None, "Catalog import", None, self.org_id)
        try:
            ref = await cat.prepare_ref(data, "import")
        except ImageRejected as e:
            await self._count("rejected_images", barcode=p.barcode, kind="image unreadable", error=e.message)
            await self._count("skipped")
            return
        keys: list[str] = []
        async with self.sm() as db:
            try:
                if ex is None:
                    prod = Product(org_id=self.org_id, sku=p.barcode, sku_key=p.barcode.lower(), name=display_name(p), min_qty=0, barcode=p.barcode,
                                   brand=(p.brand or "")[:200] or None, source="openfoodfacts", source_url=url, image_license=LICENSE,
                                   image_attribution=attribution[:500])
                    db.add(prod)
                    await db.flush()
                    audit(db, actor, "PRODUCT_CREATED", prod.sku, after={"name": prod.name, "source": "import", "origin": "openfoodfacts", "batch_id": self.batch_id})
                else:  # an earlier run created the product but could not get an image
                    prod = await db.get(Product, ex[0])
                rows, keys = await cat.store_refs(self.storage, prod, [ref], None)
                db.add_all(rows)
                audit(db, actor, "REFERENCES_ADDED", prod.sku, after={"count": 1, "source": "import", "batch_id": self.batch_id})
                await db.commit()
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                for k in keys:
                    await self.storage.delete(k)
                await self._count("failed", barcode=p.barcode, kind="could not save", error=repr(e))
                return
            self.phash.add(prod.sku, ref.content_hash, ref.phash)
            await self._count("created")
            self.created += 1
            await self._index(db, prod, [(ref.rid, ref.ref)])

    async def _index(self, db: AsyncSession, prod: Product, items: list[tuple[str, bytes]]) -> bool:
        """Send to the worker; indexed_at is set ONLY after the worker acknowledged. A failure keeps the product."""
        try:
            await cat.send_references(self.inference, self.s, prod.sku, prod.name, items)
        except InferenceError as e:
            await self._count("index_failed", barcode=prod.sku, kind="worker did not acknowledge", error=str(e))
            return False
        prod.indexed_at, prod.indexed_with = utcnow(), self.signature
        await db.commit()
        await self._count("indexed")
        return True

    async def _reindex_existing(self, code: str, product_id: str) -> None:
        if self.o.dry_run:
            await self._count("would_reindex", barcode=code, kind="would re-send photos to the worker")
            return
        async with self.sm() as db:
            prod = await db.get(Product, product_id)
            try:
                items = await cat.product_items(db, self.storage, prod)
            except Exception as e:  # noqa: BLE001
                await self._count("failed", barcode=code, kind="stored photo unreadable", error=repr(e))
                return
            if await self._index(db, prod, items):
                await self._count("reindexed")


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    a = argparse.ArgumentParser(prog="import_catalog", description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    src = a.add_argument_group("what to import (one of)")
    src.add_argument("--barcodes-file", type=Path, help="CSV of barcodes your stores stock (header 'barcode' or first column). Recommended.")
    src.add_argument("--catalog-dump", type=Path, help="local tab-separated OFF dump; use with --barcodes-file and --images-dir to avoid network requests")
    src.add_argument("--country", help="OFF country filter, e.g. france (uses countries_tags_en)")
    src.add_argument("--category", help="OFF category filter, e.g. 'Orange Juice' (uses categories_tags_en)")
    a.add_argument("--limit", type=int, help="stop after creating this many NEW products")
    a.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing (no DB, storage or worker changes)")
    a.add_argument("--check-images", action="store_true", help="with --dry-run: also download and inspect the images")
    a.add_argument("--images-dir", type=Path, help="pre-downloaded images (<barcode>.jpg, <barcode>/front.jpg or the OFF S3 layout)")
    a.add_argument("--local-only", action="store_true", help="with --images-dir: never download a missing image")
    a.add_argument("--org", default="default", help="organization slug (default: default)")
    a.add_argument("--contact", default=None, help="your email or URL, sent in the User-Agent as OFF requires (or env OFF_CONTACT)")
    a.add_argument("--lang", default="en", help="preferred language of the front image (default en)")
    a.add_argument("--min-side", type=int, default=200, help="reject images whose short side is below this many px")
    a.add_argument("--phash-distance", type=int, default=2, choices=[0, 1, 2, 3], help="near-duplicate threshold (bits of 64)")
    a.add_argument("--lookup", choices=["batch", "single"], default="batch", help="batch: search?code=a,b,c (10 calls/min); single: product/<code> (15 calls/min)")
    a.add_argument("--api-base", default=API_BASE)
    a.add_argument("--product-interval", type=float, default=4.2, help="seconds between product calls (OFF limit 15/min)")
    a.add_argument("--search-interval", type=float, default=6.5, help="seconds between search calls (OFF limit 10/min)")
    return a


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import os
    contact = args.contact or os.environ.get("OFF_CONTACT")
    if args.catalog_dump and (not args.barcodes_file or not args.images_dir):
        build_parser().error("--catalog-dump requires both --barcodes-file and --images-dir")
    if args.catalog_dump and (args.country or args.category):
        build_parser().error("--catalog-dump uses a selected --barcodes-file; do not combine it with live OFF filters")
    if not args.barcodes_file and not (args.country or args.category):
        build_parser().error("give --barcodes-file or --country/--category")
    if args.barcodes_file and (args.country or args.category):
        build_parser().error("use either --barcodes-file or --country/--category, not both")
    if not contact and not args.catalog_dump:
        build_parser().error("--contact (or OFF_CONTACT) is required: Open Food Facts asks every client to identify itself")
    opts = ImportOptions(org_slug=args.org, barcodes=read_barcodes(args.barcodes_file) if args.barcodes_file else None,
                         catalog_dump=args.catalog_dump, country=args.country,
                         category=args.category, limit=args.limit, dry_run=args.dry_run, check_images=args.check_images, images_dir=args.images_dir,
                         local_only=args.local_only, lang=args.lang, min_side=args.min_side, phash_distance=args.phash_distance, lookup=args.lookup,
                         contact=contact)
    s = get_settings()
    dbmod.init_engine(s)
    try:
        # Dump mode is strictly offline; never attach contact data or make requests from that path.
        headers = {"User-Agent": f"{APP_NAME} ({contact})"} if contact and not args.catalog_dump else {}
        async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as http:
            off = OffClient(http, api_base=args.api_base, product_interval=args.product_interval, search_interval=args.search_interval)
            imp = Importer(opts, dbmod.sessionmaker(), make_storage(s), make_inference(s), s, off)
            rep = await imp.run()
    finally:
        await dbmod.dispose_engine()
    for line in rep.lines[-200:]:
        print(line)
    print(f"batch {rep.batch_id or '(dry run, nothing written)'}: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.counts.items())))
    return 1 if rep.counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
