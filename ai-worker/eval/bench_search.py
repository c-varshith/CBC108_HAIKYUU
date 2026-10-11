"""Vector-search speed as the catalog grows (spec 5.6). Synthetic unit vectors, 768-d, 3 reference photos x 2 variants per SKU.

  python -m eval.bench_search                       # numpy exact search (baseline) and Qdrant in-memory
  python -m eval.bench_search --qdrant http://localhost:6333     # a real server"""
from __future__ import annotations

import argparse
import time

import numpy as np

from app.store import InMemoryStore, QdrantStore


def bench(store, n_skus: int, dim: int, queries: np.ndarray, rng) -> float:
    for i in range(n_skus):
        store.upsert(f"SKU-{i:05d}", f"p{i}", "r", rng.normal(size=(6, dim)).astype(np.float32))
    store.search(queries[:4], 12)  # warm-up
    t0 = time.perf_counter()
    store.search(queries, 12)
    return (time.perf_counter() - t0) * 1000 / len(queries)


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--dim", type=int, default=768); ap.add_argument("--qdrant", default=":memory:")
    ap.add_argument("--queries", type=int, default=200)
    a = ap.parse_args()
    rng = np.random.default_rng(0)
    q = rng.normal(size=(a.queries, a.dim)).astype(np.float32)
    print(f"{'SKUs':>8} {'vectors':>9} {'numpy ms/crop':>14} {'qdrant ms/crop':>15}")
    for n in (100, 1_000, 10_000):
        np_ms = bench(InMemoryStore(a.dim), n, a.dim, q, rng)
        try:
            qd = QdrantStore(a.qdrant, f"bench_{n}_{int(time.time())}", a.dim)
            qd_ms = f"{bench(qd, n, a.dim, q, rng):15.2f}"
        except Exception as e:  # qdrant-client not installed / server down
            qd_ms = f"{'n/a':>15}"; print("  (qdrant skipped:", type(e).__name__, ")")
        print(f"{n:>8} {n * 6:>9} {np_ms:>14.2f} {qd_ms}")


if __name__ == "__main__":
    main()
