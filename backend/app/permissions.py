"""RBAC matrix (spec 1.2). Direct port of the client's lib/permissions.ts so both sides agree.
The server is the authority: every route is guarded by `require_permission` in deps.py."""
from __future__ import annotations

from dataclasses import dataclass

ROLES = ("OWNER", "MANAGER", "STAFF", "AUDITOR")

CAPS: dict[str, tuple[str, ...]] = {
    "scan.capture": ("OWNER", "MANAGER", "STAFF"),
    "scan.view": ("OWNER", "MANAGER", "STAFF", "AUDITOR"),
    "scan.review": ("OWNER", "MANAGER", "STAFF"),
    "scan.submit": ("OWNER", "MANAGER", "STAFF"),
    "scan.approve": ("OWNER", "MANAGER"),
    "scan.reopen": ("OWNER", "MANAGER"),
    "scan.delete": ("OWNER", "STAFF"),  # STAFF: own drafts only, see can_delete_scan
    "catalog.write": ("OWNER", "MANAGER"),
    "catalog.request": ("STAFF",),
    "catalog.resync": ("OWNER",),  # re-send every product's photos to the matching service (heavy)
    "stock.write": ("OWNER", "MANAGER"),
    "alerts.write": ("OWNER", "MANAGER"),
    "settings.thresholds": ("OWNER",),
    "users.manage": ("OWNER",),
    "audit.view": ("OWNER", "MANAGER", "AUDITOR"),
    "reports.export": ("OWNER", "MANAGER", "AUDITOR"),
}


def can(role: str | None, cap: str) -> bool:
    return bool(role) and role in CAPS[cap]


def can_delete_scan(role: str, scan_status: str, created_by: str, user_id: str) -> bool:
    if role == "OWNER":
        return True
    if role == "STAFF":
        return scan_status == "DRAFT" and created_by == user_id
    return False


@dataclass(frozen=True)
class ApprovalRule:
    allowed: bool
    override: bool = False
    reason: str | None = None


def approval_rule(role: str, submitter_id: str | None, user_id: str) -> ApprovalRule:
    """Separation of duties: nobody approves their own submission; the Owner may, flagged as an override."""
    if not can(role, "scan.approve"):
        return ApprovalRule(False, False, "Your role cannot approve scans.")
    if submitter_id and submitter_id == user_id:
        if role == "OWNER":
            return ApprovalRule(True, True)
        return ApprovalRule(False, False, "Separation of duties: you cannot approve a scan you submitted.")
    return ApprovalRule(True, False)
