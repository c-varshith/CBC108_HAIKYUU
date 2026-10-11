"""Alembic migrations on SQLite, run in a SUBPROCESS: alembic/env.py caches get_settings() at import time, so the database URL
must be in the environment of a fresh interpreter (never call alembic in-process from the test run).
PostgreSQL is NOT covered here."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def alembic(db: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}", "AUTO_CREATE_SCHEMA": "false", "ENV": "test"}
    r = subprocess.run([sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *args], cwd=BACKEND, env=env,
                       capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def seed_old_rows(con: sqlite3.Connection) -> None:
    """Rows as the 0001 schema (before the catalog import) would hold them: one product with photos, one without."""
    con.execute("INSERT INTO organizations (id, slug, name, created_at) VALUES ('o1', 'default', 'Org', '2026-01-01 00:00:00')")
    for pid, sku in (("p-photos", "SKU-A"), ("p-none", "SKU-B")):
        con.execute("INSERT INTO products (id, org_id, sku, sku_key, name, min_qty, active, created_at) VALUES (?, 'o1', ?, ?, ?, 0, 1, '2026-01-01 00:00:00')",
                    (pid, sku, sku.lower(), sku))
    for rid, at in (("r1", "2026-02-01 10:00:00"), ("r2", "2026-03-05 12:30:00")):
        con.execute("INSERT INTO reference_images (id, product_id, storage_key, thumb_key, created_by, created_at) VALUES (?, 'p-photos', ?, ?, NULL, ?)",
                    (rid, f"catalog/p-photos/{rid}.jpg", f"catalog/p-photos/{rid}.thumb.jpg", at))
    con.commit()


def test_upgrade_backfills_existing_products_and_schema_matches_models(tmp_path):
    db = tmp_path / "old.db"
    alembic(db, "upgrade", "0001")
    con = sqlite3.connect(db)
    assert "indexed_at" not in cols(con, "products") and "import_batches" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    seed_old_rows(con)
    con.close()

    alembic(db, "upgrade", "head")
    con = sqlite3.connect(db)
    assert {"barcode", "brand", "source", "source_url", "indexed_at", "indexed_with", "image_license", "image_attribution"} <= cols(con, "products")
    assert {"source", "content_hash", "phash"} <= cols(con, "reference_images")
    assert {"client_request_id"} <= cols(con, "scans")
    assert {"id", "user_id", "token_hash", "created_at", "expires_at", "used_at"} <= cols(con, "password_resets")
    assert {"id", "org_id", "source", "started_at", "finished_at", "counts", "params", "error_log"} <= cols(con, "import_batches")
    rows = {sku: (ia, iw, src) for sku, ia, iw, src in con.execute("SELECT sku, indexed_at, indexed_with, source FROM products")}
    assert rows["SKU-A"][0] is not None and rows["SKU-A"][0].startswith("2026-03-05 12:30")  # backfilled with the NEWEST photo's time
    assert rows["SKU-B"][0] is None  # no photos: still "pending"
    assert rows["SKU-A"][1] is None  # indexed_with unknown for rows that predate it
    assert rows["SKU-A"][2] == rows["SKU-B"][2] == "manual"  # server_default
    assert {r[0] for r in con.execute("SELECT source FROM reference_images")} == {"upload"}
    assert {r[0] for r in con.execute("SELECT content_hash FROM reference_images")} == {None}
    con.close()

    out = alembic(db, "check")  # models and migrations agree: no pending autogenerate operations
    assert "No new upgrade operations detected" in out.stdout + out.stderr
    assert alembic(db, "current").stdout.strip().endswith("0004 (head)")


def test_fresh_database_upgrades_to_head_and_check_is_clean(tmp_path):
    db = tmp_path / "fresh.db"
    alembic(db, "upgrade", "head")
    out = alembic(db, "check")
    assert "No new upgrade operations detected" in out.stdout + out.stderr


def test_downgrade_then_upgrade_round_trip_keeps_data(tmp_path):
    db = tmp_path / "round.db"
    alembic(db, "upgrade", "0001")
    con = sqlite3.connect(db)
    seed_old_rows(con)
    con.close()
    alembic(db, "upgrade", "head")
    alembic(db, "downgrade", "0001")
    con = sqlite3.connect(db)
    assert "indexed_at" not in cols(con, "products") and "phash" not in cols(con, "reference_images")
    assert con.execute("SELECT count(*) FROM products").fetchone()[0] == 2 and con.execute("SELECT count(*) FROM reference_images").fetchone()[0] == 2
    assert "import_batches" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    alembic(db, "upgrade", "head")
    con = sqlite3.connect(db)
    assert con.execute("SELECT indexed_at FROM products WHERE sku = 'SKU-A'").fetchone()[0] is not None  # backfill runs again
    con.close()
