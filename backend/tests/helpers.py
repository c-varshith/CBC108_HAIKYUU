from __future__ import annotations

import io
import random

from PIL import Image

from scripts.seed import DEMO_PASSWORD

EMAILS = {"owner": "owner@calibrex.demo", "manager": "manager@calibrex.demo", "staff": "staff@calibrex.demo", "auditor": "auditor@calibrex.demo"}


def make_jpeg(seed: int = 1, size=(800, 600)) -> bytes:
    r = random.Random(seed)
    im = Image.new("RGB", size, (r.randrange(256), r.randrange(256), r.randrange(256)))
    for _ in range(40):
        x, y = r.randrange(size[0] - 50), r.randrange(size[1] - 50)
        im.paste((r.randrange(256), r.randrange(256), r.randrange(256)), (x, y, x + 40, y + 40))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


async def login(client, who: str, password: str = DEMO_PASSWORD) -> dict:
    r = await client.post("/auth/login", json={"email": EMAILS.get(who, who), "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def first_shelf(client, h) -> str:
    return (await client.get("/shelves", headers=h)).json()[0]["id"]


async def upload_scan(client, h, seed: int = 1) -> str:
    shelf = await first_shelf(client, h)
    r = await client.post("/scans", headers=h, data={"shelf_id": shelf}, files=[("files", ("shelf.jpg", make_jpeg(seed), "image/jpeg"))])
    assert r.status_code == 201, r.text
    await client.app.state.runner.wait_idle()
    return r.json()["scan_id"]


async def get_scan(client, h, sid) -> dict:
    r = await client.get(f"/scans/{sid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def resolve_all(client, h, sid) -> dict:
    """Accept every doubtful box as 'Unknown' so the scan becomes submittable."""
    v = await get_scan(client, h, sid)
    ids = [d["id"] for d in v["detections"] if d["state"] in ("CHECK", "UNKNOWN") and not d["accepted_unknown"] and not d["deleted"]]
    if ids:
        r = await client.patch(f"/scans/{sid}/detections", headers=h, json={"ids": ids, "action": "UNKNOWN"})
        assert r.status_code == 200, r.text
        v = r.json()
    assert v["totals"]["unresolved"] == 0
    return v


async def submitted_scan(client, h, seed: int = 1) -> str:
    sid = await upload_scan(client, h, seed)
    await resolve_all(client, h, sid)
    r = await client.post(f"/scans/{sid}/submit", headers=h, json={"note": "done"})
    assert r.status_code == 200, r.text
    return sid
