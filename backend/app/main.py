"""App factory: settings, storage, inference, mailer, processing runner, CORS, security headers, all routers."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import Settings, get_settings
from app.domain.processing import ProcessingRunner
from app.errors import install_error_handlers
from app.infra.inference import make_inference
from app.infra.logging import RequestLogMiddleware, configure_logging
from app.infra.mailer import make_mailer
from app.infra.storage import make_storage
from app.routers import admin, analyze, assistant, auth, catalog, media, scans, users, workflow


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    configure_logging(s.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = db.init_engine(s)
        if s.auto_create_schema:
            from app.models import Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        app.state.settings = s
        app.state.storage = make_storage(s)
        app.state.inference = make_inference(s)
        app.state.mailer = make_mailer(s)
        app.state.bg_tasks = set()  # background jobs (catalog resync); tests await them
        app.state.resync_running = set()  # org ids with a resync in flight
        app.state.runner = ProcessingRunner(s, app.state.storage, app.state.inference)
        if s.env != "test":
            await app.state.runner.recover_once()  # pick up scans interrupted by a restart
            app.state.runner.start_recovery_loop()
        if s.seed_on_start:
            from scripts.seed import seed
            await seed(s)
        yield
        for t in list(app.state.bg_tasks):
            t.cancel()
        await app.state.runner.stop()
        await db.dispose_engine()

    app = FastAPI(title="Calibrex API", version="0.3.0", lifespan=lifespan,
                  docs_url="/docs" if s.docs_enabled else None, redoc_url=None,
                  openapi_url="/openapi.json" if s.docs_enabled else None)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(RequestLogMiddleware)
    install_error_handlers(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if s.env == "production":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.url.path.startswith(("/docs", "/openapi")):
            resp.headers.setdefault("Cache-Control", "no-store") if not request.url.path.startswith("/media") else None
        return resp

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request):
        from sqlalchemy import text
        checks = {}
        try:
            async with db.sessionmaker()() as session:
                await session.execute(text("select 1"))
            checks["database"] = True
        except Exception:
            checks["database"] = False
        checks["storage"] = await request.app.state.storage.ping()
        checks["inference"] = await request.app.state.inference.ping()
        from fastapi.responses import JSONResponse
        ok = all(checks.values())
        return JSONResponse({"status": "ready" if ok else "degraded", **checks}, status_code=200 if ok else 503)

    for r in (auth.router, users.router, scans.router, workflow.router, catalog.router, admin.router, media.router, analyze.router, assistant.router):
        app.include_router(r)
    return app


app = create_app()
