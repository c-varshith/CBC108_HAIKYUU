# CALIBREX backend (Phase 3)

## Verification update (2026-10-10, staged OFF catalog refresh)
- Follow-up from the user's preview: the `--replace-catalog` invocation stopped before staging because settings resolved to `INFERENCE_BACKEND=stub`; no DB connection or catalog mutation occurred. Improved this preflight error, documented the required shell exports, rejected OFF placeholder URL path segments, and tightened obvious raw-produce/supplement/alcohol exclusions. New checks cover these exclusions and config failure. The focused network-free tests now report **8 passed, 2 database-backed tests deselected**; py_compile and Ruff remain clean.
- Added `scripts/refresh_off_catalog.py`: isolated-Python preview/apply commands stream the local TSV/gzip dump, select the first qualifying India rows, reject invalid barcodes/names/categories/URLs, and stage exactly the requested number of distinct, decodable, non-placeholder images from the OFF image host before catalog changes. Downloads have host/redirect checks, byte and dimension limits, bounded concurrency, retries, and barcode filenames. `--replace-catalog` is explicit and restricted to the `default` organization.
- Replacement verifies worker/storage health and cross-organization SKU collisions, removes vectors before the database transaction, restores removed vectors and deleted photo objects when cleanup fails, clears default-org expected stock and product/reference rows, and audits the replacement. Users, scans, audit records, requests and earlier import batches are retained. The existing local importer then creates records with OFF attribution and indexes the references.
- Added network-free tests for category exclusions, valid metadata and front-image URL preference, first-qualifying order/deduplication, retries and bad/oversized/untrusted images, pre-reset shortfall safety, and exact 2,000-image staging. **7 passed** (`pytest -q tests/test_refresh_off_catalog.py -k 'category_filter or candidate_validation or streaming_selector or image_download or downloader_rejects or shortfall or staging_continues'`). The 2 database-backed replacement tests are present but could not complete because this environment's async SQLite setup hangs; the broader focused file run reached that same environment block and timed out after 30 seconds. No database or catalog was modified.
- `python -I -m py_compile` passed for the changed scripts/tests; Ruff `--select F` passed for them. A no-download preview against the root dump with the final packaged-food filter and `--limit 2000` exceeded a 60-second timeout before producing a summary. The final eligible count and image availability remain unverified.
- No OFF image host/API requests were made, no images were staged, and no live catalog replacement or real worker/Qdrant run was attempted. The real run still needs network access, running worker/Qdrant/API configuration and available image downloads. Exact availability of 2,000 valid images is therefore unverified.

## Verification update (2026-10-10, offline dump and product learning)
- Implemented offline TSV/gzip barcode selection and created the requested `../stocked.csv` from the root dump: **300 unique India barcodes**, after reading **30,884 rows**. `python3 -I select_off_barcodes.py ...` completed; the selector's network-free tests report **2 passed**.
- Frontend checks in this session: `npx tsc --noEmit` passed; `npm run test:unit` reports **19 passed**; `npm run build` passed (Vite output 555.03 kB). `npm ci` required `--offline --ignore-scripts` because its normal esbuild lifecycle spawn was denied in this environment.
- Python syntax compilation passed for the changed backend and selector files. `ruff check --select F` passed on changed Python files.
- Backend application tests could not reach test setup: pytest hangs while the FastAPI lifespan initializes SQLite. The same environment issue reproduces with a direct `aiosqlite.connect('/tmp/...')`, which times out, although synchronous `sqlite3` works. The two isolated selector tests do not require the database and pass. No backend test pass count is claimed for this session.
- The network probe to `https://pypi.org` failed with DNS resolution error (curl exit 6); `/tmp/venv` dependency installation therefore could not complete. AI-worker tests were not run because the available project virtualenv lacks `torch`, `torchvision`, `qdrant-client`, and `transformers`.
- Real worker/Qdrant matching was not attempted: there is no local product photo directory, so selected dump rows cannot be indexed or scanned as real references. The 13 GB catalog dump supplies product metadata and image URLs, not local photo bytes. No Open Food Facts API or image host was contacted.
- The Playwright end-to-end suite was not run; a live API could not start because of the SQLite async connection hang. WIP8 archive excludes the 13 GB dump, image datasets, node_modules, virtual environments, and generated/cache directories; it includes the available detector models and the small selected `stocked.csv`.

