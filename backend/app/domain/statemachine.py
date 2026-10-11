"""Scan lifecycle (spec 1.3), server side.

Superset of the client's lib/statusMachine.ts: the client table lists the "main road", while the server
must also accept what the client's `permissions.can_submit` allows (RETURNED/REOPENED/NEEDS_REVIEW with
nothing left unresolved can be submitted directly, without a pointless edit first)."""
from __future__ import annotations

TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"UPLOADED", "ARCHIVED"},
    "UPLOADED": {"PROCESSING", "FAILED"},
    "PROCESSING": {"NEEDS_REVIEW", "AUTO_READY", "FAILED"},
    "NEEDS_REVIEW": {"IN_REVIEW", "SUBMITTED"},
    "IN_REVIEW": {"SUBMITTED"},
    "AUTO_READY": {"SUBMITTED", "IN_REVIEW"},
    "SUBMITTED": {"APPROVED", "RETURNED"},
    "RETURNED": {"IN_REVIEW", "SUBMITTED"},
    "APPROVED": {"REOPENED", "ARCHIVED"},
    "REOPENED": {"IN_REVIEW", "SUBMITTED"},
    "FAILED": {"UPLOADED"},
    "ARCHIVED": set(),
}
STATUSES = tuple(TRANSITIONS)
EDITABLE = {"NEEDS_REVIEW", "IN_REVIEW", "AUTO_READY", "RETURNED", "REOPENED"}
SUBMITTABLE = {"NEEDS_REVIEW", "IN_REVIEW", "AUTO_READY", "RETURNED", "REOPENED"}
LOCKED = {"APPROVED", "ARCHIVED"}


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())
