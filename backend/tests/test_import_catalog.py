"""Catalog importer against a mocked Open Food Facts and a fake worker: no network, no GPU."""
from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import func, select

from app import db as dbmod
from app.infra.imaging import hamming, inspect_image
from app.models import AuditLog, ImportBatch, Product, ReferenceImage
from scripts.import_catalog import (ImportOptions, Importer, OffClient, barcode_path, find_local_image, parse_product,
                                    read_barcodes, read_dump_products)
from scripts.seed import seed
from tests.fakes import FakeOff, FakeWorker, photo

RED, GREEN, BLUE, YELLOW = (200, 30, 30), (30, 190, 60), (40, 60, 210), (220, 200, 30)
B1, B2, B3, B4 = "3017620422003", "5000112637922", "8076809513388", "4006381333931"


class Env:
    def __init__(self, c, off: FakeOff, worker: FakeWorker, http):
        self.c, self.off, self.worker, self.http = c, off, worker, http
        self.sleeps: list[float] = []

    async def run(self, **kw):
        async def nosleep(d):
            self.sleeps.append(d)
        opts = ImportOptions(**{"barcodes": [B1, B2, B3], **kw})
        client = OffClient(self.http, product_interval=0, search_interval=0, image_interval=0, sleep=nosleep, backoff=0.5)
        imp = Importer(opts, dbmod.sessionmaker(), self.c.app.state.storage, self.worker, self.c.app.state.settings, client)
        return await imp.run()

    async def q(self, stmt):
        async with dbmod.sessionmaker()() as db:
            return (await db.execute(stmt)).all()


async def make_env(client):
    await seed(client.app.state.settings)
    off = FakeOff()
    off.add(B1, "Choco Spread", photo(RED, 1))
    off.add(B2, "Orange Juice", photo(GREEN, 2), quantity="1 L")
    off.add(B3, "Tomato Pasta", photo(BLUE, 3))
    http = off.client()
    return Env(client, off, FakeWorker(), http)


async def test_import_creates_indexes_and_audits(client):
    e = await make_env(client)
    rep = await e.run()
    assert rep.counts["created"] == 3 and rep.counts["indexed"] == 3 and not rep.counts.get("failed")
    prods = {p.sku: p for (p,) in await e.q(select(Product).where(Product.source == "openfoodfacts"))}
    assert set(prods) == {B1, B2, B3} and e.worker.skus() == {B1, B2, B3}
    p = prods[B2]
    assert (p.barcode, p.brand, p.name) == (B2, "Acme", "Orange Juice (1 L)")
    assert p.indexed_at is not None and p.indexed_with == "fake-dino@fake_refs"
    assert p.image_license == "CC BY-SA" and "Open Food Facts" in p.image_attribution and "alice" in p.image_attribution
    assert p.source_url.endswith(B2)
    refs = await e.q(select(ReferenceImage.source, ReferenceImage.content_hash, ReferenceImage.phash))
    assert len(refs) == 3 and all(r[0] == "import" and len(r[1]) == 64 and len(r[2]) == 16 for r in refs)
    audit = await e.q(select(AuditLog.action, AuditLog.after).where(AuditLog.entity == B1))
    assert {a for a, _ in audit} == {"PRODUCT_CREATED", "REFERENCES_ADDED"} and all('"source": "import"' in after for _, after in audit)
    (batch,) = [b for (b,) in await e.q(select(ImportBatch))]
    assert batch.source == "openfoodfacts" and batch.finished_at and batch.counts["created"] == 3 and batch.params["lookup"] == "batch"
    ua = {r.headers["user-agent"] for r in e.off.requests}
    assert ua == {"CALIBREX-test/1.0 (test@example.com)"}


