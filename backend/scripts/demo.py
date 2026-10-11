"""Four-role walkthrough against a RUNNING API (seeded demo users).

    python -m scripts.demo --base-url http://localhost:8000 [--image shelf.jpg]

Staff captures and reviews -> Staff submits -> Manager approves -> Auditor reads and exports (and is refused an edit)
-> Owner reads the audit trail. Prints each step so it doubles as a live smoke test."""
from __future__ import annotations

import argparse
import io
import random
import sys
import time

import httpx
from PIL import Image

PASSWORD = "Calibrex-demo1"


def demo_image() -> bytes:
    r = random.Random(42)
    im = Image.new("RGB", (1600, 1200), (60, 70, 90))
    for _ in range(80):
        x, y = r.randrange(1500), r.randrange(1100)
        im.paste((r.randrange(256), r.randrange(256), r.randrange(256)), (x, y, x + 60, y + 90))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--image")
    args = ap.parse_args()
    photo = open(args.image, "rb").read() if args.image else demo_image()
    http = httpx.Client(base_url=args.base_url, timeout=60)

    def login(role: str) -> dict:
        r = http.post("/auth/login", json={"email": f"{role}@calibrex.demo", "password": PASSWORD})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def check(r: httpx.Response, want: int = 200) -> httpx.Response:
        if r.status_code != want:
            print(f"   UNEXPECTED {r.status_code}: {r.text[:200]}")
            sys.exit(1)
        return r

    step(1, "STAFF signs in and uploads a shelf photo")
    staff = login("staff")
    shelf = check(http.get("/shelves", headers=staff)).json()[0]
    r = check(http.post("/scans", headers=staff, data={"shelf_id": shelf["id"], "device": "demo script"},
                        files=[("files", ("shelf.jpg", photo, "image/jpeg"))]), 201)
    sid = r.json()["scan_id"]
    print(f"   scan {sid[:8]} on '{shelf['name']}'")

    step(2, "Processing (detect -> match -> count)")
    for _ in range(120):
        v = check(http.get(f"/scans/{sid}", headers=staff)).json()
        if v["status"] not in ("UPLOADED", "PROCESSING"):
            break
        time.sleep(1)
    t = v["totals"]
    print(f"   {v['status']}: {t['detected']} boxes, {t['sure']} sure, {t['check']} check, {t['unknown']} unknown")
    if v["status"] == "FAILED":
        return 1

    step(3, "STAFF reviews every doubtful box (confirm the top suggestion, else accept as unknown)")
    todo = [d for d in v["detections"] if d["state"] in ("CHECK", "UNKNOWN") and not d["accepted_unknown"] and not d["deleted"]]
    confirm = [d["id"] for d in todo if d["top_matches"] and d["state"] == "CHECK"]
    unknown = [d["id"] for d in todo if d["id"] not in confirm]
    for ids, action in ((confirm, "CONFIRM"), (unknown, "UNKNOWN")):
        if ids:
            v = check(http.patch(f"/scans/{sid}/detections", headers=staff, json={"ids": ids, "action": action})).json()
    print(f"   unresolved now: {v['totals']['unresolved']}")

    step(4, "STAFF submits for approval")
    check(http.post(f"/scans/{sid}/submit", headers=staff, json={"note": "Counted by demo script"}))
    check(http.post(f"/scans/{sid}/approve", headers=staff), 403)
    print("   submitted; staff cannot approve (403, as designed)")

    step(5, "MANAGER approves")
    mgr = login("manager")
    inbox = check(http.get("/approvals", headers=mgr)).json()
    print(f"   inbox has {len(inbox)} scan(s)")
    v = check(http.post(f"/scans/{sid}/approve", headers=mgr)).json()
    print(f"   {v['status']} by {v['approval']['decided_by']['name']}; reconciliation (reviewed - expected):")
    for row in v["summary"][:6]:
        print(f"     {row['sku']:8} {row['name']:22} reviewed {row['reviewed']:3} expected {row['expected']:3} variance {row['variance']:+d} {row['status']}")
    check(http.patch(f"/scans/{sid}/detections", headers=mgr, json={"ids": [v['detections'][0]['id']], "action": "UNKNOWN"}), 409)
    print("   approved scan is locked (409 on edit)")

    step(6, "AUDITOR reads and exports; edits are refused")
    aud = login("auditor")
    check(http.get(f"/scans/{sid}", headers=aud))
    csv_text = check(http.get(f"/scans/{sid}/export?format=csv", headers=aud)).text
    print(f"   exported CSV, {len(csv_text.splitlines())} lines")
    check(http.post(f"/scans/{sid}/submit", headers=aud, json={}), 403)
    print("   auditor edit refused (403)")

    step(7, "OWNER reads the audit trail for this scan")
    owner = login("owner")
    for a in reversed(check(http.get(f"/audit-log?scan_id={sid}&limit=50", headers=owner)).json()):
        print(f"   {a['at'][11:19]}  {a['actor']:22} {a['action']}")
    print("\nDemo complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
