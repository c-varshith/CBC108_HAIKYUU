"""Offline coverage for the destructive OFF catalog refresh pipeline."""
from __future__ import annotations

import csv
import hashlib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app import db as dbmod
from app.domain import catalog as cat
from app.infra.imaging import ImageInfo, inspect_image
from app.infra.inference import InferenceError
from app.models import (AuditLog, ExpectedStock, Organization, Product, ReferenceImage, Scan, Shelf, User)
from scripts.refresh_off_catalog import (Candidate, RefreshError, apply_refresh, candidate_from_row,
                                         iter_candidates, is_packaged_food, reset_catalog, stage_images)
from scripts.seed import seed
from tests.fakes import FakeWorker, photo
from tests.helpers import login, upload_scan


def _row(code: str, **kwargs):
    row = {"code": code, "countries_tags": "en:india", "countries_en": "India", "product_name": "Snack pack",
           "categories_tags": "en:snacks,en:sweet-snacks", "categories_en": "Snacks,Sweet snacks",
           "food_groups_tags": "en:sugary-snacks", "food_groups_en": "Sugary snacks", "brands": "Test Brand",
           "quantity": "100 g", "image_url": f"https://images.openfoodfacts.org/images/products/{code}/front.jpg"}
    row.update(kwargs)
    return row


def test_category_filter_accepts_packaged_food_and_rejects_fresh_nonfood_and_unknown():
    assert is_packaged_food(_row("12345678")) == (True, "eligible_category")
    assert is_packaged_food(_row("12345678", categories_tags="en:vegetables", categories_en="Vegetables"))[0] is False
    assert is_packaged_food(_row("12345678", categories_tags="en:cosmetics", categories_en="Cosmetics"))[0] is False
    assert is_packaged_food(_row("12345678", categories_tags="en:food-products", categories_en="Food products",
                                 food_groups_tags="", food_groups_en=""))[0] is False
    assert is_packaged_food(_row("12345678", categories_tags="en:bananas", categories_en="Bananas"))[0] is False
    assert is_packaged_food(_row("12345678", product_name="Ginger", categories_tags="en:spices"))[1] == "raw_produce_name"
    assert is_packaged_food(_row("12345678", product_name="Afresh", brands="Herbalife", categories_tags="en:beverages"))[1] == (
        "non_food_or_supplement_name")
    assert is_packaged_food(_row("12345678", categories_tags="en:alcoholic-beverages"))[1] == "non_food_or_supplement"
    assert is_packaged_food(_row("12345678", categories_tags="", categories_en="", food_groups_tags="", food_groups_en="")) == (
        False, "no_food_category")
    assert is_packaged_food(_row("12345678", categories_tags="en:unknown-category", categories_en="Unknown",
                                 food_groups_tags="", food_groups_en=""))[0] is False


def test_candidate_validation_requires_india_name_barcode_category_and_off_image_url():
    good, reason = candidate_from_row(_row("12345678", product_name="M&amp;M snack",
                                            image_url="https://images.openfoodfacts.org/generic.jpg",
                                            image_front_url="https://images.openfoodfacts.org/front.jpg"))
    assert good and good.name == "M&M snack" and good.image_url.endswith("front.jpg") and reason == "eligible_category"
    assert candidate_from_row(_row("12345678", image_url="http://images.openfoodfacts.org/a.jpg"))[0] is None
    assert candidate_from_row(_row("12345678", image_url="https://example.com/a.jpg"))[0] is None
    assert candidate_from_row(_row("12345678", image_url="https://images.openfoodfacts.org/images/products/invalid/front.jpg"))[1] == (
        "placeholder_image_url")
    assert candidate_from_row(_row("12345678", product_name=""))[0] is None


def test_streaming_selector_takes_first_qualifying_rows_and_deduplicates(tmp_path):
    dump = tmp_path / "products.tsv"
    header = ["code", "countries_tags", "countries_en", "product_name", "categories_tags", "categories_en",
              "food_groups_tags", "food_groups_en", "brands", "quantity", "image_url"]
    rows = [
        _row("11111111", categories_tags="en:vegetables", categories_en="Vegetables"),
        _row("22222222"),
        _row("22222222", product_name="Duplicate name"),
        _row("33333333", countries_tags="en:france", countries_en="France"),
        _row("44444444", product_name=""),
        _row("55555555", categories_tags="en:unknown", categories_en="Unknown", food_groups_tags="", food_groups_en=""),
        _row("66666666"),
    ]
    with dump.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    counts = Counter()
    selected = list(iter_candidates(dump, counts=counts))
    assert [item.barcode for item in selected] == ["22222222", "66666666"]
    assert counts["fresh_or_raw_produce"] == 1
    assert counts["duplicate_barcode"] == 1
    assert counts["other_country"] == 1
    assert counts["missing_name"] == 1


