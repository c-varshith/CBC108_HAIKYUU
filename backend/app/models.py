"""SQLAlchemy 2.0 models. Portable across PostgreSQL (production) and SQLite (tests)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        TypeDecorator, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python, whatever the backend does (SQLite drops tzinfo)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSONType, list[Any]: JSONType}


def _id() -> Mapped[str]:
    return mapped_column(String(64), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------- tenancy / people
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = _id()
    slug: Mapped[str] = mapped_column(String(64), unique=True)  # bootstrap race guard: one "default" org
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("org_id", "name_key"),)
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))  # lower(name): case-insensitive uniqueness


class Shelf(Base):
    __tablename__ = "shelves"
    __table_args__ = (UniqueConstraint("store_id", "name_key"),)
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-case
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))  # OWNER | MANAGER | STAFF | AUDITOR
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)  # invited, not yet accepted
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)  # bump = every access token dies
    last_active: Mapped[datetime] = _created()
    created_at: Mapped[datetime] = _created()


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256; the raw token is never stored
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()


class RefreshToken(Base):
    """One row per device session. Rotated on every refresh; reuse of a rotated token revokes the family."""
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device: Mapped[str] = mapped_column(String(200), default="")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PasswordReset(Base):
    """Single-use password recovery tokens; only a hash of the emailed token is stored."""
    __tablename__ = "password_resets"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# ---------------------------------------------------------------- catalog / stock
class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("org_id", "sku_key"),)
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    sku_key: Mapped[str] = mapped_column(String(64))  # lower(sku)
    name: Mapped[str] = mapped_column(String(200))
    min_qty: Mapped[int] = mapped_column(Integer, default=0)  # low-stock minimum (single source of truth)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # soft delete keeps history readable
    created_at: Mapped[datetime] = _created()
    # --- provenance / catalog import ---
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # EAN/UPC when known (SKU = barcode for imports)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")  # 'manual' | 'openfoodfacts'
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Set only when the AI worker acknowledged the embeddings. NULL = "not searchable yet" (or the worker failed).
    indexed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # "<DINO_MODEL>@<QDRANT_COLLECTION>" the worker reported when it acknowledged: lets us warn when vectors may be stale.
    indexed_with: Mapped[str | None] = mapped_column(String(300), nullable=True)
    image_license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_attribution: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ReferenceImage(Base):
    __tablename__ = "reference_images"
    id: Mapped[str] = _id()
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    storage_key: Mapped[str] = mapped_column(String(300))
    thumb_key: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()
    source: Mapped[str] = mapped_column(String(16), default="upload", server_default="upload")  # 'upload' | 'import' | 'review_crop'
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # sha256 of the source bytes
    phash: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 64-bit perceptual (difference) hash, hex


class ImportBatch(Base):
    """One run of the catalog importer (source='openfoodfacts') or of a search-index resync (source='resync')."""
    __tablename__ = "import_batches"
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # NULL while running (or if the process died)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # created / skipped / failed (+ details)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error_log: Mapped[list[Any]] = mapped_column(JSONType, default=list)  # [{"key": sku/barcode, "kind": ..., "error": ...}]


class ProductRequest(Base):
    __tablename__ = "product_requests"
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    requested_by: Mapped[str] = mapped_column(String(64))  # user id
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()


class ExpectedStock(Base):
    """Mock WMS: expected quantity of a SKU on a shelf."""
    __tablename__ = "wms_expected_inventory"
    __table_args__ = (UniqueConstraint("shelf_id", "sku"),)
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    expected_qty: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- scans
class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (Index("ix_scans_org_status", "org_id", "status"), Index("ix_scans_lease", "status", "lease_until"),
                      UniqueConstraint("created_by", "client_request_id"))
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[str] = mapped_column(String(64))
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_key: Mapped[str | None] = mapped_column(String(300), nullable=True)  # original (EXIF-normalised, GPS stripped)
    display_key: Mapped[str | None] = mapped_column(String(300), nullable=True)  # ~1600 px copy
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    device: Mapped[str | None] = mapped_column(String(120), nullable=True)
    proc_step: Mapped[str] = mapped_column(String(16), default="Detecting")
    proc_pct: Mapped[int] = mapped_column(Integer, default=0)
    proc_found: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pinned: Mapped[list[Any]] = mapped_column(JSONType, default=list)  # detection ids pinned by a Return
    current_approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Detection(Base):
    __tablename__ = "detections"
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # "d1", "d2"... unique within a scan
    seq: Mapped[int] = mapped_column(Integer)  # reading order
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    x2: Mapped[float] = mapped_column(Float)
    y2: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(10))  # SURE | CHECK | UNKNOWN | CORRECTED
    assigned_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    det_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    top_matches: Mapped[list[Any]] = mapped_column(JSONType, default=list)  # [{"sku","score"}], best first, up to 5
    corrected_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # "Not a product"
    accepted_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    orig_state: Mapped[str] = mapped_column(String(10), default="SURE")  # what the matcher decided (RESET target)
    orig_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flags: Mapped[list[Any]] = mapped_column(JSONType, default=list)  # blurry / glare / tiny / low_det_conf


class ScanSummary(Base):
    """Frozen reconciliation snapshot written at approval. Later WMS or tolerance edits cannot rewrite history."""
    __tablename__ = "scan_summaries"
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), primary_key=True)
    rows: Mapped[list[Any]] = mapped_column(JSONType)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONType)
    tolerance: Mapped[dict[str, Any]] = mapped_column(JSONType)
    frozen_at: Mapped[datetime] = _created()


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = _id()
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(10))  # APPROVED | RETURNED
    note: Mapped[str] = mapped_column(Text, default="")
    pinned: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # null = automatic (in tolerance)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    override: Mapped[bool] = mapped_column(Boolean, default=False)  # Owner approved own submission
    at: Mapped[datetime] = _created()


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[str] = _id()
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str] = mapped_column(String(64))
    author_name: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = _created()


class ScanEvent(Base):
    __tablename__ = "scan_events"
    id: Mapped[str] = _id()
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = _created()
    actor_name: Mapped[str] = mapped_column(String(200))
    event: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- cross-cutting
class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_at", "user_id", "at"),)
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Append-only. No FKs on purpose: rows must outlive deleted scans and users (a DB trigger blocks UPDATE/DELETE on PostgreSQL)."""
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_org_at", "org_id", "at"),)
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = _created()
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(48), index=True)
    entity: Mapped[str] = mapped_column(String(300))
    scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    before: Mapped[str | None] = mapped_column(Text, nullable=True)
    after: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class OrgSettings(Base):
    __tablename__ = "settings"
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONType)
    tolerance: Mapped[dict[str, Any]] = mapped_column(JSONType)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=365)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