async def test_rerun_is_idempotent_and_makes_no_api_calls(client):
    e = await make_env(client)
    await e.run()
    n_requests, n_calls = len(e.off.requests), len(e.worker.calls)
    rep = await e.run()
    assert rep.counts.get("created", 0) == 0 and rep.counts["skipped"] == 3
    assert len(e.off.requests) == n_requests and len(e.worker.calls) == n_calls  # existing SKUs are filtered before any API call
    assert (await e.q(select(func.count()).select_from(ReferenceImage)))[0][0] == 3
    assert (await e.q(select(func.count()).select_from(Product).where(Product.source == "openfoodfacts")))[0][0] == 3


async def test_manual_product_with_same_sku_is_never_touched(client):
    e = await make_env(client)
    async with dbmod.sessionmaker()() as db:
        from app.models import Organization
        org = await db.scalar(select(Organization))
        db.add(Product(org_id=org.id, sku=B1, sku_key=B1, name="My own", source="manual"))
        await db.commit()
    rep = await e.run()
    assert rep.counts["created"] == 2
    assert (await e.q(select(Product.name, Product.source).where(Product.sku == B1)))[0] == ("My own", "manual")
    assert B1 not in e.worker.skus()


async def test_duplicate_placeholder_and_small_images_are_rejected(client):
    e = await make_env(client)
    same = photo(YELLOW, 9)
    e.off.add(B4, "Yellow Pack", same)
    e.off.add("1111111111116", "Same Photo Other Code", same)  # byte-identical
    e.off.add("2222222222222", "Re-encoded Copy", photo(YELLOW, 9, quality=55))  # same picture, other bytes: perceptual hash
    e.off.add("3333333333331", "Blank", _png((255, 255, 255)))
    e.off.add("4444444444448", "Tiny", photo(RED, 5, size=(150, 150)))
    e.off.add("5555555555555", "No Image", None)
    a, b = inspect_image(same), inspect_image(photo(YELLOW, 9, quality=55))
    assert a.sha256 != b.sha256 and hamming(a.phash, b.phash) <= 2  # premise of the test
    rep = await e.run(barcodes=[B4, "1111111111116", "2222222222222", "3333333333331", "4444444444448", "5555555555555", "9999999999994", "12ab"])
    c = rep.counts
    assert c["created"] == 1 and c["duplicate_images"] == 2 and c["rejected_images"] == 2
    assert {p for (p,) in await e.q(select(Product.sku))} >= {B4} and not ({"1111111111116", "2222222222222", "3333333333331", "4444444444448", "5555555555555"} & {p for (p,) in await e.q(select(Product.sku))})
    log = {(x["key"], x["kind"]) for (b,) in await e.q(select(ImportBatch)) for x in b.error_log}
    assert ("1111111111116", "identical file already used by " + B4) in log
    assert ("2222222222222", "near-identical image already used by " + B4) in log
    assert any(k == "3333333333331" and "placeholder" in t for k, t in log) and any(k == "4444444444448" and "too small" in t for k, t in log)
    assert any(k == "5555555555555" and "no front image" in t for k, t in log) and any(k == "9999999999994" and "not found" in t for k, t in log)
    assert any(k == "12ab" and "invalid barcode" in t for k, t in log)


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), color).save(buf, "PNG")
    return buf.getvalue()


async def test_worker_failure_keeps_product_unindexed_then_resumes(client):
    e = await make_env(client)
    e.worker.fail = 1  # the first product's worker call fails
    rep = await e.run()
    assert rep.counts["created"] == 3 and rep.counts["indexed"] == 2 and rep.counts["index_failed"] == 1  # the run kept going
    rows = {sku: ia for sku, ia in await e.q(select(Product.sku, Product.indexed_at).where(Product.source == "openfoodfacts"))}
    assert sorted(k for k, v in rows.items() if v is None) == [B1] and rows[B2] and rows[B3]
    assert (await e.q(select(func.count()).select_from(ReferenceImage)))[0][0] == 3  # photos are stored regardless
    assert B1 not in e.worker.skus()
    (batch,) = [b for (b,) in await e.q(select(ImportBatch))]
    assert any(x["key"] == B1 and x["kind"] == "worker did not acknowledge" for x in batch.error_log)

    rep = await e.run()  # worker is healthy again: the same command heals it, without re-downloading or duplicating photos
    assert rep.counts["reindexed"] == 1 and rep.counts.get("created", 0) == 0
    assert (await e.q(select(Product.indexed_at).where(Product.sku == B1)))[0][0] is not None and B1 in e.worker.skus()
    assert (await e.q(select(func.count()).select_from(ReferenceImage)))[0][0] == 3
    assert not any("front_en" in str(r.url) for r in e.off.requests[-1:])


