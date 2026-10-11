"""The permission matrix (spec 1.2) enforced by the API. 403s come from the route guard, before any lookup."""
from __future__ import annotations

import pytest

from tests.helpers import login, make_jpeg

# (role, method, path, json body or None)  ->  must be 403
FORBIDDEN = [
    ("auditor", "POST", "/scans", None),
    ("auditor", "PATCH", "/scans/x/detections", {"ids": ["d1"], "action": "CONFIRM"}),
    ("auditor", "POST", "/scans/x/submit", {}),
    ("auditor", "POST", "/scans/x/comments", {"text": "hi"}),
    ("auditor", "POST", "/scans/x/approve", None),
    ("auditor", "POST", "/catalog/products", {"sku": "A1", "name": "A"}),
    ("auditor", "PUT", "/wms/expected", {"shelf_id": "s", "sku": "A1", "expected_qty": 1}),
    ("staff", "POST", "/scans/x/approve", None),
    ("staff", "POST", "/scans/x/return", {"note": "n", "pinned": []}),
    ("staff", "POST", "/scans/x/reopen", {"reason": "r"}),
    ("staff", "GET", "/scans/x/export?format=csv", None),
    ("staff", "GET", "/approvals", None),
    ("staff", "POST", "/catalog/products", {"sku": "A1", "name": "A"}),
    ("staff", "PATCH", "/catalog/products/SKU-001", {"name": "Z"}),
    ("staff", "POST", "/catalog/products/SKU-001/references", None),
    ("staff", "POST", "/catalog/products/SKU-001/test-recognition", None),
    ("auditor", "POST", "/catalog/products/SKU-001/test-recognition", None),
    ("staff", "PUT", "/wms/expected", {"shelf_id": "s", "sku": "A1", "expected_qty": 1}),
    ("staff", "POST", "/wms/expected/import", None),
    ("staff", "GET", "/audit-log", None),
    ("staff", "GET", "/users", None),
    ("staff", "PUT", "/settings/thresholds", {"sure": 0.9, "margin": 0.1, "check": 0.5}),
    ("manager", "PUT", "/settings/thresholds", {"sure": 0.9, "margin": 0.1, "check": 0.5}),
    ("manager", "PUT", "/settings/tolerance", {"pct": 1, "units": 1}),
    ("manager", "PUT", "/settings/approval-policy", {"auto_approve": True}),
    ("manager", "POST", "/users/invite", {"email": "x@example.com", "role": "STAFF"}),
    ("manager", "POST", "/stores", {"name": "S"}),
    ("manager", "DELETE", "/scans/x", None),
    ("auditor", "DELETE", "/scans/x", None),
    ("auditor", "POST", "/stores", {"name": "S"}),
    # catalog index: resync is Owner-only; reindex / review crops / index health / batches need catalog.write
    ("manager", "POST", "/catalog/resync", None),
    ("staff", "POST", "/catalog/resync", None),
    ("auditor", "POST", "/catalog/resync", None),
    ("staff", "POST", "/catalog/products/SKU-001/reindex", None),
    ("auditor", "POST", "/catalog/products/SKU-001/reindex", None),
    ("staff", "POST", "/catalog/products/SKU-001/references/from-detection", {"scan_id": "x", "detection_id": "d1"}),
    ("auditor", "POST", "/catalog/products/SKU-001/references/from-detection", {"scan_id": "x", "detection_id": "d1"}),
    ("staff", "GET", "/catalog/index-status", None),
    ("auditor", "GET", "/catalog/index-status", None),
    ("staff", "GET", "/catalog/batches", None),
    ("auditor", "GET", "/catalog/batches", None),
    ("staff", "GET", "/catalog/batches/x", None),
    ("auditor", "GET", "/catalog/batches/x", None),
]
# allowed: must NOT be 401/403
ALLOWED = [
    ("staff", "GET", "/scans/live-status"),
    ("auditor", "GET", "/scans"), ("auditor", "GET", "/audit-log"), ("auditor", "GET", "/reports/summary"), ("auditor", "GET", "/catalog/products"),
    ("manager", "GET", "/approvals"), ("manager", "GET", "/audit-log"), ("manager", "GET", "/scans"),
    ("staff", "GET", "/scans"), ("staff", "GET", "/catalog/products"), ("staff", "GET", "/shelves"), ("owner", "GET", "/users"),
    ("staff", "GET", "/catalog/products?slim=true&limit=5&q=a&status=ready"), ("auditor", "GET", "/catalog/credits"), ("staff", "GET", "/catalog/credits"),
    ("manager", "GET", "/catalog/index-status"), ("manager", "GET", "/catalog/batches"), ("owner", "GET", "/catalog/index-status"),
]


@pytest.mark.parametrize("role,method,path,body", FORBIDDEN)
async def test_forbidden(seeded, role, method, path, body):
    h = await login(seeded, role)
    if body is None and method in ("POST", "PUT", "PATCH"):
        r = await seeded.request(method, path, headers=h, files=[("files", ("a.jpg", make_jpeg(), "image/jpeg"))] if "references" in path or path == "/scans" else None)
    else:
        r = await seeded.request(method, path, headers=h, json=body)
    assert r.status_code == 403, f"{role} {method} {path} -> {r.status_code} {r.text[:120]}"


@pytest.mark.parametrize("role,method,path", ALLOWED)
async def test_allowed(seeded, role, method, path):
    h = await login(seeded, role)
    r = await seeded.request(method, path, headers=h)
    assert r.status_code not in (401, 403), f"{role} {method} {path} -> {r.status_code}"


async def test_every_route_needs_a_session(seeded):
    for method, path in [("GET", "/scans"), ("GET", "/scans/x"), ("GET", "/scans/live-status"), ("POST", "/scans/live-detect"), ("GET", "/approvals"), ("GET", "/catalog/products"), ("GET", "/wms/expected"),
                         ("GET", "/audit-log"), ("GET", "/notifications"), ("GET", "/settings"), ("GET", "/users"), ("GET", "/reports/summary"),
                         ("GET", "/catalog/credits"), ("GET", "/catalog/index-status"), ("GET", "/catalog/batches"), ("GET", "/catalog/batches/x"),
                         ("POST", "/catalog/resync"), ("POST", "/catalog/products/SKU-001/reindex"),
                         ("POST", "/catalog/products/SKU-001/test-recognition"),
                         ("POST", "/catalog/products/SKU-001/references/from-detection")]:
        assert (await seeded.request(method, path)).status_code == 401, path


async def test_staff_only_touches_own_scans(seeded):
    from tests.helpers import get_scan, upload_scan
    c = seeded
    staff, mgr = await login(c, "staff"), await login(c, "manager")
    other = await upload_scan(c, mgr, seed=5)  # a scan the staff member did not create
    v = await get_scan(c, staff, other)  # viewing by id follows the matrix (Staff can view)
    assert v["permissions"]["can_edit"] is False
    ids = [d["id"] for d in v["detections"]][:1]
    assert (await c.patch(f"/scans/{other}/detections", headers=staff, json={"ids": ids, "action": "NOT_A_PRODUCT"})).status_code == 403
    assert (await c.post(f"/scans/{other}/submit", headers=staff, json={})).status_code == 403
    assert other not in [i["scan_id"] for i in (await c.get("/scans", headers=staff)).json()]  # list = own scans
