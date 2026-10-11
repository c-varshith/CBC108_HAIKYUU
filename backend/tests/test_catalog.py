from __future__ import annotations

from tests.helpers import first_shelf, login, make_jpeg


async def test_manager_registers_product_from_reference_photos(seeded):
    c = seeded
    mgr, staff = await login(c, "manager"), await login(c, "staff")
    r = await c.post("/catalog/products", headers=mgr, json={"sku": "SKU-100", "name": "Tea Tin", "min_qty": 4})
    assert r.status_code == 201 and r.json()["reference_count"] == 0
    assert (await c.post("/catalog/products", headers=mgr, json={"sku": "sku-100", "name": "Dup"})).status_code == 409  # case-insensitive
    assert (await c.post("/catalog/products", headers=mgr, json={"sku": "bad sku!", "name": "X"})).status_code == 422
    assert (await c.post("/catalog/products", headers=staff, json={"sku": "SKU-101", "name": "X"})).status_code == 403

    files = [("files", (f"{i}.jpg", make_jpeg(i), "image/jpeg")) for i in (1, 2, 3)]
    r = await c.post("/catalog/products/SKU-100/references", headers=mgr, files=files)
    assert r.status_code == 201 and r.json()["reference_count"] == 3
    assert (await c.post("/catalog/products/SKU-100/references", headers=mgr, files=[("files", ("x.jpg", b"nope", "image/jpeg"))])).status_code >= 400
    assert (await c.get("/catalog/products/SKU-100", headers=staff)).json()["reference_count"] == 3  # staff can read the catalog

    thumb = r.json()["references"][0]["thumb_url"]
    img = await c.get(thumb.replace("/api", "", 1))  # the dev proxy strips /api; here we call the API directly
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    tampered = thumb.replace("/api", "", 1)[:-1] + ("0" if thumb[-1] != "0" else "1")
    assert (await c.get(tampered)).status_code == 403


async def test_new_product_is_available_for_review_assignment(seeded):
    """Register -> it appears in the catalog the review dropdown uses -> a box can be assigned to it."""
    from tests.helpers import get_scan, upload_scan
    c = seeded
    mgr, staff = await login(c, "manager"), await login(c, "staff")
    sid = await upload_scan(c, staff)  # scanned BEFORE the product exists, so the stub cannot have matched it
    await c.post("/catalog/products", headers=mgr, json={"sku": "SKU-200", "name": "Brand New"})
    v = await get_scan(c, staff, sid)
    box = next(d["id"] for d in v["detections"] if d["state"] != "SURE")
    r = await c.patch(f"/scans/{sid}/detections", headers=staff, json={"ids": [box], "action": "ASSIGN", "sku": "SKU-200"})
    assert r.status_code == 200
    row = next(x for x in r.json()["summary"] if x["sku"] == "SKU-200")
    assert row["reviewed"] == 1 and row["name"] == "Brand New"
    assert (await c.patch(f"/scans/{sid}/detections", headers=staff, json={"ids": [box], "action": "ASSIGN", "sku": "NOPE"})).status_code == 422


async def test_deactivated_product_leaves_catalog_but_old_scans_still_read(seeded):
    c = seeded
    mgr, staff = await login(c, "manager"), await login(c, "staff")
    assert (await c.delete("/catalog/products/SKU-010", headers=mgr)).status_code == 204
    skus = [p["sku"] for p in (await c.get("/catalog/products", headers=staff)).json()]
    assert "SKU-010" not in skus and len(skus) == 9
    assert "SKU-010" in [p["sku"] for p in (await c.get("/catalog/products?include_inactive=true", headers=mgr)).json()]


