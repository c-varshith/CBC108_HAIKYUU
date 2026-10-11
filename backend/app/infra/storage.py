"""Object storage behind one small interface: local disk (default) or S3-compatible (MinIO)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.security import media_expiry, media_signature

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_\-.]{0,298}$")


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key)) and ".." not in key and "//" not in key


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> None: ...
    def url(self, key: str, *, ttl: int = 3600, bucket: int = 900) -> str: ...
    async def ping(self) -> bool: ...


class LocalStorage:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = Path(settings.storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        if not valid_key(key):
            raise ValueError("invalid storage key")
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("path escapes storage root")
        return p

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self.path(key)

        def _w() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)  # atomic: readers never see half a file

        await asyncio.to_thread(_w)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self.path(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self.path(key).unlink(missing_ok=True))

    async def delete_prefix(self, prefix: str) -> None:
        import shutil
        d = self.path(prefix.rstrip("/"))
        await asyncio.to_thread(lambda: shutil.rmtree(d, ignore_errors=True))

    def url(self, key: str, *, ttl: int = 3600, bucket: int = 900) -> str:
        exp = media_expiry(ttl, bucket)
        return f"{self.s.media_url_prefix}/{key}?exp={exp}&sig={media_signature(self.s, key, exp)}"

    async def ping(self) -> bool:
        return self.root.is_dir()


class S3Storage:
    """boto3 is synchronous, so every call runs in a worker thread. Not exercised by the test-suite (needs MinIO)."""

    def __init__(self, settings: Settings):
        import boto3
        self.s = settings
        self.bucket = settings.s3_bucket
        self.c = boto3.client("s3", endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region,
                              aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self.c.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def get(self, key: str) -> bytes:
        r = await asyncio.to_thread(self.c.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(r["Body"].read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.c.delete_object, Bucket=self.bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> None:
        def _d() -> None:
            for page in self.c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    self.c.delete_objects(Bucket=self.bucket, Delete={"Objects": objs})
        await asyncio.to_thread(_d)

    def url(self, key: str, *, ttl: int = 3600, bucket: int = 900) -> str:
        # presigned URLs embed their own expiry/signature; quantise "now" so repeated calls give the same URL
        return self.c.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl + bucket)

    async def ping(self) -> bool:
        try:
            await asyncio.to_thread(self.c.head_bucket, Bucket=self.bucket)
            return True
        except Exception:
            return False


def make_storage(settings: Settings) -> Storage:
    return S3Storage(settings) if settings.storage_backend == "s3" else LocalStorage(settings)
