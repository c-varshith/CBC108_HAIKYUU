"""End-to-end scan lifecycle through the real API (stub inference, in-memory DB)."""
from __future__ import annotations

from app.infra.inference import RawDetection, RawMatch
from tests.helpers import (
    first_shelf,
    get_scan,
    login,
    make_jpeg,
    resolve_all,
    submitted_scan,
    upload_scan,
)


async def test_staff_to_approval_happy_path(seeded):
    c = seeded
    staff, mgr, owner = await login(c, "staff"), await login(c, "manager"), await login(c, "owner")
    sid = await upload_scan(c, staff)
    v = await get_scan(c, staff, sid)
    assert v["status"] == "NEEDS_REVIEW" and v["totals"]["unresolved"] > 0
    assert v["permissions"] == {"can_edit": True, "can_submit": True, "can_approve": False, "can_reopen": False}

    # cannot submit while anything is unresolved
    r = await c.post(f"/scans/{sid}/submit", headers=staff, json={})
    assert r.status_code == 409 and "still need a decision" in r.text

    v = await resolve_all(c, staff, sid)
    assert v["status"] == "IN_REVIEW" and v["permissions"]["can_submit"] is True
    r = await c.post(f"/scans/{sid}/submit", headers=staff, json={"note": "all checked"})
    assert r.status_code == 200 and r.json()["status"] == "SUBMITTED"

    # staff cannot approve; manager can; approved scans are locked
    assert (await c.post(f"/scans/{sid}/approve", headers=staff)).status_code == 403
    r = await c.post(f"/scans/{sid}/approve", headers=mgr)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    v = r.json()
    assert v["approval"]["decision"] == "APPROVED" and v["approval"]["decided_by"]["role"] == "MANAGER"
    assert v["permissions"]["can_edit"] is False
    ids = [d["id"] for d in v["detections"]][:1]
    r = await c.patch(f"/scans/{sid}/detections", headers=mgr, json={"ids": ids, "action": "NOT_A_PRODUCT"})
    assert r.status_code == 409 and "locked" in r.text

    # reopen needs a reason
    assert (await c.post(f"/scans/{sid}/reopen", headers=mgr, json={"reason": " "})).status_code == 422
    r = await c.post(f"/scans/{sid}/reopen", headers=owner, json={"reason": "Wrong shelf selected"})
    assert r.status_code == 200 and r.json()["status"] == "REOPENED"

    # every transition is in the audit log, and the timeline tells the story
    log = (await c.get(f"/audit-log?scan_id={sid}&limit=200", headers=owner)).json()
    actions = {a["action"] for a in log}
    assert {"UPLOADED", "NEEDS_REVIEW", "DETECTIONS_CORRECTED", "SUBMITTED", "APPROVED", "REOPENED"} <= actions
    events = [t["event"] for t in v["timeline"]]
    assert events[:2] == ["UPLOADED", "PROCESSING"] and "SUBMITTED" in events and "APPROVED" in events


async def test_list_totals_match_detail_totals(seeded):
    c = seeded
    staff = await login(c, "staff")
    sid = await upload_scan(c, staff, seed=3)
    for stage in range(2):
        v = await get_scan(c, staff, sid)
        item = next(i for i in (await c.get("/scans", headers=staff)).json() if i["scan_id"] == sid)
        assert item["totals"] == v["totals"]  # SQL twin and Python selectors must agree
        if stage == 0:
            ids = [d["id"] for d in v["detections"] if d["state"] != "SURE"][:3]
            await c.patch(f"/scans/{sid}/detections", headers=staff, json={"ids": ids, "action": "NOT_A_PRODUCT"})


async def test_live_camera_frames_use_org_catalog_without_creating_scans(seeded):
    class FixedInference:
        async def ping(self):
            return True

        async def detect(self, image, **kwargs):
            assert ("SKU-001", "Canned Goods") in kwargs["catalog"]
            return [
                RawDetection((20, 20, 120, 180), 0.98, [RawMatch("SKU-001", 0.97)]),
                RawDetection((140, 20, 240, 180), 0.92, [RawMatch("foreign-org-sku", 0.99)]),
            ]

    c = seeded
    staff = await login(c, "staff")
    c.app.state.settings = c.app.state.settings.model_copy(update={"inference_backend": "http"})
    c.app.state.inference = FixedInference()

    status = await c.get("/scans/live-status", headers=staff)
    assert status.status_code == 200 and status.json() == {"available": True, "reason": None}
    before = (await c.get("/scans", headers=staff)).json()
    response = await c.post("/scans/live-detect", headers=staff,
                            files={"image": ("frame.jpg", make_jpeg(3, (800, 600)), "image/jpeg")})
    assert response.status_code == 200
    body = response.json()
    assert body["width"] == 800 and body["height"] == 600
    assert len(body["detections"]) == 2
    assert {item["sku"]: item["quantity"] for item in body["estimates"]} == {"SKU-001": 1, None: 1}
    after = (await c.get("/scans", headers=staff)).json()
    assert [item["scan_id"] for item in before] == [item["scan_id"] for item in after]