@pytest.mark.asyncio
async def test_image_download_retries_and_saves_valid_images_in_candidate_order(tmp_path, monkeypatch):
    import scripts.refresh_off_catalog as refresh

    async def inspect_in_process(data):
        return inspect_image(data)

    monkeypatch.setattr(refresh, "ainspect_image", inspect_in_process)
    good_a, good_b = photo((210, 40, 30), 10), photo((30, 190, 60), 11)
    calls = Counter()

    def handler(request):
        code = request.url.path.split("/")[-2]
        calls[code] += 1
        if code == "11111111":
            return httpx.Response(404)
        if code == "22222222" and calls[code] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=good_a if code == "22222222" else good_b,
                              headers={"content-type": "image/jpeg"})

    async def no_sleep(_):
        return None

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        chosen, counts = await stage_images(iter([
            Candidate("11111111", "Missing", "", "", "https://images.openfoodfacts.org/images/11111111/front.jpg"),
            Candidate("22222222", "First", "", "", "https://images.openfoodfacts.org/images/22222222/front.jpg"),
            Candidate("33333333", "Second", "", "", "https://images.openfoodfacts.org/images/33333333/front.jpg"),
        ]), tmp_path, target=2, client=client, retries=1, sleep=no_sleep)
    assert [item.barcode for item in chosen] == ["22222222", "33333333"]
    assert calls["22222222"] == 2 and counts["image_rejected"] == 1 and counts["downloaded"] == 2
    assert (tmp_path / "22222222.jpg").read_bytes() == good_a


