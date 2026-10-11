"""Idempotent demo seed: one organisation, one user per role, stores/shelves, a catalog and expected stock.

    python -m scripts.seed            # uses DATABASE_URL from .env (creates tables if AUTO_CREATE_SCHEMA=true)

Demo password for every account: see DEMO_PASSWORD. Never run against a production database."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app import db
from app.config import Settings, get_settings
from app.domain.common import get_org_settings
from app.models import Base, ExpectedStock, Organization, Product, Shelf, Store, User
from app.security import hash_password

DEMO_PASSWORD = "Calibrex-demo1"
USERS = [("owner@calibrex.demo", "Olivia Owner", "OWNER"), ("manager@calibrex.demo", "Marcus Manager", "MANAGER"),
         ("staff@calibrex.demo", "Sam Staff", "STAFF"), ("auditor@calibrex.demo", "Ada Auditor", "AUDITOR")]
STORE = "Main Warehouse"
SHELVES = ["Aisle 1 / Shelf A", "Aisle 1 / Shelf B", "Aisle 2 / Shelf A"]
PRODUCTS = [("SKU-001", "Canned Goods", 12), ("SKU-002", "Olive Oil Bottle", 6), ("SKU-003", "Almond Milk Carton", 10),
            ("SKU-004", "Cracker Box", 8), ("SKU-005", "Honey Jar", 6), ("SKU-006", "Sparkling Water Can", 24),
            ("SKU-007", "Cereal Box", 8), ("SKU-008", "Pasta Pack", 10), ("SKU-009", "Coffee Bag", 5), ("SKU-010", "Pet Food Pouch", 6)]
# expected quantity per shelf for the first shelf; the others get a smaller slice so reconciliation has variety
EXPECTED = {"SKU-001": 18, "SKU-002": 9, "SKU-003": 14, "SKU-004": 12, "SKU-005": 8, "SKU-006": 30, "SKU-007": 11, "SKU-008": 15, "SKU-009": 7, "SKU-010": 9}


async def seed(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    own_engine = False
    try:
        db.sessionmaker()
    except AssertionError:
        engine = db.init_engine(s)
        own_engine = True
        if s.auto_create_schema:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
    pw_hash = await asyncio.to_thread(hash_password, DEMO_PASSWORD)
    async with db.sessionmaker()() as session:
        org = await session.scalar(select(Organization).where(Organization.slug == "default"))
        if org is None:
            org = Organization(slug="default", name="Demo Warehouse Co.")
            session.add(org)
            await session.flush()
        await get_org_settings(session, org.id)
        for email, name, role in USERS:
            if await session.scalar(select(User).where(User.email == email)) is None:
                session.add(User(org_id=org.id, email=email, name=name, role=role, password_hash=pw_hash))
        store = await session.scalar(select(Store).where(Store.org_id == org.id, Store.name_key == STORE.lower()))
        if store is None:
            store = Store(org_id=org.id, name=STORE, name_key=STORE.lower())
            session.add(store)
            await session.flush()
        shelves = []
        for n in SHELVES:
            sh = await session.scalar(select(Shelf).where(Shelf.store_id == store.id, Shelf.name_key == n.lower()))
            if sh is None:
                sh = Shelf(org_id=org.id, store_id=store.id, name=n, name_key=n.lower())
                session.add(sh)
                await session.flush()
            shelves.append(sh)
        for sku, name, min_qty in PRODUCTS:
            if await session.scalar(select(Product).where(Product.org_id == org.id, Product.sku_key == sku.lower())) is None:
                session.add(Product(org_id=org.id, sku=sku, sku_key=sku.lower(), name=name, min_qty=min_qty))
        await session.flush()
        for i, sh in enumerate(shelves):
            have = {e.sku for e in await session.scalars(select(ExpectedStock).where(ExpectedStock.shelf_id == sh.id))}
            for sku, qty in EXPECTED.items():
                if sku not in have:
                    session.add(ExpectedStock(org_id=org.id, shelf_id=sh.id, sku=sku, expected_qty=max(qty - 4 * i, 2)))
        await session.commit()
    if own_engine:
        await db.dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
    print(f"Seeded. Sign in with owner@/manager@/staff@/auditor@calibrex.demo and password {DEMO_PASSWORD}")