async def test_live_camera_requires_real_worker_and_capture_permission(seeded):
    c = seeded
    staff, auditor = await login(c, "staff"), await login(c, "auditor")
    status = await c.get("/scans/live-status", headers=staff)
    assert status.status_code == 200 and status.json()["available"] is False
    frame = {"image": ("frame.jpg", make_jpeg(3, (800, 600)), "image/jpeg")}
    response = await c.post("/scans/live-detect", headers=staff, files=frame)
    assert response.status_code == 503 and "requires the real AI worker" in response.text
    assert (await c.get("/scans/live-status", headers=auditor)).status_code == 403
    assert (await c.get("/scans/live-status")).status_code == 401


async def test_return_with_pinned_boxes_then_resubmit(seeded):
    c = seeded
    staff, mgr = await login(c, "staff"), await login(c, "manager")
    sid = await submitted_scan(c, staff)
    box = (await get_scan(c, mgr, sid))["detections"][0]["id"]
    assert (await c.post(f"/scans/{sid}/return", headers=mgr, json={"note": "", "pinned": []})).status_code == 422
    assert (await c.post(f"/scans/{sid}/return", headers=mgr, json={"note": "x", "pinned": ["nope"]})).status_code == 422
    r = await c.post(f"/scans/{sid}/return", headers=mgr, json={"note": "Recount the top row", "pinned": [box]})
    assert r.status_code == 200 and r.json()["status"] == "RETURNED"

    v = await get_scan(c, staff, sid)
    assert v["approval"]["decision"] == "RETURNED" and v["approval"]["note"] == "Recount the top row"
    assert v["pinned_detection_ids"] == [box]
    notes = (await c.get("/notifications", headers=staff)).json()
    assert any(n["kind"] == "RETURNED" and n["scan_id"] == sid for n in notes["items"])

    r = await c.post(f"/scans/{sid}/submit", headers=staff, json={})
    assert r.status_code == 200 and r.json()["status"] == "SUBMITTED"


async def test_separation_of_duties_and_flagged_owner_override(seeded):
    c = seeded
    mgr, owner = await login(c, "manager"), await login(c, "owner")
    sid = await submitted_scan(c, mgr)  # a manager may submit...
    r = await c.post(f"/scans/{sid}/approve", headers=mgr)  # ...but never approve their own submission
    assert r.status_code == 403 and "Separation of duties" in r.text
    assert (await get_scan(c, mgr, sid))["permissions"]["can_approve"] is False

    r = await c.post(f"/scans/{sid}/approve", headers=owner)  # a different approver is fine
    assert r.status_code == 200

    sid2 = await submitted_scan(c, owner, seed=7)  # the Owner may approve their own, but it is flagged
    r = await c.post(f"/scans/{sid2}/approve", headers=owner)
    assert r.status_code == 200
    log = (await c.get(f"/audit-log?scan_id={sid2}&action=APPROVED_OVERRIDE", headers=owner)).json()
    assert len(log) == 1


async def test_approvals_inbox_marks_own_submissions(seeded):
    c = seeded
    staff, mgr = await login(c, "staff"), await login(c, "manager")
    a = await submitted_scan(c, staff, seed=1)
    b = await submitted_scan(c, mgr, seed=2)
    inbox = {i["scan_id"]: i for i in (await c.get("/approvals", headers=mgr)).json()}
    assert inbox[a]["can_approve"] is True and inbox[b]["can_approve"] is False
    assert (await c.get("/approvals", headers=staff)).status_code == 403