@pytest.mark.asyncio
async def test_downloader_rejects_untrusted_host_invalid_and_oversized_images(tmp_path, monkeypatch):
    import scripts.refresh_off_catalog as refresh

    async def inspect_in_process(data):
        return inspect_image(data)

    monkeypatch.setattr(refresh, "ainspect_image", inspect_in_process)
    async def no_sleep(_):
        return None

    def handler(request):
        if request.url.path.endswith("invalid.jpg"):
            return httpx.Response(200, content=b"not an image", headers={"content-type": "image/jpeg"})
        if request.url.path.endswith("large.jpg"):
            return httpx.Response(200, content=b"x" * 32, headers={"content-type": "image/jpeg"})
        return httpx.Response(200, content=photo((100, 40, 180), 12), headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chosen, counts = await stage_images(iter([
            Candidate("11111111", "External", "", "", "https://example.com/a.jpg"),
            Candidate("22222222", "Invalid", "", "", "https://images.openfoodfacts.org/invalid.jpg"),
            Candidate("33333333", "Large", "", "", "https://images.openfoodfacts.org/large.jpg"),
        ]), tmp_path, target=1, client=client, max_bytes=16, retries=0, sleep=no_sleep)
    assert not chosen and counts["image_rejected"] == 3
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_continues_in_dump_order_until_exact_target(tmp_path, monkeypatch):
    import scripts.refresh_off_catalog as refresh
    calls = Counter()

    async def fake_download(candidate, *_args, **_kwargs):
        calls[candidate.barcode] += 1
        phash = hashlib.sha256(candidate.barcode.encode()).hexdigest()[:16]
        return b"image", ".jpg", ImageInfo(240, 240, f"sha-{candidate.barcode}", phash, False)

    monkeypatch.setattr(refresh, "download_and_validate", fake_download)
    candidates = (Candidate(f"{i:08d}", f"Product {i}", "", "", "https://images.openfoodfacts.org/front.jpg")
                  for i in range(1, 2006))
    async with httpx.AsyncClient() as client:
        chosen, counts = await stage_images(candidates, tmp_path, target=2000, client=client)
    assert len(chosen) == 2000
    assert chosen[0].barcode == "00000001" and chosen[-1].barcode == "00002000"
    assert counts["downloaded"] == 2000
    assert sum(calls.values()) == 2000
    assert len(list(tmp_path.glob("*.jpg"))) == 2000


async def test_shortfall_aborts_before_database_or_catalog_reset(tmp_path, monkeypatch):
    import scripts.refresh_off_catalog as refresh

    dump = tmp_path / "too-small.tsv"
    fields = ["code", "countries_tags", "countries_en", "product_name", "categories_tags", "categories_en",
              "food_groups_tags", "food_groups_en", "brands", "quantity", "image_url"]
    with dump.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerow(_row("12345678", image_url=""))
    monkeypatch.setattr(refresh, "get_settings", lambda: SimpleNamespace(inference_backend="http"))
    monkeypatch.setattr(dbmod, "init_engine", lambda *_: pytest.fail("database must not be touched on a selection shortfall"))
    args = SimpleNamespace(dump=dump, images_dir=tmp_path / "images", org="default", limit=1)
    with pytest.raises(RefreshError, match="Catalog was not changed"):
        await apply_refresh(args)
    assert not args.images_dir.joinpath("12345678.jpg").exists()


@pytest.mark.asyncio
async def test_apply_explains_http_worker_configuration_before_staging(tmp_path, monkeypatch):
    import scripts.refresh_off_catalog as refresh

    monkeypatch.setattr(refresh, "get_settings", lambda: SimpleNamespace(inference_backend="stub"))
    monkeypatch.setattr(dbmod, "init_engine", lambda *_: pytest.fail("configuration failure must precede DB setup"))
    args = SimpleNamespace(dump=tmp_path / "dump.tsv", images_dir=tmp_path / "images", org="default", limit=2000)
    with pytest.raises(RefreshError, match="AI_WORKER_URL, DATABASE_URL, and STORAGE_DIR"):
        await apply_refresh(args)
    assert not args.images_dir.exists()


async def test_reset_catalog_removes_only_default_products_and_preserves_history(client):
    await seed(client.app.state.settings)
    owner = await login(client, "owner")
    scan_id = await upload_scan(client, owner, seed=17)
    settings = client.app.state.settings
    storage = client.app.state.storage
    worker = FakeWorker()
    async with dbmod.sessionmaker()() as db:
        org = await db.scalar(select(Organization).where(Organization.slug == "default"))
        shelf = await db.scalar(select(Shelf).where(Shelf.org_id == org.id))
        other = Organization(slug="other", name="Other")
        db.add(other)
        await db.flush()
        db.add(Product(org_id=other.id, sku="OTHER-1", sku_key="other-1", name="Other org product"))
        product = Product(org_id=org.id, sku="1234567890123", sku_key="1234567890123", name="Old product")
        db.add(product)
        await db.flush()
        ref = await cat.prepare_ref(photo((210, 40, 30), 19), "import")
        ref_rows, _ = await cat.store_refs(storage, product, [ref], None)
        db.add_all(ref_rows)
        db.add(ExpectedStock(org_id=org.id, shelf_id=shelf.id, sku=product.sku, expected_qty=4))
        await db.commit()
    await worker.add_references("1234567890123", "Old product", [(ref.rid, ref.ref)])
    before_users = await scalar_count(User)
    before_audit = await scalar_count(AuditLog)
    result = await reset_catalog(dbmod.sessionmaker(), storage, worker, "default", settings)
    assert result == {"products_removed": 1, "references_removed": 1, "worker_skus_removed": 1}
    async with dbmod.sessionmaker()() as db:
        remaining_products = list((await db.scalars(select(Product.sku))).all())
        assert await db.get(Scan, scan_id) is not None
    assert remaining_products == ["OTHER-1"] and await scalar_count(ReferenceImage) == 0
    assert await scalar_count(ExpectedStock) == 0 and await scalar_count(User) == before_users
    assert await scalar_count(Scan) == 1
    assert await scalar_count(AuditLog) == before_audit + 1
    assert not worker.store
    assert await storage.ping()
    assert not (Path(settings.storage_dir) / "catalog" / product.id).exists()


async def test_reset_catalog_worker_failure_keeps_product_rows(client):
    await seed(client.app.state.settings)
    settings = client.app.state.settings
    storage = client.app.state.storage
    worker = FakeWorker()
    async with dbmod.sessionmaker()() as db:
        org = await db.scalar(select(Organization).where(Organization.slug == "default"))
        db.add(Product(org_id=org.id, sku="5555555555555", sku_key="5555555555555", name="Keep me"))
        await db.commit()

    async def fail_remove(_):
        raise InferenceError("worker unavailable")

    worker.remove_product = fail_remove
    with pytest.raises(RefreshError, match="database catalog was not deleted"):
        await reset_catalog(dbmod.sessionmaker(), storage, worker, "default", settings)
    assert await scalar_count(Product) == 1


async def scalar_count(model):
    async with dbmod.sessionmaker()() as db:
        return await db.scalar(select(func.count()).select_from(model))
