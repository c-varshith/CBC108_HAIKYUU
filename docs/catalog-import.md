# Catalog setup and import

This guide covers reference-photo matching, manual and bulk catalog setup, Open Food Facts imports, Qdrant indexing, and review-crop references.

## Loading a catalog

### How products are learned

Registering a product saves its SKU and reference-photo records in the database; the image files go to the configured local or S3 storage. The photos are sent to the AI worker, which embeds each with DINOv2 and stores the vectors in Qdrant. The next scan searches those vectors to match detected packages to catalog SKUs. Catalog photos improve SKU matching; they do not retrain the detector. Three to five clear photos from different angles work best. Keep the full package visible, use good light, and include varied backgrounds. In Catalog, use **Test recognition** to check a photo and compare the returned similarity scores before scanning a shelf. Close scores can indicate that products or their packaging look alike; add more varied reference photos and review scan suggestions carefully.

You can register a few products with your own photos in Catalog. For bulk catalogs, use the importer with Open Food Facts (OFF), or provide a local image folder with `--images-dir`; the importer never needs to be used to register products by hand. OFF product images are **CC BY-SA 3.0** and require attribution and share-alike terms; imported image credits are shown in the app. See the importer instructions below for local-only folder usage.

**Use an existing OFF dump without network access.** `select_off_barcodes.py` streams plain tab-separated dumps and `.gz` dumps; it does not load the full file in memory. For the 13 GB file in the project root:

```bash
python -I select_off_barcodes.py en.openfoodfacts.org.products.csv --country India --limit 300 -o stocked.csv
```

The offline importer reads metadata for those barcodes from the same dump and only reads photos from a local OFF image dataset or another supported `--images-dir`. It makes no OFF API or image requests and does not require `OFF_CONTACT`:

```bash
cd backend
../.venv/bin/python -I -m scripts.import_catalog --barcodes-file ../stocked.csv \
  --catalog-dump ../en.openfoodfacts.org.products.csv --images-dir /data/off-images --local-only --dry-run --check-images
../.venv/bin/python -I -m scripts.import_catalog --barcodes-file ../stocked.csv \
  --catalog-dump ../en.openfoodfacts.org.products.csv --images-dir /data/off-images --local-only
```

The image folder must contain files matching the barcode (`<barcode>.jpg` or `<barcode>/front*.jpg`). The dump's image URL is metadata only in offline mode; the importer does not download it or search an unrelated image dataset. Products with missing or unusable local photos are reported and skipped; only products with photos can become Ready and searchable. OFF images remain CC BY-SA 3.0, so preserve attribution and share-alike terms.

### Replace the default catalog with 2,000 India packaged foods

`backend/scripts/refresh_off_catalog.py` stages the first 2,000 qualifying rows in dump order. It requires India country tags, a valid barcode, a product name, a grocery-food category and an OFF front-image URL. It excludes fresh/raw fruits and vegetables, non-food items, supplements and unknown categories. OFF packaging fields are sparse, so this is a documented category heuristic, not a verified packaging flag. The script reports skips and category decisions; it keeps scanning past missing/bad images until 2,000 distinct usable images are staged. If the dump cannot supply 2,000, it exits without replacing the catalog.

First preview (no downloads or database changes):

```bash
cd backend
../.venv/bin/python -I scripts/refresh_off_catalog.py --dry-run
```

To apply, keep the database and AI worker running and use the **same** database and storage settings as the API. For a local worker, export its URL and inference mode in the shell where you run the script (replace the database/storage values with the exact ones used by your API):

```bash
export INFERENCE_BACKEND=http
export AI_WORKER_URL=http://localhost:8001
export DATABASE_URL='<same DATABASE_URL used by the API>'
export STORAGE_DIR='<same STORAGE_DIR used by the API>'
```

Stop the API while its catalog is being replaced. The script downloads only from `images.openfoodfacts.org`; it does not call the OFF product API. It stages and validates all 2,000 images before deleting anything. The selector rejects placeholder image paths, obvious raw-produce-only names, supplements, and alcohol categories in addition to category exclusions. OFF packaging metadata is sparse, so the category rule is still an approximation. Run the apply command only when ready to replace **all products in the default organization**:

```bash
../.venv/bin/python -I scripts/refresh_off_catalog.py --replace-catalog
```

The reset removes that organization's product vectors, stored reference photos, products and expected-stock rows. It preserves users, scans, audit history, product requests and import-batch history. It aborts on SKU overlap with another organization because the worker keys vectors by SKU. The refresh uses one OFF front photo per product; use shelf photos as additional references for products that need better shelf recognition. Images are attributed to Open Food Facts under CC BY-SA 3.0 in the catalog. After the run, inspect the printed import batch counts and confirm the Catalog index status reports 2,000 Ready products. If indexing fails partway, keep `backend/data/off-images/stocked.csv` and resume through the existing offline importer (set the same database/storage environment first):

```bash
../.venv/bin/python -I scripts/import_catalog.py \
  --barcodes-file data/off-images/stocked.csv \
  --catalog-dump ../en.openfoodfacts.org.products.csv \
  --images-dir data/off-images --local-only --org default
```

**1. Migrate the database** (from `backend/`, with the root `.venv` active):

```bash
cd backend
alembic upgrade head
```

`0001_initial` is frozen. `0002_catalog_import` adds catalog metadata, `0003` adds password-reset tokens, and `0004` adds scan-upload idempotency keys. Migrations should be exercised against a PostgreSQL copy before applying them to a production database.

**2. Environment.** The importer and the resync script do not go through the HTTP API. They talk to the database, the storage and the AI worker directly, so they need the same settings as the API, and the worker must be running:

```bash
export DATABASE_URL=postgresql+asyncpg://calibrex:calibrex@localhost:5432/calibrex
export INFERENCE_BACKEND=http            # real inference is the default; stub has no embeddings to build
export AI_WORKER_URL=http://localhost:8001
export AI_WORKER_TOKEN=...               # only if the worker has one (required when ENV=production)
# storage: STORAGE_BACKEND=local (STORAGE_DIR, default ./data) or s3 (S3_ENDPOINT_URL, S3_BUCKET, S3_REGION, S3_ACCESS_KEY, S3_SECRET_KEY)
```

Point `STORAGE_DIR` at the same directory the API uses, otherwise the API will not find the photos.

**3. Import the SKUs your store stocks.** Put the barcodes in a CSV (a `barcode` header, or just the first column):

```bash
cd backend
python -m scripts.import_catalog --barcodes-file stocked.csv --contact you@example.com
```

`--contact` (or the `OFF_CONTACT` environment variable) is required. Use your own address or a URL in place of the `you@example.com` placeholder; it is only ever sent in the User-Agent, `CALIBREX-catalog-importer/1.0 (<contact>)`, because OFF asks every client to identify itself as `AppName/Version (contact)`. The importer refuses to start without it. No address is stored in this repository.

**Dry run first.** `--dry-run` fetches and reports and writes nothing (no database, storage or worker changes). Add `--check-images` to also download and inspect the images:

```bash
python -m scripts.import_catalog --barcodes-file stocked.csv --contact you@example.com --dry-run
python -m scripts.import_catalog --barcodes-file stocked.csv --contact you@example.com --dry-run --check-images
```

**Filter instead of a list.** `--country` filters on `countries_tags_en` and `--category` on `categories_tags_en`. `--limit N` stops after N *new* products:

```bash
python -m scripts.import_catalog --country france --category "Orange Juice" --limit 200 --contact you@example.com
```

The `--country` filter name is assumed by analogy with the documented category filter and has not been checked against the live API. Prefer `--barcodes-file`.

**Pre-downloaded images.** `--images-dir DIR` accepts `<barcode>.jpg`, `<barcode>/front*.jpg`, or the OFF S3 layout. In that layout the barcode is zero-padded to 13 digits and split 3/3/3/4, so `3017620422003` is `data/301/762/042/2003/1.jpg` in the `openfoodfacts-images` bucket (region `eu-west-3`). Add `--local-only` to never download a missing image:

```bash
python -m scripts.import_catalog --barcodes-file stocked.csv --images-dir /data/off-images --local-only --contact you@example.com
```