async def test_auto_approve_only_when_owner_enabled_and_within_tolerance(seeded):
    c = seeded
    staff, owner = await login(c, "staff"), await login(c, "owner")
    sid = await submitted_scan(c, staff, seed=1)
    assert (await get_scan(c, owner, sid))["status"] == "SUBMITTED"  # policy off: always waits for a human

    assert (await c.put("/settings/tolerance", headers=owner, json={"pct": 100, "units": 10000})).status_code == 200
    assert (await c.put("/settings/approval-policy", headers=owner, json={"auto_approve": True})).status_code == 200
    sid2 = await submitted_scan(c, staff, seed=2)
    v = await get_scan(c, owner, sid2)
    assert v["status"] == "APPROVED" and v["approval"]["decided_by"]["id"] == "system"

    # tight tolerance: outside tolerance still needs a manager even with the policy on
    assert (await c.put("/settings/tolerance", headers=owner, json={"pct": 0, "units": 0})).status_code == 200
    sid3 = await submitted_scan(c, staff, seed=3)
    assert (await get_scan(c, owner, sid3))["status"] == "SUBMITTED"


async def test_delete_rules_and_upload_validation(seeded):
    c = seeded
    staff, owner = await login(c, "staff"), await login(c, "owner")
    sid = await upload_scan(c, staff)
    assert (await c.delete(f"/scans/{sid}", headers=staff)).status_code == 403  # staff: own DRAFTS only
    assert (await c.delete(f"/scans/{sid}", headers=owner)).status_code == 204
    assert (await c.get(f"/scans/{sid}", headers=owner)).status_code == 404

    shelf = (await c.get("/shelves", headers=staff)).json()[0]["id"]
    r = await c.post("/scans", headers=staff, data={"shelf_id": shelf}, files=[("files", ("x.jpg", b"not an image", "image/jpeg"))])
    assert 400 <= r.status_code < 500
    r = await c.post("/scans", headers=staff, data={"shelf_id": "missing"}, files=[("files", ("x.jpg", make_jpeg(), "image/jpeg"))])
    assert r.status_code == 404


async def test_multi_photo_upload_creates_one_scan_each(seeded):
    c = seeded
    staff = await login(c, "staff")
    shelf = (await c.get("/shelves", headers=staff)).json()[0]["id"]
    files = [("files", (f"{i}.jpg", make_jpeg(i), "image/jpeg")) for i in (1, 2, 3)]
    r = await c.post("/scans", headers=staff, data={"shelf_id": shelf}, files=files)
    assert r.status_code == 201 and len(r.json()["scan_ids"]) == 3
    await c.app.state.runner.wait_idle()
    assert len((await c.get("/scans", headers=staff)).json()) == 3


async def test_scan_upload_replay_returns_original_scan_without_duplicate(seeded):
    c = seeded
    staff = await login(c, "staff")
    shelf = await first_shelf(c, staff)
    image = make_jpeg()
    form = {"shelf_id": shelf, "request_ids": '["offline-retry-1"]'}

    first = await c.post("/scans", headers=staff, data=form,
                         files=[("files", ("shelf.jpg", image, "image/jpeg"))])
    replay = await c.post("/scans", headers=staff, data=form,
                          files=[("files", ("shelf.jpg", image, "image/jpeg"))])
    assert first.status_code == replay.status_code == 201
    assert replay.json()["scan_ids"] == first.json()["scan_ids"]
    listed = await c.get("/scans", headers=staff)
    assert sum(row["scan_id"] == first.json()["scan_id"] for row in listed.json()) == 1


async def test_reconciliation_variance_is_reviewed_minus_expected(seeded):
    c = seeded
    staff = await login(c, "staff")
    sid = await upload_scan(c, staff)
    v = await resolve_all(c, staff, sid)
    assert v["summary"], "stub catalog produces counts"
    for row in v["summary"]:
        assert row["variance"] == row["reviewed"] - row["expected"]
        assert row["status"] == ("SHORTFALL" if row["variance"] < 0 else "SURPLUS" if row["variance"] > 0 else "OK")


async def test_export_roles_and_formats(seeded):
    c = seeded
    staff, mgr, auditor = await login(c, "staff"), await login(c, "manager"), await login(c, "auditor")
    sid = await submitted_scan(c, staff)
    assert (await c.get(f"/scans/{sid}/export?format=csv", headers=staff)).status_code == 403
    r = await c.get(f"/scans/{sid}/export?format=csv", headers=auditor)
    assert r.status_code == 200 and "sku,name,detected" in r.text
    r = await c.get(f"/scans/{sid}/export?format=pdf", headers=mgr)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
