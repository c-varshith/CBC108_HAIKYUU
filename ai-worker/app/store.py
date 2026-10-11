"""Reference-embedding store. New SKU = add vectors here; there is no retraining anywhere in the system."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Protocol

import numpy as np

_NS = uuid.UUID("6f1f0e0e-3b5e-4c2b-9d0a-ca11b0e00001")


@dataclass
class Hit:
    sku: str
    name: str
    score: float  # cosine similarity, 1.0 = identical direction


class VectorStore(Protocol):
    def upsert(self, sku: str, name: str, ref_id: str, vectors: np.ndarray) -> int: ...
    def delete_reference(self, sku: str, ref_id: str) -> None: ...
    def delete_sku(self, sku: str) -> None: ...
    def search(self, vectors: np.ndarray, k: int) -> list[list[Hit]]: ...
    def count(self) -> int: ...
    def skus(self) -> list[str]: ...
    def ping(self) -> bool: ...


def aggregate_by_sku(hits: list[Hit], top_n: int = 5) -> list[Hit]:
    """Collapse raw neighbours (several per SKU, one per reference photo/augmentation) to one score per SKU.
    Score = best neighbour, plus a small bonus when several references agree (cap 0.02), which separates a SKU
    that matches from every angle from a look-alike that matches one photo by chance. Best first, distinct SKUs
    only, so the backend's 'margin to the best different SKU' is always taken against a real competitor."""
    by: dict[str, list[float]] = {}
    names: dict[str, str] = {}
    for h in hits:
        by.setdefault(h.sku, []).append(h.score)
        names[h.sku] = h.name
    out = []
    for sku, sc in by.items():
        sc.sort(reverse=True)
        support = min(0.02, 0.01 * (len(sc) - 1)) if len(sc) > 1 and sc[1] >= sc[0] - 0.05 else 0.0
        out.append(Hit(sku, names[sku], float(min(1.0, sc[0] + support))))
    out.sort(key=lambda h: -h.score)
    return out[:top_n]


class InMemoryStore:
    """Exact cosine search with numpy. Used by tests, the evaluation scripts and QDRANT_URL=':memory:'-style demos."""

    def __init__(self, dim: int):
        self.dim = dim
        self._v = np.zeros((0, dim), dtype=np.float32)
        self._meta: list[tuple[str, str, str]] = []  # sku, name, ref_id
        self._lock = threading.Lock()

    def upsert(self, sku, name, ref_id, vectors):
        v = _l2(np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim))
        with self._lock:
            keep = [i for i, m in enumerate(self._meta) if not (m[0] == sku and m[2] == ref_id)]
            self._v, self._meta = self._v[keep], [self._meta[i] for i in keep]
            self._v = np.concatenate([self._v, v])
            self._meta += [(sku, name, ref_id)] * len(v)
        return len(v)

    def delete_sku(self, sku):
        with self._lock:
            keep = [i for i, m in enumerate(self._meta) if m[0] != sku]
            self._v, self._meta = self._v[keep], [self._meta[i] for i in keep]

    def delete_reference(self, sku, ref_id):
        with self._lock:
            keep = [i for i, m in enumerate(self._meta) if not (m[0] == sku and m[2] == ref_id)]
            self._v, self._meta = self._v[keep], [self._meta[i] for i in keep]

    def search(self, vectors, k):
        q = _l2(np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim))
        with self._lock:
            v, meta = self._v, list(self._meta)
        if len(v) == 0:
            return [[] for _ in range(len(q))]
        sims = q @ v.T
        kk = min(k, sims.shape[1])
        idx = np.argpartition(-sims, kk - 1, axis=1)[:, :kk]
        out = []
        for r in range(len(q)):
            order = idx[r][np.argsort(-sims[r, idx[r]])]
            out.append([Hit(meta[i][0], meta[i][1], float(sims[r, i])) for i in order])
        return out

    def count(self):
        return len(self._meta)

    def skus(self):
        return sorted({m[0] for m in self._meta})

    def ping(self):
        return True


class QdrantStore:
    """Qdrant (cosine). Payload: sku, name, ref_image_id. Point ids are deterministic (uuid5 of sku/ref/index), so
    re-uploading the same reference photo overwrites instead of duplicating."""

    def __init__(self, url: str, collection: str, dim: int, api_key: str | None = None):
        from qdrant_client import QdrantClient, models

        self.m = models
        self.client = QdrantClient(location=":memory:") if url == ":memory:" else QdrantClient(url=url, api_key=api_key, timeout=30)
        self.collection, self.dim = collection, dim
        existing = {c.name for c in self.client.get_collections().collections}
        if collection not in existing:
            self.client.create_collection(collection, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
            self.client.create_payload_index(collection, "sku", models.PayloadSchemaType.KEYWORD)
        else:
            size = self.client.get_collection(collection).config.params.vectors.size
            if size != dim:
                raise RuntimeError(f"Qdrant collection '{collection}' has dim {size} but the embedder produces {dim}. "
                                   "Use a new QDRANT_COLLECTION (or delete it and re-register references) when changing DINO_MODEL.")

    def upsert(self, sku, name, ref_id, vectors):
        v = _l2(np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim))
        self._delete_ref(sku, ref_id)
        pts = [self.m.PointStruct(id=str(uuid.uuid5(_NS, f"{sku}/{ref_id}/{i}")), vector=row.tolist(),
                                  payload={"sku": sku, "name": name, "ref_image_id": ref_id}) for i, row in enumerate(v)]
        self.client.upsert(self.collection, points=pts, wait=True)
        return len(pts)

    def _filter(self, **kv):
        return self.m.Filter(must=[self.m.FieldCondition(key=k, match=self.m.MatchValue(value=v)) for k, v in kv.items()])

    def delete_reference(self, sku, ref_id):
        self.client.delete(self.collection, points_selector=self.m.FilterSelector(filter=self._filter(sku=sku, ref_image_id=ref_id)), wait=True)

    def _delete_ref(self, sku, ref_id):
        self.delete_reference(sku, ref_id)

    def delete_sku(self, sku):
        self.client.delete(self.collection, points_selector=self.m.FilterSelector(filter=self._filter(sku=sku)), wait=True)

    def search(self, vectors, k):
        q = _l2(np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim))
        if len(q) == 0:
            return []
        reqs = [self.m.QueryRequest(query=row.tolist(), limit=k, with_payload=True) for row in q]
        res = self.client.query_batch_points(self.collection, requests=reqs)
        return [[Hit(p.payload["sku"], p.payload.get("name", ""), float(p.score)) for p in r.points] for r in res]

    def count(self):
        return int(self.client.count(self.collection, exact=True).count)

    def skus(self):
        seen: set[str] = set()
        offset = None
        while True:
            pts, offset = self.client.scroll(self.collection, limit=512, offset=offset, with_payload=["sku"], with_vectors=False)
            seen.update(p.payload["sku"] for p in pts)
            if offset is None:
                return sorted(seen)

    def ping(self):
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False


def _l2(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, 1e-12)
