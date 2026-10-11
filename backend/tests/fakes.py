"""Test doubles: a fake AI worker (colour-based embedder + in-memory cosine store, no torch/Qdrant/GPU) and a fake Open Food Facts.

FakeWorker follows the same InferenceClient contract as the real worker (backend/app/infra/inference.py) and enforces the worker's
limit of 12 photos per call, so batching bugs fail loudly."""
from __future__ import annotations

import io
import math
import random

import httpx
from PIL import Image

from app.infra.inference import InferenceError, RawDetection, RawMatch

BG = (30, 30, 30)


def photo(color: tuple[int, int, int], seed: int = 0, size: tuple[int, int] = (400, 400), quality: int = 92) -> bytes:
    """A textured product photo whose mean colour is ~`color`. Different seeds give different perceptual hashes."""
    r = random.Random(seed * 7919 + color[0] * 31 + color[1] * 17 + color[2])
    im = Image.new("RGB", size, color)
    for _ in range(80):
        x, y = r.randrange(size[0] - 30), r.randrange(size[1] - 30)
        d = r.randrange(-18, 19)
        im.paste(tuple(max(0, min(255, c + d)) for c in color), (x, y, x + r.randrange(10, 60), y + r.randrange(10, 60)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def shelf_photo(colors: list[tuple[int, int, int]], size=(800, 600), w=100, gap=30) -> bytes:
    """Dark shelf with one flat coloured box per colour, left to right."""
    im = Image.new("RGB", size, BG)
    for i, c in enumerate(colors):
        x = gap + i * (w + gap)
        im.paste(c, (x, 150, x + w, 450))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _embed(im: Image.Image) -> list[float]:
    a = im.convert("RGB").resize((8, 8))
    px = list(a.getdata())
    m = [sum(p[i] for p in px) / len(px) / 255.0 for i in range(3)]
    v = m + [x * x for x in m] + [math.sin(x * 5) for x in m] + [math.cos(x * 5) for x in m]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class FakeWorker:
    model, collection = "fake-dino", "fake_refs"

    def __init__(self):
        self.store: dict[tuple[str, str], tuple[str, list[float]]] = {}  # (sku, ref id) -> (name, vector)
        self.calls: list[tuple[str, int]] = []  # (sku, photos in the call)
        self.fail = 0  # the next N add_references calls fail
        self.down = False

    # --- InferenceClient ---
    async def add_references(self, sku, name, items):
        if self.down:
            raise InferenceError("AI service unreachable")
        if not 1 <= len(items) <= 12:
            raise InferenceError("AI service returned 422")
        self.calls.append((sku, len(items)))
        if self.fail > 0:
            self.fail -= 1
            raise InferenceError("AI service returned 500")
        for rid, data in items:
            self.store[(sku, rid)] = (name, _embed(Image.open(io.BytesIO(data))))
        return len(items)

    async def remove_product(self, sku):
        for k in [k for k in self.store if k[0] == sku]:
            del self.store[k]

    async def remove_reference(self, sku, ref_id):
        if self.down:
            raise InferenceError("AI service unreachable")
        self.store.pop((sku, ref_id), None)

    async def ping(self):
        return not self.down

    async def info(self):
        if self.down:
            raise InferenceError("AI service unreachable")
        return {"model": self.model, "collection": self.collection, "dim": 12, "references": len(self.store)}

    async def detect(self, image, *, scan_id, catalog, width, height, seed=None, dense=False):
        im = Image.open(io.BytesIO(image)).convert("RGB")
        sx, sy = 10, 10
        small = im.resize((im.width // sx, im.height // sy), Image.Resampling.NEAREST)
        w, h = small.size
        px = small.load()
        fg = [any(sum(abs(a - b) for a, b in zip(px[x, y], BG)) > 40 for y in range(h)) for x in range(w)]
        out, x = [], 0
        while x < w:
            if not fg[x]:
                x += 1
                continue
            x0 = x
            while x < w and fg[x]:
                x += 1
            ys = [y for y in range(h) if any(sum(abs(a - b) for a, b in zip(px[xx, y], BG)) > 40 for xx in range(x0, x))]
            box = (x0 * sx, min(ys) * sy, x * sx, (max(ys) + 1) * sy)
            vec = _embed(im.crop(box))
            best: dict[str, float] = {}
            for (sku, _), (_, v) in self.store.items():
                best[sku] = max(best.get(sku, -1.0), _cos(vec, v))
            matches = [RawMatch(s, round(max(0.0, min(1.0, sc)), 4)) for s, sc in sorted(best.items(), key=lambda kv: -kv[1])[:5]]
            out.append(RawDetection(tuple(float(v) for v in box), 0.9, matches))
        return out

    # --- helpers for assertions ---
    def skus(self) -> set[str]:
        return {k[0] for k in self.store}


# ---------------------------------------------------------------- fake Open Food Facts
class FakeOff:
    """Serves /api/v2/product/<code>.json, /api/v2/search (code=, categories_tags_en=, countries_tags_en=) and image URLs.
    `flaky` = {path-substring: [status, status, ...]} makes the first requests to a matching URL fail with those codes."""

    def __init__(self):
        self.products: dict[str, dict] = {}
        self.images: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.flaky: dict[str, list[int]] = {}

    def add(self, code: str, name: str, image: bytes | None, *, brand="Acme", quantity="500 g", categories=("Snacks",), countries=("France",),
            image_url: str | None = None) -> None:
        url = image_url or f"https://images.openfoodfacts.org/images/products/{code}/front_en.4.400.jpg"
        p: dict = {"code": code, "product_name": name, "brands": f"{brand}, Other", "quantity": quantity,
                   "_categories": list(categories), "_countries": list(countries)}
        if image is not None:
            self.images[url] = image
            p["selected_images"] = {"front": {"display": {"en": url}, "small": {"en": url}}}
            p["images"] = {"front_en": {"imgid": "1", "rev": "4"}, "1": {"uploader": "alice"}}
        self.products[code] = p

    def _public(self, p: dict) -> dict:
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = str(request.url)
        for frag, codes in self.flaky.items():
            if frag in key and codes:
                st = codes.pop(0)
                return httpx.Response(st, headers={"Retry-After": "1"} if st == 429 else {}, text="slow down")
        path = request.url.path
        q = request.url.params
        if request.url.host == "images.openfoodfacts.org":
            data = self.images.get(key)
            return httpx.Response(200, content=data) if data else httpx.Response(404)
        if path.startswith("/api/v2/product/"):
            code = path.rsplit("/", 1)[1].removesuffix(".json")
            p = self.products.get(code)
            return httpx.Response(200, json={"code": code, "status": 1, "product": self._public(p)} if p else {"code": code, "status": 0})
        if path == "/api/v2/search":
            if q.get("code"):
                found = [self._public(self.products[c]) for c in q["code"].split(",") if c in self.products]
                return httpx.Response(200, json={"count": len(found), "page": 1, "page_size": len(found), "products": found})
            rows = [p for p in self.products.values()
                    if (not q.get("categories_tags_en") or q["categories_tags_en"] in p["_categories"])
                    and (not q.get("countries_tags_en") or q["countries_tags_en"] in p["_countries"])]
            size, page = int(q.get("page_size", 24)), int(q.get("page", 1))
            return httpx.Response(200, json={"page": page, "page_size": size, "products": [self._public(p) for p in rows[(page - 1) * size:page * size]]})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), headers={"User-Agent": "CALIBREX-test/1.0 (test@example.com)"})