async def test_staff_requests_new_product_manager_decides(seeded):
    c = seeded
    staff, mgr = await login(c, "staff"), await login(c, "manager")
    assert (await c.post("/catalog/product-requests", headers=mgr, json={"name": "X"})).status_code == 403  # Staff-only action
    r = await c.post("/catalog/product-requests", headers=staff, json={"name": "Oat Drink", "note": "seen on aisle 2"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert (await c.post(f"/catalog/product-requests/{rid}/decision", headers=staff, json={"decision": "APPROVED"})).status_code == 403
    assert (await c.post(f"/catalog/product-requests/{rid}/decision", headers=mgr, json={"decision": "APPROVED"})).json()["status"] == "APPROVED"
    assert (await c.post(f"/catalog/product-requests/{rid}/decision", headers=mgr, json={"decision": "REJECTED"})).status_code == 409
    assert any(n["text"].startswith("Your request for Oat Drink") for n in (await c.get("/notifications", headers=staff)).json()["items"])


async def test_expected_stock_edit_and_csv_import(seeded):
    c = seeded
    mgr = await login(c, "manager")
    shelf = await first_shelf(c, mgr)
    r = await c.put("/wms/expected", headers=mgr, json={"shelf_id": shelf, "sku": "SKU-001", "expected_qty": 25})
    assert r.status_code == 200 and r.json()["expected_qty"] == 25
    assert (await c.put("/wms/expected", headers=mgr, json={"shelf_id": shelf, "sku": "SKU-001", "expected_qty": -1})).status_code == 422
    assert (await c.put("/wms/expected", headers=mgr, json={"shelf_id": shelf, "sku": "NOPE", "expected_qty": 1})).status_code == 404

    good = b"shelf,sku,expected_qty\nAisle 1 / Shelf A,SKU-002,40\nAisle 1 / Shelf A,sku-003,41\n"
    r = await c.post("/wms/expected/import", headers=mgr, files=[("file", ("stock.csv", good, "text/csv"))])
    assert r.status_code == 200 and r.json()["rows"] == 2
    rows = {(e["sku"]): e["expected_qty"] for e in (await c.get(f"/wms/expected?shelf_id={shelf}", headers=mgr)).json()}
    assert rows["SKU-002"] == 40 and rows["SKU-003"] == 41

    bad = b"shelf,sku,expected_qty\nAisle 1 / Shelf A,SKU-002,99\nNowhere,SKU-003,5\nAisle 1 / Shelf A,SKU-004,abc\n"
    r = await c.post("/wms/expected/import", headers=mgr, files=[("file", ("stock.csv", bad, "text/csv"))])
    assert r.status_code == 422 and "row 3" in r.text and "row 4" in r.text and "nothing was changed" in r.text
    rows = {(e["sku"]): e["expected_qty"] for e in (await c.get(f"/wms/expected?shelf_id={shelf}", headers=mgr)).json()}
    assert rows["SKU-002"] == 40  # all-or-nothing

    r = await c.post("/wms/expected/import", headers=mgr, files=[("file", ("stock.csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.status_code == 400 and "Missing column" in r.text


async def test_thresholds_tolerance_and_preview(seeded):
    from tests.helpers import upload_scan
    c = seeded
    owner, mgr, staff = await login(c, "owner"), await login(c, "manager"), await login(c, "staff")
    assert (await c.get("/settings", headers=staff)).json()["thresholds"] == {"sure": 0.85, "margin": 0.08, "check": 0.6}  # readable by all
    assert (await c.put("/settings/thresholds", headers=owner, json={"sure": 0.5, "margin": 0.08, "check": 0.6})).status_code == 422
    assert (await c.put("/settings/tolerance", headers=owner, json={"pct": 101, "units": 1})).status_code == 422
    assert (await c.put("/settings/thresholds", headers=mgr, json={"sure": 0.9, "margin": 0.1, "check": 0.5})).status_code == 403

    await upload_scan(c, staff)
    strict = {"thresholds": {"sure": 0.99, "margin": 0.2, "check": 0.9}}
    p = (await c.post("/settings/thresholds/preview", headers=owner, json=strict)).json()
    assert p["scans"] == 1 and p["boxes"] > 0
    assert sum(p["current"].values()) == sum(p["proposed"].values()) == p["boxes"]
    assert p["proposed"]["SURE"] <= p["current"]["SURE"]  # stricter thresholds can only flag more

    assert (await c.put("/settings/thresholds", headers=owner, json={"sure": 0.9, "margin": 0.1, "check": 0.5})).status_code == 200
    log = (await c.get("/audit-log?action=THRESHOLDS_CHANGED", headers=owner)).json()
    assert len(log) == 1 and '"sure": 0.85' in log[0]["before"] and '"sure": 0.9' in log[0]["after"]


async def test_notifications_are_private_and_markable(seeded):
    from tests.helpers import submitted_scan
    c = seeded
    staff, mgr = await login(c, "staff"), await login(c, "manager")
    await submitted_scan(c, staff)
    n = (await c.get("/notifications", headers=mgr)).json()
    assert n["unread"] >= 1 and any(i["kind"] == "SUBMITTED" for i in n["items"])
    assert not any(i["kind"] == "SUBMITTED" for i in (await c.get("/notifications", headers=staff)).json()["items"])
    ids = [i["id"] for i in n["items"]]
    assert (await c.post("/notifications/read", headers=staff, json={"ids": ids})).status_code == 204  # someone else's ids: no effect
    assert (await c.get("/notifications", headers=mgr)).json()["unread"] == n["unread"]
    assert (await c.post("/notifications/read", headers=mgr, json={"all": True})).status_code == 204
    assert (await c.get("/notifications", headers=mgr)).json()["unread"] == 0


async def test_audit_log_search_and_roles(seeded):
    c = seeded
    owner, auditor, staff = await login(c, "owner"), await login(c, "auditor"), await login(c, "staff")
    await c.post("/catalog/products", headers=owner, json={"sku": "SKU-300", "name": "Audit Me"})
    hits = (await c.get("/audit-log?q=sku-300", headers=auditor)).json()
    assert hits and hits[0]["action"] == "PRODUCT_CREATED" and hits[0]["actor"] == "Olivia Owner"
    assert (await c.get("/audit-log", headers=staff)).status_code == 403
    r = await c.get("/audit-log/export", headers=auditor)
    assert r.status_code == 200 and "PRODUCT_CREATED" in r.text
    assert (await c.get("/audit-log?date_from=nonsense", headers=owner)).status_code == 422