## Verification update (2026-10-10, catalog import work)
- Real results of the final verification run (2026-10-10, network available): backend **159 passed** (112 s; it was 100 before the catalog additions), ai-worker **34 passed**, frontend **19 unit tests passed** (the earlier brief said 17; the real baseline is 19). `ruff check --select F` is clean on the new and changed files (import_catalog.py, resync_qdrant.py, routers/catalog.py, infra/inference.py, test_import_catalog.py, test_catalog_index.py, test_migrations.py). `tsc --noEmit` is clean and `npm run build` succeeds.
- A wider `ruff check --select F backend ai-worker` reports 6 unused imports in older files outside this work (eval/evaluate.py, domain/processing.py, routers/auth.py, routers/scans.py, tests/test_auth.py); they are left as they were. The Playwright e2e suite was not run in this verification.
- Not covered by that run: the real OFF API, the real worker and Qdrant over HTTP, PostgreSQL, S3, and the repo's Playwright e2e suite. The new UI was smoke-tested only with hand-written scripts in Chromium against stub inference; those scripts are not in the repo.

## Verification update (2026-10-10, earlier)
- Project-root `.venv` uses Python 3.12.15; backend requirements and development requirements installed.
- `timeout 180s /home/varshith/Projects/calibrex/.venv/bin/python -m pytest -q`: 96 passed at that time (stale; see the counts above).
- Started the seeded SQLite API with `AUTO_CREATE_SCHEMA=true SEED_ON_START=true`; `python -m scripts.demo --base-url http://127.0.0.1:8000` completed the staff, manager, owner, and auditor walkthrough.
- Docker base profile built and started. `http://127.0.0.1:8080/`, `/api/docs`, and `/api/health` returned HTTP 200.
- The H5 worker tests and real-model CPU smoke inference are recorded in `../ai-worker/README.md`. No labeled shelf dataset is present, so H5-versus-YOLO accuracy comparison has not been run.
- PostgreSQL container health was verified; the full backend pytest suite uses SQLite. S3 is not configured or tested.

## Done
- Foundations: config, db, models (19 tables), errors, permissions, state machine, selectors, security, deps, infra (storage, imaging, inference, mailer, ratelimit, logging), domain services (scans, uploads, processing, reports, exports).
- Routers: auth (register-first-owner, login, rotating per-device refresh with replay detection, logout, logout-all, invites),
  users (invite/role/deactivate, last-Owner guard), scans (upload, list, detail, SSE events, corrections, submit/approve/return/reopen/retry/delete, comments, timeline, CSV/PDF export),
  approvals inbox, notifications, catalog (products, reference photos, staff product requests), stores/shelves, expected stock (edit + all-or-nothing CSV import),
  settings (thresholds, tolerance, approval policy, "what would be flagged" preview), reports/alerts, audit log (search + CSV), signed media, health + readiness, `/analyze`.
