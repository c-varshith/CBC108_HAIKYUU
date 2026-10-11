"""Counting, variance, tolerance and the SURE/CHECK/UNKNOWN threshold matrix.

Ported 1:1 from the client (lib/selectors.ts, lib/classify.ts). The client recomputes the same numbers
locally for instant feedback, so any drift between the two implementations shows up as flicker."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Protocol


class Det(Protocol):
    id: str
    state: str
    assigned_sku: str | None
    top_matches: list[Any]
    deleted: bool
    accepted_unknown: bool


# ------------------------------------------------------------------ threshold matrix (spec 5.4)
def classify(matches: list[dict[str, Any]], t: dict[str, float]) -> str:
    """SURE: top1 >= sure AND margin to the best *different* SKU >= margin.
    CHECK: check <= top1 < sure, or margin too small (look-alike packs). UNKNOWN: top1 < check."""
    if not matches or matches[0]["score"] < t["check"]:
        return "UNKNOWN"
    top = matches[0]
    other = next((m for m in matches if m["sku"] != top["sku"]), None)
    margin = round(top["score"] - (other["score"] if other else 0.0), 6)  # 0.85 - 0.77 is 0.0799999... in floats: round so "exactly 0.08" passes
    return "SURE" if top["score"] >= t["sure"] and margin >= t["margin"] else "CHECK"


def demote(state: str, det_confidence: float, flags: Iterable[str], det_conf_min: float) -> tuple[str, list[str]]:
    """Low detector confidence or bad crops (tiny / blurry / glare) can never be SURE."""
    flags = list(flags)
    if det_confidence < det_conf_min and "low_det_conf" not in flags:
        flags.append("low_det_conf")
    if state == "SURE" and flags:
        return "CHECK", flags
    return state, flags


# ------------------------------------------------------------------ resolution & counts
def is_resolved(d: Det) -> bool:
    return bool(d.deleted or d.accepted_unknown or d.state in ("SURE", "CORRECTED"))


def is_unresolved(d: Det) -> bool:
    return not is_resolved(d)


def reviewed_counts(dets: Iterable[Det]) -> dict[str, int]:
    m: dict[str, int] = defaultdict(int)
    for d in dets:
        if d.deleted or not d.assigned_sku:
            continue
        if d.state in ("SURE", "CORRECTED"):
            m[d.assigned_sku] += 1
    return dict(m)


def detected_counts(dets: Iterable[Det]) -> dict[str, int]:
    m: dict[str, int] = defaultdict(int)
    for d in dets:
        # A weak top match on an UNKNOWN box is only a suggestion, not an SKU estimate.
        # CHECK boxes remain estimated under their best candidate until staff corrects them.
        sku = d.assigned_sku or (d.top_matches[0]["sku"] if d.state in ("SURE", "CHECK") and d.top_matches else None)
        if d.deleted or d.accepted_unknown or not sku:
            continue
        m[sku] += 1
    return dict(m)


def compute_totals(dets: list[Det]) -> dict[str, int]:
    live = [d for d in dets if not d.deleted]
    return {
        "detected": len(live),
        "sure": sum(1 for d in live if d.state == "SURE"),
        "check": sum(1 for d in live if d.state == "CHECK" and not d.accepted_unknown),
        "unknown": sum(1 for d in live if d.state == "UNKNOWN" and not d.accepted_unknown),
        "unresolved": sum(1 for d in dets if is_unresolved(d)),
    }


def within_tolerance(variance: int, expected: int, tol: dict[str, float]) -> bool:
    ab = abs(variance)
    if ab <= tol["units"]:
        return True
    return expected > 0 and (ab / expected) * 100 <= tol["pct"]


def compute_summary(dets: list[Det], names: dict[str, str], expected: dict[str, int], tol: dict[str, float],
                    manual_counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """variance = reviewed - expected (spec 2.8). Shortfalls first, biggest gap first."""
    rev, det = reviewed_counts(dets), detected_counts(dets)
    overrides = manual_counts or {}
    rows = []
    for sku in set(rev) | set(det) | set(expected) | set(overrides):
        ai_reviewed, exp = rev.get(sku, 0), expected.get(sku, 0)
        reviewed = overrides.get(sku, ai_reviewed)
        variance = reviewed - exp
        rows.append({
            "sku": sku, "name": names.get(sku, sku), "detected": det.get(sku, 0), "reviewed": reviewed,
            "ai_reviewed": ai_reviewed, "quantity_adjusted": sku in overrides,
            "expected": exp, "variance": variance,
            "status": "SHORTFALL" if variance < 0 else "SURPLUS" if variance > 0 else "OK",
            "within_tolerance": within_tolerance(variance, exp, tol),
        })
    rows.sort(key=lambda r: (r["variance"], r["sku"]))  # sku tiebreak keeps the order deterministic
    return rows


def reading_order(boxes: list[tuple[float, float, float, float]]) -> list[int]:
    """Indices of boxes sorted top-to-bottom, then left-to-right (rows found by y-centre clustering).
    "Next uncertain" walks the array, so shelf reading order is what staff expect."""
    if not boxes:
        return []
    heights = sorted(b[3] - b[1] for b in boxes)
    mh = heights[len(heights) // 2] or 1.0
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i][1] + boxes[i][3]) / 2)
    rows: list[list[int]] = []
    ref = None
    for i in order:
        yc = (boxes[i][1] + boxes[i][3]) / 2
        if ref is None or yc - ref > 0.6 * mh:
            rows.append([])
            ref = yc
        rows[-1].append(i)
    return [i for row in rows for i in sorted(row, key=lambda k: boxes[k][0])]
