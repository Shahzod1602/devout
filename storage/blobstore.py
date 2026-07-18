"""BOL/POD hujjat-baytlari uchun object-storage abstraktsiyasi.

Interfeys + S3-mos (Cloudflare R2 / Hetzner Object Storage / MinIO / AWS S3)
implementatsiya. boto3 (sync) client `run_in_executor`da chaqiriladi — blob
put/get juda kam (hujjatiga bir marta), shuning uchun executor yetarli va
aioboto3 kabi og'ir async-stack shart emas (mavjud Gemini `run_in_executor`
naqshi bilan bir xil). boto3 LAZY import qilinadi — 's3' backend yoqilmasa
umuman kerak emas.
"""
import asyncio
import functools
import logging
from typing import Protocol, runtime_checkable

import config

logger = logging.getLogger(__name__)


def blob_key(kind: str, group_id, load_id, message_id) -> str:
    """Determinstik object kalit: 'bols/<group_id>/<load_id>/<message_id>.pdf'.

    kind: 'bols' yoki 'pods'. Kalit idempotent — bir xil hujjat qayta yozilsa
    o'sha object ustiga tushadi (INSERT OR IGNORE / ON CONFLICT bilan mos).
    """
    return f"{kind}/{group_id}/{load_id}/{message_id}.pdf"


@runtime_checkable
class BlobStore(Protocol):
    """Hujjat-baytlarini kalit bo'yicha saqlash interfeysi."""

    async def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...


def _not_found(exc: Exception) -> bool:
    """S3 'yo'q' xatosini (404 / NoSuchKey / NotFound) botocore'siz aniqlash."""
    text = str(exc).lower()
    return "nosuchkey" in text or "not found" in text or "404" in text


class S3BlobStore:
    """S3-mos object storage implementatsiyasi.

    `client` inject qilinishi mumkin (test uchun MagicMock) — bo'lmasa boto3 lazy
    import qilinib yaratiladi. `prefix` bilan muhitlar (bot/botprod/devbot) bitta
    bucket'da ajraladi.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "auto",
        client=None,
    ):
        if not bucket:
            raise ValueError("S3BlobStore: bucket bo'sh bo'lishi mumkin emas")
        if client is None:
            import boto3  # lazy — faqat 's3' backend yoqilganda kerak
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                region_name=region or None,
            )
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def _full(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    async def _run(self, fn, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(fn, **kwargs))

    async def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None:
        await self._run(
            self._client.put_object,
            Bucket=self._bucket, Key=self._full(key), Body=data, ContentType=content_type,
        )

    async def get(self, key: str) -> bytes:
        resp = await self._run(self._client.get_object, Bucket=self._bucket, Key=self._full(key))
        return resp["Body"].read()

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=self._full(key))

    async def exists(self, key: str) -> bool:
        try:
            await self._run(self._client.head_object, Bucket=self._bucket, Key=self._full(key))
            return True
        except Exception as exc:
            if _not_found(exc):
                return False
            raise


# Modul-darajali singleton (config o'zgarmaydi, client thread-safe).
_store: BlobStore | None = None


def get_blob_store() -> BlobStore | None:
    """Config bo'yicha BlobStore qaytaradi, yoki None.

    None qaytsa (BLOB_STORAGE_BACKEND != 's3') — chaqiruvchi eski inline-blob
    yo'lini ishlatadi (Phase 0 xulqi). Phase 1'da operations.py None bo'lganda
    inline, aks holda shu store'ni ishlatadi.
    """
    global _store
    if config.BLOB_STORAGE_BACKEND != "s3":
        return None
    if _store is None:
        _store = S3BlobStore(
            bucket=config.BLOB_S3_BUCKET,
            prefix=config.BLOB_S3_PREFIX,
            endpoint_url=config.BLOB_S3_ENDPOINT,
            access_key=config.BLOB_S3_ACCESS_KEY,
            secret_key=config.BLOB_S3_SECRET_KEY,
            region=config.BLOB_S3_REGION,
        )
        logger.info("🗄️ Blob storage: S3 backend (bucket=%s, prefix=%s)",
                    config.BLOB_S3_BUCKET, config.BLOB_S3_PREFIX)
    return _store


def reset_blob_store() -> None:
    """Test/rekonfiguratsiya uchun singleton'ni tozalash."""
    global _store
    _store = None
