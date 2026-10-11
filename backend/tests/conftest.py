"""Test harness: in-memory SQLite, stub inference with zero latency, temp storage, real lifespan (runner included)."""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="calibrex-test-")
os.environ.update(ENV="test", DATABASE_URL="sqlite+aiosqlite:///:memory:", AUTO_CREATE_SCHEMA="true", INFERENCE_BACKEND="stub",
                  STUB_LATENCY_MS="0", STORAGE_DIR=_TMP, EXPOSE_INVITE_TOKEN="true", MAIL_BACKEND="console",
                  LOGIN_MAX_ATTEMPTS="1000", RECOVERY_INTERVAL_SECONDS="0", LOG_LEVEL="WARNING", ANALYZE_ENABLED="true")

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.auth import reset_login_limiter  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest_asyncio.fixture
async def client(tmp_path):
    """A fresh app, file-backed SQLite database and storage folder per test (file DB, not :memory:, so the
    background runner and request handlers get separate connections like they do on PostgreSQL)."""
    reset_login_limiter()
    s = get_settings().model_copy(update={"database_url": f"sqlite+aiosqlite:///{tmp_path}/test.db", "storage_dir": str(tmp_path / "files")})
    app = create_app(s)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # tests reach app.state (runner, settings) through the client
            yield c


@pytest_asyncio.fixture
async def seeded(client):
    await seed(client.app.state.settings)
    return client
