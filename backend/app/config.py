"""Runtime configuration (12-factor: everything comes from the environment / .env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEV_SECRET = "dev-only-secret-change-me-dev-only-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://calibrex:calibrex@localhost:5432/calibrex"
    # Dev/test only: build tables with create_all instead of Alembic.
    auto_create_schema: bool = False

    # --- auth ---
    jwt_secret: str = _DEV_SECRET
    jwt_algorithm: str = "HS256"
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 30
    refresh_cookie_name: str = "cx_refresh"
    # "/" works whether the API is served at the root or behind a /api proxy; narrow it only if you know your public path.
    refresh_cookie_path: str = "/"
    cookie_secure: bool | None = None  # None -> True in production
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    login_max_attempts: int = 5
    login_window_seconds: int = 60
    invite_ttl_hours: int = 168
    password_reset_ttl_minutes: int = 60
    # Return the raw invite token from POST /users/invite. Demo/e2e only: a real deployment emails it.
    expose_invite_token: bool = False
    public_app_url: str = "http://localhost:5173"

    # --- http ---
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    trust_proxy_headers: bool = False  # honour X-Forwarded-For (set only behind your own proxy)
    docs_enabled: bool = True
    # "text": plain-text error bodies (what the signed-off frontend shows verbatim). "json": {"detail": ...}
    error_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"

    # --- storage ---
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: str = "./data"
    # Where the BROWSER reaches /media. Dev proxy and the compose reverse proxy both expose the API under /api.
    media_url_prefix: str = "/api/media"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "calibrex"
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # --- images ---
    max_upload_bytes: int = 25 * 1024 * 1024
    max_megapixels: float = 50.0
    display_max_side: int = 1600
    max_files_per_request: int = 10
    max_reference_bytes: int = 10 * 1024 * 1024

    # --- inference (Phase 4 service) ---
    inference_backend: Literal["stub", "http"] = "http"
    ai_worker_url: str = "http://localhost:8001"
    ai_worker_token: str | None = None
    inference_timeout_seconds: float = 120.0
    # The worker rejects /v1/references calls with more photos than this (its MAX_REFS_PER_CALL). The backend itself accepts
    # at most 10 per request; bulk senders (importer, resync, reindex) use min(10, this).
    ai_worker_max_refs_per_call: int = 12
    stub_latency_ms: int = 2500  # makes the laser-scan animation visible in demos
    det_conf_min: float = 0.50  # detector confidence below this can never be SURE
    max_detections: int = 2000
    processing_lease_seconds: int = 300
    recovery_interval_seconds: int = 30

    # --- /analyze (contract used by the AI-inventory frontend) ---
    analyze_enabled: bool = True
    analyze_review_threshold: float = 0.75
    analyze_per_minute: int = 30  # per client IP, single-photo uploads
    analyze_live_per_minute: int = 90  # per client IP, live-camera frames (?live=1), a separate bucket
    analyze_catalog: Annotated[list[str], NoDecode] = [
        "Canned Goods", "Olive Oil Bottle", "Almond Milk Carton", "Cracker Box", "Honey Jar",
        "Sparkling Water Can", "Cereal Box", "Pasta Pack", "Coffee Bag", "Pet Food Pouch"]

    # --- inventory assistant (Groq OpenAI-compatible chat completions API) ---
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # --- mail ---
    mail_backend: Literal["console", "smtp"] = "console"
    mail_from: str = "Calibrex <no-reply@calibrex.local>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True

    # --- housekeeping ---
    retention_sweep_enabled: bool = False
    seed_on_start: bool = False

    @field_validator("cors_origins", "analyze_catalog", mode="before")
    @classmethod
    def _split_origins(cls, v):  # allow "a,b" as well as JSON
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _production_guards(self):
        if self.cookie_secure is None:
            self.cookie_secure = self.env == "production"
        if self.env == "production":
            if self.jwt_secret == _DEV_SECRET or len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must be set to a random value of at least 32 characters in production")
            if self.expose_invite_token:
                raise ValueError("EXPOSE_INVITE_TOKEN must be false in production")
            if self.inference_backend == "stub":
                raise ValueError("INFERENCE_BACKEND=stub is for development only; use http in production")
            if self.auto_create_schema:
                raise ValueError("AUTO_CREATE_SCHEMA is for development only; run `alembic upgrade head`")
            if self.docs_enabled:
                raise ValueError("DOCS_ENABLED must be false in production")
            if self.seed_on_start:
                raise ValueError("SEED_ON_START must be false in production")
            if self.mail_backend != "smtp":
                raise ValueError("MAIL_BACKEND=smtp is required in production")
            if not self.trust_proxy_headers or not self.cookie_secure:
                raise ValueError("Production requires TRUST_PROXY_HEADERS=true and COOKIE_SECURE=true")
            if not self.public_app_url.startswith("https://"):
                raise ValueError("PUBLIC_APP_URL must use https in production")
            if not self.cors_origins or "*" in self.cors_origins:
                raise ValueError("Production CORS_ORIGINS must list explicit origins")
            if not self.ai_worker_token:
                raise ValueError("AI_WORKER_TOKEN is required in production")
            if self.smtp_host in ("", "localhost", "127.0.0.1"):
                raise ValueError("SMTP_HOST must identify the production mail relay")
            if self.storage_backend == "s3" and (not self.s3_bucket or not self.s3_region):
                raise ValueError("S3_BUCKET and S3_REGION are required when STORAGE_BACKEND=s3")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
