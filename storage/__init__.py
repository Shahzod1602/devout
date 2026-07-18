"""Blob storage abstraktsiyasi — Postgres migratsiyasi 0-bosqich poydevori.

docs/POSTGRES_MIGRATION_PLAN.md. Hozircha faqat interfeys + S3/R2 implementatsiyasi
va factory. operations.py bunga HALI ULANMAGAN — `BLOB_STORAGE_BACKEND` default 'db',
ya'ni eski inline-blob xulqi o'zgarmaydi. Phase 1'da add_bol/add_pod/get_last_bol
shu qatlamdan o'tkaziladi.
"""
from storage.blobstore import BlobStore, S3BlobStore, blob_key, get_blob_store

__all__ = ["BlobStore", "S3BlobStore", "blob_key", "get_blob_store"]