- Alembic async env, `0001_initial` (frozen explicit table definitions, plus the PostgreSQL append-only trigger on audit_log), `0002_catalog_import` (catalog metadata and import batches), `0003_password_resets`, and `0004_scan_idempotency`.
- scripts/seed.py (idempotent), scripts/demo.py (four-role walkthrough).
- Catalog import: `python -m scripts.import_catalog` (OFF by barcode list, country or category, or a local images folder incl. the OFF S3 layout; `--contact`/`OFF_CONTACT` required; dry-run, idempotent, resumable; image quality and duplicate rejection) and `python -m scripts.resync_qdrant` (see the root README, "Loading a catalog").
- Catalog routes: `GET /catalog/products` (q, limit, offset, slim, status), `POST /catalog/products/{sku}/reindex`, `GET /catalog/index-status`, `POST /catalog/resync` (Owner, 202), `GET /catalog/batches` and `/catalog/batches/{id}`, `GET /catalog/credits`, `POST /catalog/products/{sku}/references/from-detection` (review crops). Status model ready / pending / failed with `indexed_at` and `indexed_with`. Worker `/health` returns `model` and `collection`.
- Frontend: Catalog status badges with Retry, search, status filter and "Load more", the Owner "Search index" panel, "Add this crop as a reference photo" in ScanDetail, and the "Image credits" page (`#/app/credits`).
- New tests: `test_import_catalog.py`, `test_catalog_index.py`, `test_migrations.py`, new RBAC entries; ai-worker `/health` and over-limit tests.
- tests/: RBAC matrix (parametrised), separation of duties + flagged Owner override, locking, submit-blocking, return with pinned boxes, auto-approve policy, delete rules, auth (rotation, replay, rate limit, invites, deactivation), catalog/CSV import, settings preview, notifications, audit, unit tests for selectors/state machine/permissions/exports/detection building.
- Dockerfile, docker-compose.yml (api, postgres, qdrant, proxy; ai-worker under profile "ai"), nginx.conf, .env.example.

## Known gaps / things to know
1. `0001_initial` was rewritten from `create_all` into explicit, frozen table definitions (`create_all` picks up later model changes, which made every autogenerated migration collide on a fresh database). Do not edit it. Schema changes go in new revisions. PostgreSQL migration verification remains required.
2. The real workflow is served at `frontend/#/app` and uses the API contract described in `frontend/src/lib/contract.ts`; the older sample dashboard is separate.
3. The frontend classifier rounds margins to six decimals to match `selectors.classify` at the 0.08 boundary.
4. Background processing is in-process with a DB lease (restart-safe, multi-worker-safe). For heavy load move `ProcessingRunner.run` behind ARQ/Celery.
5. Rate limiting is in-memory (single process). Put it behind Redis before running several API workers.
6. PostgreSQL was exercised through the Docker stack but not through the full pytest suite; S3 storage is untested.
7. The Phase 4 AI worker implements the existing HTTP contract at the top of `app/infra/inference.py`; its H5/YOLO model details and measured checks are in `../ai-worker/README.md`. Set `INFERENCE_BACKEND=http` to use it.
8. Catalog import has not been run against the real Open Food Facts API (unreachable from the build sandbox). The `countries_tags_en` filter name, the `selected_images` response shape (parsed defensively) and `search?code=a,b,c` batch behaviour are assumptions from the docs. The "hours" estimates are arithmetic from the default intervals, not measurements.
9. The worker and Qdrant were exercised only through a fake worker in tests. Embedding time on CPU/GPU and indexing time for a large catalog are unmeasured.
10. Reference deletion now calls the worker's idempotent per-reference delete endpoint. Resync re-embeds catalog references but is additive, so it cannot discover unknown orphan vectors from manual edits or older deployments.
11. The perceptual-hash duplicate check (default 2 bits, org-wide) has not been tried on real look-alike packs; false positives are possible.
12. The repo's own Playwright e2e suite (`frontend/e2e`) was not run for this work.

## Decisions to confirm
- Error bodies are plain text (the signed-off client shows `res.text()`); `ERROR_FORMAT=json` for `{"detail": ...}`.
- Detection fields marked "client-only" (deleted, needs_review, accepted_unknown) are persisted and returned, for cross-device resume.
- Reopen sets REOPENED (the mock jumps to IN_REVIEW). Staff list = own scans; Staff can edit/submit only their own scans.
- POST /scans with several files creates one scan per file (returns `scan_id` of the first plus `scan_ids`).
- Staff product request flow: Manager decides APPROVED/REJECTED, then registers the product by reference photos (the request does not auto-create a product).
- The refresh cookie path defaults to "/" so it works behind the /api proxy (`REFRESH_COOKIE_PATH` to narrow).
