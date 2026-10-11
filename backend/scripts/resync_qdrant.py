"""Re-send every active product's stored reference photos to the AI worker (rebuilds Qdrant).

    python -m scripts.resync_qdrant                 # all active products of the default organization
    python -m scripts.resync_qdrant --org acme --sku 5000112637922 --sku SKU-001

Use it when Qdrant was wiped or restored from an old snapshot, or after changing DINO_MODEL / QDRANT_COLLECTION on the
worker (a new model needs a new collection; vectors of different models are not comparable). Photos come from storage
(the 512 px copies that were originally sent), so nothing is re-downloaded. Safe to re-run: the worker upserts by photo id.
The same job is available to Owners as POST /catalog/resync."""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app import db as dbmod
from app.config import get_settings
from app.domain import catalog as cat
from app.domain.common import Actor
from app.infra.inference import make_inference
from app.infra.storage import make_storage
from app.models import Organization


async def amain(argv: list[str] | None = None) -> int:
    a = argparse.ArgumentParser(prog="resync_qdrant", description=__doc__.split("\n\n")[0])
    a.add_argument("--org", default="default", help="organization slug (default: default)")
    a.add_argument("--sku", action="append", help="only this SKU (repeatable)")
    args = a.parse_args(argv)
    s = get_settings()
    dbmod.init_engine(s)
    try:
        inference = make_inference(s)
        if not await inference.ping():
            print("The AI worker is not reachable (check AI_WORKER_URL / AI_WORKER_TOKEN).", file=sys.stderr)
            return 2
        sm = dbmod.sessionmaker()
        async with sm() as db:
            org = await db.scalar(select(Organization).where(Organization.slug == args.org))
            if org is None:
                print(f"Organization '{args.org}' not found.", file=sys.stderr)
                return 2
            batch = await cat.start_batch(db, org.id, "resync", {"cli": True, "skus": args.sku})
        done_msg = lambda d, t: print(f"\r{d}/{t} products", end="", flush=True) if d % 25 == 0 or d == t else None  # noqa: E731
        counts = await cat.resync_org(sm, make_storage(s), inference, s, org.id, batch.id, Actor(None, "Catalog resync", None, org.id),
                                      skus=args.sku, progress=done_msg)
        print()
        for w in counts.get("warnings", []):
            print("WARNING:", w, file=sys.stderr)
        print(f"model/collection: {counts.get('signature')}")
        print(f"batch {batch.id}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if isinstance(v, int)))
        return 1 if counts.get("failed") else 0
    finally:
        await dbmod.dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