Other options: `--org SLUG` (default `default`), `--lang` (default `en`), `--min-side` (default 200 px), `--phash-distance 0..3` (default 2), `--lookup batch|single` (default `batch`), `--api-base`, `--product-interval` (default 4.2 s) and `--search-interval` (default 6.5 s).

**What it does and does not do.**
- Idempotent and resumable. Existing SKUs are skipped before any API call. Products created without an image, or whose worker call failed, are healed when you run it again.
- SKU = barcode. The OFF quantity is folded into the name, for example `Orange Juice (1 L)`.
- Rejected images: shorter side under 200 px, flat, blank or placeholder images, and duplicates (identical sha256, or perceptual hash within 2 bits, checked across the whole organization because identical references for two SKUs make matching ambiguous). A product with no usable image is **not created**; it is logged to the batch `error_log`.
- Each run writes an `import_batches` row and audit rows. The Owner can see recent batches on the Catalog page.

**Rate limits and time.** OFF allows 15 product reads and 10 searches per minute per IP; the defaults sit below both. Batch lookup sends `search?code=a,b,c` with 24 codes per call. Single lookup makes one call per product. For 10,000 products, from the defaults (arithmetic, **not measured**):

- single: 4.2 s x 10,000 = 42,000 s, about 11.7 hours.
- batch: 10,000 / 24 = about 417 search calls x 6.5 s = about 2,700 s, about 45 minutes, plus image downloads (0.3 s apart) and the embedding time on your worker.

Expect hours at that scale. OFF itself advises using its data dumps and image dataset once you go beyond a few hundred products; use the S3 layout with `--images-dir` for that.

**Import only what you stock.** A bigger catalog means more look-alike packs for DINOv2 to confuse and more review work. Keep it to the SKUs on your shelves.

**Licensing.** OFF product images are CC BY-SA 3.0 (keep attribution, share-alike) and OFF data is ODbL 1.0. The app has an "Image credits" page at `#/app/credits` (linked from the navigation and the Catalog footer) with both notices and a searchable list of credited products, and imported products show their licence line in the Catalog.

### Product status and the search index

Each product shows a server-driven status:

| Status | Meaning |
|---|---|
| Ready | It has photos **and** the worker acknowledged them. `indexed_at` is set only after that acknowledgement. |
| Not searchable yet | No photos. |
| Failed | Photos are stored but the worker never confirmed them. Use **Retry** in the Catalog, or `POST /catalog/products/{sku}/reindex`. A product whose resync failed is also left as Failed. |

`indexed_with` records `<DINO_MODEL>@<QDRANT_COLLECTION>` at the moment of acknowledgement. `GET /catalog/index-status` and every resync batch warn when it differs from what the worker reports now, and when the worker holds no vectors while products are marked Ready. The worker's `/health` returns `model` and `collection` for that comparison. The Owner sees these warnings in the Catalog "Search index" panel.

### Resync Qdrant

After a wiped Qdrant volume, or after changing `DINO_MODEL` or `QDRANT_COLLECTION` (see [ai-worker/README.md](../ai-worker/README.md)), re-send every stored reference photo to the worker:

```bash
cd backend
python -m scripts.resync_qdrant                       # whole default org
python -m scripts.resync_qdrant --org default --sku 3017620422003 --sku 5449000000996   # only these SKUs
```

The Owner can do the same from the Catalog "Search index" panel, or through the API: `POST /catalog/resync` returns 202 with a `batch_id`; poll `GET /catalog/batches/{batch_id}`. It returns 409 if a resync is already running and 503 if the worker is unreachable. Resync re-embeds the active products' stored references. Normal reference-photo deletion removes that photo's vectors from the worker; retries are safe. Resync is additive and cannot discover unknown orphan vectors left by manual Qdrant edits or older deployments. It removes inactive products on a best-effort basis.

### Review crops

When a reviewer corrects a box to a SKU in a scan, "Add this crop as a reference photo" (Manager or Owner, `catalog.write`) keeps that crop as an extra reference photo (`source` = `review_crop`, `POST /catalog/products/{sku}/references/from-detection`). Shelf crops narrow the gap between catalog packshots and what the camera sees on a shelf. The call returns 409 for an uncorrected box or a duplicate crop; a product holds at most 20 photos.

