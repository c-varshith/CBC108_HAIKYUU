"""Pure-logic tests: no HTTP, no database."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.config import Settings
from app.domain import exports
from app.domain.processing import build_detection_rows
from app.domain.selectors import (classify, compute_summary, compute_totals, demote, is_unresolved, reading_order, reviewed_counts,
                                  within_tolerance)
from app.domain.statemachine import EDITABLE, LOCKED, SUBMITTABLE, TRANSITIONS, can_transition
from app.infra.inference import HttpInference, RawDetection, RawMatch, StubInference, make_inference
from app.permissions import CAPS, approval_rule, can, can_delete_scan

T = {"sure": 0.85, "margin": 0.08, "check": 0.60}


def test_real_worker_is_the_default_and_stub_requires_explicit_selection(monkeypatch):
    monkeypatch.delenv("INFERENCE_BACKEND", raising=False)
    monkeypatch.delenv("AI_WORKER_URL", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.inference_backend == "http"
    assert defaults.ai_worker_url == "http://localhost:8001"
    assert isinstance(make_inference(defaults), HttpInference)
    assert isinstance(make_inference(Settings(_env_file=None, inference_backend="stub")), StubInference)


def det(state="SURE", sku="A", matches=None, deleted=False, accepted=False, i="d1"):
    return NS(id=i, state=state, assigned_sku=sku, top_matches=matches or [], deleted=deleted, accepted_unknown=accepted)


# ---------------------------------------------------------------- threshold matrix (spec 5.4)
@pytest.mark.parametrize("matches,expected", [
    ([{"sku": "A", "score": 0.91}, {"sku": "B", "score": 0.70}], "SURE"),
    ([{"sku": "A", "score": 0.85}, {"sku": "B", "score": 0.77}], "SURE"),     # exactly on both limits
    ([{"sku": "A", "score": 0.90}, {"sku": "B", "score": 0.85}], "CHECK"),    # look-alike: margin 0.05 < 0.08
    ([{"sku": "A", "score": 0.84}, {"sku": "B", "score": 0.30}], "CHECK"),    # below sure
    ([{"sku": "A", "score": 0.60}], "CHECK"),                                  # exactly on check
    ([{"sku": "A", "score": 0.59}], "UNKNOWN"),
    ([], "UNKNOWN"),
    ([{"sku": "A", "score": 0.95}, {"sku": "A", "score": 0.94}, {"sku": "B", "score": 0.50}], "SURE"),  # same-SKU runner-up is not a rival
])
def test_classify(matches, expected):
    assert classify(matches, T) == expected


def test_demote_blocks_sure_for_bad_crops():
    assert demote("SURE", 0.9, [], 0.5) == ("SURE", [])
    assert demote("SURE", 0.4, [], 0.5) == ("CHECK", ["low_det_conf"])
    assert demote("SURE", 0.9, ["blurry"], 0.5) == ("CHECK", ["blurry"])
    assert demote("UNKNOWN", 0.4, [], 0.5)[0] == "UNKNOWN"


# ---------------------------------------------------------------- counting
def test_resolution_rules():
    assert not is_unresolved(det("SURE"))
    assert not is_unresolved(det("CORRECTED"))
    assert is_unresolved(det("CHECK")) and is_unresolved(det("UNKNOWN"))
    assert not is_unresolved(det("CHECK", deleted=True))
    assert not is_unresolved(det("UNKNOWN", accepted=True))


def test_reviewed_counts_only_sure_and_corrected_with_a_sku():
    dets = [det("SURE", "A"), det("CORRECTED", "A"), det("CHECK", "A"), det("SURE", "B", deleted=True), det("CORRECTED", None, accepted=True)]
    assert reviewed_counts(dets) == {"A": 2}


def test_totals():
    dets = [det("SURE", i="1"), det("CHECK", i="2"), det("UNKNOWN", i="3"), det("UNKNOWN", accepted=True, i="4"), det("CHECK", deleted=True, i="5")]
    assert compute_totals(dets) == {"detected": 4, "sure": 1, "check": 1, "unknown": 1, "unresolved": 2}


@pytest.mark.parametrize("variance,expected,tol,ok", [
    (0, 10, {"pct": 5, "units": 2}, True), (-2, 100, {"pct": 0, "units": 2}, True), (-3, 100, {"pct": 0, "units": 2}, False),
    (-5, 100, {"pct": 5, "units": 0}, True), (-6, 100, {"pct": 5, "units": 0}, False), (3, 0, {"pct": 50, "units": 2}, False),
])
def test_within_tolerance(variance, expected, tol, ok):
    assert within_tolerance(variance, expected, tol) is ok


def test_summary_variance_status_and_ordering():
    dets = [det("SURE", "A"), det("SURE", "A"), det("CORRECTED", "B"), det("CHECK", None, [{"sku": "C", "score": 0.7}])]
    rows = compute_summary(dets, {"A": "Alpha", "B": "Beta", "C": "Gamma", "D": "Delta"}, {"A": 5, "B": 1, "D": 4}, {"pct": 5, "units": 0})
    by = {r["sku"]: r for r in rows}
    assert by["A"]["detected"] == 2 and by["A"]["reviewed"] == 2
    assert by["A"]["variance"] == -3 and by["A"]["status"] == "SHORTFALL"
    assert by["B"]["variance"] == 0 and by["B"]["status"] == "OK" and by["B"]["within_tolerance"] is True
    assert by["C"]["detected"] == 1 and by["C"]["reviewed"] == 0  # a CHECK box is detected, not yet reviewed
    assert by["D"]["variance"] == -4  # expected but never seen = shortfall
    assert [r["sku"] for r in rows] == ["D", "A", "B", "C"]  # shortfalls first, biggest gap first


def test_reading_order_is_rows_top_to_bottom_then_left_to_right():
    boxes = [(300, 10, 340, 60), (10, 12, 50, 62), (150, 8, 190, 58), (20, 200, 60, 250), (200, 205, 240, 255)]
    assert reading_order(boxes) == [1, 2, 0, 3, 4]
    assert reading_order([]) == []


def test_build_detection_rows_clamps_drops_and_classifies():
    raw = [
        RawDetection((10, 10, 110, 210), 0.95, [RawMatch("A", 0.93), RawMatch("B", 0.5)]),          # SURE
        RawDetection((130, 10, 230, 210), 0.95, [RawMatch("A", 0.70), RawMatch("B", 0.69)]),        # CHECK
        RawDetection((250, 10, 350, 210), 0.95, [RawMatch("A", 0.20)]),                              # UNKNOWN
        RawDetection((370, 10, 470, 210), 0.30, [RawMatch("A", 0.99), RawMatch("B", 0.1)]),         # low detector confidence -> CHECK
        RawDetection((-50, -50, 40, 40), 0.9, [RawMatch("GHOST", 0.99), RawMatch("A", 0.9), RawMatch("B", 0.1)]),  # clamped; unknown SKU ignored
        RawDetection((100, 100, 101, 101), 0.9, [RawMatch("A", 0.9)]),                               # 1px: dropped
    ]
    rows = build_detection_rows(raw, width=600, height=300, valid_skus={"A", "B"}, thresholds=T, det_conf_min=0.5, max_n=100)
    assert len(rows) == 5 and [r["id"] for r in rows] == ["d1", "d2", "d3", "d4", "d5"]
    by_state = sorted(r["state"] for r in rows)
    assert by_state == ["CHECK", "CHECK", "SURE", "SURE", "UNKNOWN"]
    clamped = next(r for r in rows if r["x1"] == 0.0)
    assert clamped["top_matches"][0]["sku"] == "A" and all(m["sku"] != "GHOST" for m in clamped["top_matches"])
    low = next(r for r in rows if r["x1"] == 370)
    assert low["state"] == "CHECK" and "low_det_conf" in low["flags"] and low["assigned_sku"] is None
    sure = next(r for r in rows if r["x1"] == 10)
    assert sure["assigned_sku"] == "A" and sure["needs_review"] is False and sure["orig_state"] == "SURE"


# ---------------------------------------------------------------- state machine
def test_state_machine_main_road_and_dead_ends():
    path = ["DRAFT", "UPLOADED", "PROCESSING", "NEEDS_REVIEW", "IN_REVIEW", "SUBMITTED", "APPROVED"]
    assert all(can_transition(a, b) for a, b in zip(path, path[1:]))
    assert can_transition("PROCESSING", "AUTO_READY") and can_transition("AUTO_READY", "SUBMITTED")
    assert can_transition("SUBMITTED", "RETURNED") and can_transition("RETURNED", "IN_REVIEW")
    assert can_transition("APPROVED", "REOPENED") and can_transition("FAILED", "UPLOADED")
    assert not can_transition("APPROVED", "IN_REVIEW") and not can_transition("DRAFT", "APPROVED")
    assert not can_transition("ARCHIVED", "DRAFT") and not can_transition("SUBMITTED", "IN_REVIEW")
    assert not (EDITABLE & LOCKED) and SUBMITTABLE <= set(TRANSITIONS)


# ---------------------------------------------------------------- permissions (spec 1.2 matrix)
def test_permission_matrix():
    expect = {
        "scan.capture": "OWNER MANAGER STAFF AUDITOR", "scan.view": "OWNER MANAGER STAFF AUDITOR", "scan.review": "OWNER MANAGER STAFF AUDITOR",
        "scan.submit": "OWNER MANAGER STAFF AUDITOR", "scan.approve": "OWNER MANAGER", "scan.reopen": "OWNER MANAGER",
        "catalog.write": "OWNER MANAGER", "stock.write": "OWNER MANAGER", "alerts.write": "OWNER MANAGER",
        "settings.thresholds": "OWNER", "users.manage": "OWNER", "audit.view": "OWNER MANAGER AUDITOR",
        "reports.export": "OWNER MANAGER AUDITOR", "catalog.request": "STAFF",
    }
    for cap, roles in expect.items():
        for role in ("OWNER", "MANAGER", "STAFF", "AUDITOR"):
            assert can(role, cap) == (role in roles.split()), (cap, role)
    assert set(expect) <= set(CAPS)
    assert not can(None, "scan.view")


def test_delete_rule():
    assert can_delete_scan("OWNER", "APPROVED", "u1", "u2")
    assert can_delete_scan("STAFF", "DRAFT", "u1", "u1")
    assert not can_delete_scan("STAFF", "DRAFT", "u1", "u2")
    assert not can_delete_scan("STAFF", "SUBMITTED", "u1", "u1")
    assert not can_delete_scan("MANAGER", "DRAFT", "u1", "u1") and not can_delete_scan("AUDITOR", "DRAFT", "u1", "u1")


def test_approval_rule_separation_of_duties():
    assert approval_rule("MANAGER", "staff1", "m1").allowed
    r = approval_rule("MANAGER", "m1", "m1")
    assert not r.allowed and "Separation" in (r.reason or "")
    r = approval_rule("OWNER", "o1", "o1")
    assert r.allowed and r.override
    assert not approval_rule("STAFF", None, "s1").allowed and not approval_rule("AUDITOR", None, "a1").allowed


# ---------------------------------------------------------------- exports
def test_csv_cells_are_neutralised_against_formula_injection():
    assert exports._cell("=SUM(A1)") == "'=SUM(A1)" and exports._cell("@cmd") == "'@cmd" and exports._cell("+1") == "'+1"
    assert exports._cell("Honey Jar") == "Honey Jar" and exports._cell(-3) == -3  # numbers stay numeric
    view = {"scan_id": "s1", "status": "APPROVED", "created_by": {"name": "=evil()"},
            "summary": [{"sku": "A", "name": "-x", "detected": 1, "reviewed": 1, "expected": 2, "variance": -1, "status": "SHORTFALL", "within_tolerance": True}]}
    text = exports.scan_csv(view, "Shelf").decode("utf-8-sig")
    assert "'=evil()" in text and "'-x" in text
