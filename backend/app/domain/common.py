"""Helpers shared by services: acting identity, audit log, notifications, settings, catalog lookups."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import unprocessable
from app.models import AuditLog, Notification, OrgSettings, Product, User

DEFAULT_THRESHOLDS = {"sure": 0.85, "margin": 0.08, "check": 0.60}
DEFAULT_TOLERANCE = {"pct": 5, "units": 2}


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


@dataclass(frozen=True)
class Actor:
    """Who is doing something, plus where from. Used for audit rows and timeline entries."""
    id: str | None
    name: str
    role: str | None = None
    org_id: str | None = None
    ip: str | None = None
    ua: str | None = None

    @classmethod
    def of(cls, user: User, ip: str | None = None, ua: str | None = None) -> "Actor":
        return cls(user.id, user.name, user.role, user.org_id, ip, ua)


def system_actor(org_id: str) -> Actor:
    return Actor(None, "System", None, org_id)


def user_ref(u: User) -> dict[str, str]:
    return {"id": u.id, "name": u.name, "role": u.role}


# ---------------------------------------------------------------- audit + notifications
def audit(db: AsyncSession, actor: Actor, action: str, entity: str, *, scan_id: str | None = None,
          before: Any = None, after: Any = None, org_id: str | None = None) -> None:
    """Append an audit row (added to the caller's transaction, so it commits or rolls back with the change)."""
    def s(v: Any) -> str | None:
        if v is None:
            return None
        return v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
    db.add(AuditLog(org_id=org_id or actor.org_id or "-", actor_id=actor.id, actor_name=actor.name, action=action,
                    entity=str(entity)[:300], scan_id=scan_id, before=s(before), after=s(after),
                    ip=actor.ip, user_agent=(actor.ua or "")[:300] or None))


async def user_ids_with_roles(db: AsyncSession, org_id: str, roles: tuple[str, ...], exclude: set[str | None] = frozenset()) -> list[str]:
    rows = await db.scalars(select(User.id).where(User.org_id == org_id, User.role.in_(roles), User.active.is_(True)))
    return [i for i in rows if i not in exclude]


def notify(db: AsyncSession, user_ids: list[str] | set[str], kind: str, text: str, scan_id: str | None = None) -> None:
    for uid in dict.fromkeys(user_ids):  # de-duplicate, keep order
        db.add(Notification(user_id=uid, kind=kind, text=text, scan_id=scan_id))


# ---------------------------------------------------------------- settings
async def get_org_settings(db: AsyncSession, org_id: str) -> OrgSettings:
    row = await db.get(OrgSettings, org_id)
    if row is None:
        row = OrgSettings(org_id=org_id, thresholds=dict(DEFAULT_THRESHOLDS), tolerance=dict(DEFAULT_TOLERANCE),
                          auto_approve=False, retention_days=365)
        db.add(row)
        await db.flush()
    return row


def settings_dict(s: OrgSettings) -> dict[str, Any]:
    return {"thresholds": dict(s.thresholds), "tolerance": dict(s.tolerance),
            "auto_approve": s.auto_approve, "retention_days": s.retention_days}


def validate_thresholds(t: dict[str, Any]) -> dict[str, float]:
    try:
        out = {k: float(t[k]) for k in ("sure", "margin", "check")}
    except (KeyError, TypeError, ValueError):
        raise unprocessable("Thresholds need numeric sure, margin and check values.")
    if not (0 < out["check"] < out["sure"] <= 1):
        raise unprocessable("Thresholds must satisfy 0 < check < sure <= 1.")
    if not (0 <= out["margin"] <= 0.5):
        raise unprocessable("Margin must be between 0 and 0.5.")
    return {k: round(v, 4) for k, v in out.items()}


def validate_tolerance(t: dict[str, Any]) -> dict[str, float]:
    try:
        pct, units = float(t["pct"]), float(t["units"])
    except (KeyError, TypeError, ValueError):
        raise unprocessable("Tolerance needs numeric pct and units values.")
    if not (0 <= pct <= 100):
        raise unprocessable("Tolerance percent must be between 0 and 100.")
    if not (0 <= units <= 10000) or units != int(units):
        raise unprocessable("Tolerance units must be a whole number from 0 to 10000.")
    return {"pct": round(pct, 2), "units": int(units)}


# ---------------------------------------------------------------- catalog lookups
async def product_names(db: AsyncSession, org_id: str) -> dict[str, str]:
    """sku -> name for ALL products, including deactivated ones, so old scans still read correctly."""
    rows = await db.execute(select(Product.sku, Product.name).where(Product.org_id == org_id))
    return {sku: name for sku, name in rows}


async def active_catalog(db: AsyncSession, org_id: str) -> list[tuple[str, str]]:
    rows = await db.execute(select(Product.sku, Product.name).where(Product.org_id == org_id, Product.active.is_(True)).order_by(Product.sku))
    return [(sku, name) for sku, name in rows]