async def test_worker_down_entirely_still_stores_everything(client):
    e = await make_env(client)
    e.worker.down = True
    rep = await e.run()
    assert rep.counts["created"] == 3 and rep.counts["index_failed"] == 3 and not rep.counts.get("indexed")
    assert all(ia is None for (ia,) in await e.q(select(Product.indexed_at).where(Product.source == "openfoodfacts")))


async def test_api_retries_with_backoff_honour_retry_after_and_give_up(client):
    e = await make_env(client)
    e.off.flaky["/api/v2/search"] = [429, 503]  # two failures, then success
    rep = await e.run()
    assert rep.counts["created"] == 3
    assert 1 in e.sleeps and any(s >= 0.5 for s in e.sleeps)  # Retry-After: 1, then exponential backoff
    e2 = await make_env_second(client)
    e2.off.flaky["/api/v2/search"] = [503] * 20  # never recovers
    rep = await e2.run(barcodes=["4006381333931"])
    assert rep.counts["failed"] == 1 and not rep.counts.get("created")
    assert len(e2.sleeps) >= 5


async def make_env_second(client):
    off = FakeOff()
    off.add(B4, "Yellow Pack", photo(YELLOW, 4))
    return Env(client, off, FakeWorker(), off.client())


async def test_dry_run_writes_nothing(client):
    e = await make_env(client)
    rep = await e.run(dry_run=True)
    assert rep.counts["would_create"] == 3 and rep.batch_id is None
    assert not await e.q(select(Product).where(Product.source == "openfoodfacts")) and not await e.q(select(ImportBatch))
    assert not e.worker.calls and not e.worker.store
    assert not any(r.url.host == "images.openfoodfacts.org" for r in e.off.requests)  # no image downloads without --check-images
    rep = await e.run(dry_run=True, check_images=True)
    assert rep.counts["would_create"] == 3 and not await e.q(select(Product).where(Product.source == "openfoodfacts"))


async def test_limit_counts_new_products_only(client):
    e = await make_env(client)
    rep = await e.run(limit=2)
    assert rep.counts["created"] == 2
    rep = await e.run(limit=2)  # resumes: the remaining product
    assert rep.counts["created"] == 1


async def test_country_category_search_paginates(client):
    e = await make_env(client)
    e.off.add("6001240100011", "Spanish Thing", photo(YELLOW, 11), countries=("Spain",))
    rep = await e.run(barcodes=None, country="France", category="Snacks", page_size=2)
    assert rep.counts["created"] == 3  # 3 French snacks over 2 pages; the Spanish one is filtered out by the API
    assert "6001240100011" not in {s for (s,) in await e.q(select(Product.sku))}


async def test_single_lookup_mode(client):
    e = await make_env(client)
    rep = await e.run(lookup="single")
    assert rep.counts["created"] == 3
    assert any(r.url.path.startswith("/api/v2/product/") for r in e.off.requests)


async def test_pre_downloaded_images_dir_avoids_image_downloads(client, tmp_path):
    e = await make_env(client)
    (tmp_path / f"{B1}.jpg").write_bytes(photo(RED, 1))
    d = tmp_path / B2
    d.mkdir()
    (d / "other.jpg").write_bytes(photo(GREEN, 77))
    (d / "front_en.jpg").write_bytes(photo(GREEN, 2))
    s3 = tmp_path / "data" / barcode_path(B3)
    s3.mkdir(parents=True)
    (s3 / "1.jpg").write_bytes(photo(BLUE, 3))  # OFF image dataset layout; imgid 1 comes from the API metadata
    rep = await e.run(images_dir=tmp_path, local_only=True)
    assert rep.counts["created"] == 3
    assert not any(r.url.host == "images.openfoodfacts.org" for r in e.off.requests)
    assert find_local_image(tmp_path, B2, None).name == "front_en.jpg"


async def test_offline_dump_import_uses_local_metadata_and_images_only(client, tmp_path):
    e = await make_env(client)
    dump = tmp_path / "products.tsv"
    dump.write_text(
        "code\tproduct_name\tproduct_name_en\tbrands\tquantity\timage_url\tcountries_tags\tcountries_en\n"
        f"{B1}\tLocal Cocoa Spread\t\tLocal Brand, Other\t350 g\thttps://images.openfoodfacts.org/x.jpg\ten:india\tIndia\n"
        f"{B2}\tNo Local Photo\t\tLocal Brand\t1 L\thttps://images.openfoodfacts.org/y.jpg\ten:india\tIndia\n",
        encoding="utf-8",
    )
    (tmp_path / f"{B1}.jpg").write_bytes(photo(RED, 28))
    opts = ImportOptions(barcodes=[B1, B2], catalog_dump=dump, images_dir=tmp_path, local_only=True)
    imp = Importer(opts, dbmod.sessionmaker(), e.c.app.state.storage, e.worker, e.c.app.state.settings, e.off)
    rep = await imp.run()
    assert rep.counts["created"] == 1 and rep.counts["indexed"] == 1 and rep.counts["skipped"] == 1
    assert e.off.requests == []  # dump mode never calls OFF API or image URLs
    (product,) = [p for (p,) in await e.q(select(Product).where(Product.sku == B1))]
    assert product.name == "Local Cocoa Spread (350 g)" and product.brand == "Local Brand"
    assert product.indexed_at is not None
    assert product.image_license == "CC BY-SA" and B2 not in {sku for (sku,) in await e.q(select(Product.sku))}
    (batch,) = [b for (b,) in await e.q(select(ImportBatch))]
    assert any(error["key"] == B2 and error["kind"] == "no front image" for error in batch.error_log)


def test_read_dump_products_keeps_only_requested_rows(tmp_path):
    dump = tmp_path / "products.tsv"
    dump.write_text("code\tproduct_name\tbrands\n" + f"{B1}\tCocoa\tBrand\n{B2}\tJuice\tOther\n", encoding="utf-8")
    assert read_dump_products(dump, {B2}) == {B2: {"code": B2, "product_name": "Juice", "product_name_en": "",
                                                       "brands": "Other", "quantity": "", "image_url": ""}}


def test_parsing_and_helpers(tmp_path):
    raw = {"code": B1, "product_name": "Nutella", "brands": "Ferrero, Other", "quantity": "750 g",
           "selected_images": {"front": {"display": {"fr": "https://x/fr.jpg", "en": "https://x/en.jpg"}}}}
    p = parse_product(raw, "en")
    assert (p.name, p.brand, p.quantity, p.image_url) == ("Nutella", "Ferrero", "750 g", "https://x/en.jpg")
    assert parse_product({**raw, "selected_images": {"front": {"display": {"fr": {"url": "https://x/fr.jpg"}}}}}, "en").image_url == "https://x/fr.jpg"
    assert parse_product({"code": B1, "image_front_url": "https://x/f.jpg", "product_name": "A"}).image_url == "https://x/f.jpg"
    assert parse_product({"code": B1, "product_name": ""}) is None
    assert barcode_path("3017620422003") == "301/762/042/2003" and barcode_path("12345678") == "000/001/234/5678"
    f = tmp_path / "b.csv"
    f.write_text("name,Barcode\nA,3017620422003\nB,3017620422003\nC, 5000112637922 \n\n")
    assert read_barcodes(f) == ["3017620422003", "5000112637922"]
    f.write_text("3017620422003\n5000112637922\n")
    assert read_barcodes(f) == ["3017620422003", "5000112637922"]
