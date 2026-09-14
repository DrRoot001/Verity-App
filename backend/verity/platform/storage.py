"""Object storage abstraction (PRD §45, §38 provider abstraction).

Uploaded documents, exports and transient media never touch the database.
Storage keys are opaque UUID paths — a user-supplied filename is metadata, never
part of a path, so a crafted name cannot traverse out of its prefix (§28.3).
"""

from __future__ import annotations

import pathlib
import shutil
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger

log = get_logger("platform.storage")


class StorageBucket(StrEnum):
    """Separate prefixes so retention policy can differ per class (PRD §24.8)."""

    RESUMES = "resumes"
    JOB_DESCRIPTIONS = "job-descriptions"
    EXPORTS = "exports"
    SCREEN_CONTEXT = "screen-context"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    content_type: str


@runtime_checkable
class ObjectStorage(Protocol):
    name: str

    async def put(
        self, bucket: StorageBucket, data: bytes, *, content_type: str, owner_id: uuid.UUID
    ) -> StoredObject: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    #: Deletion is only complete when a listing comes back empty
    #: (PRD AC-PRIV-001), so verification is part of the interface.
    async def list_prefix(self, prefix: str) -> list[str]: ...

    async def signed_url(self, key: str, *, expires_in: timedelta) -> str: ...


def build_key(bucket: StorageBucket, owner_id: uuid.UUID, *, extension: str = "") -> str:
    """Owner-prefixed, opaque key.

    The owner prefix is what makes ``delete_prefix`` a complete, verifiable
    deletion for one user (PRD §29.4).
    """
    suffix = f".{extension.lstrip('.')}" if extension else ""
    return f"{bucket}/{owner_id}/{uuid.uuid4()}{suffix}"


class LocalObjectStorage:
    """Filesystem-backed storage for local development and tests."""

    name = "local"

    def __init__(self, root: pathlib.Path | None = None) -> None:
        self._root = (root or pathlib.Path(settings.storage_local_path)).resolve()

    def _resolve(self, key: str) -> pathlib.Path:
        path = (self._root / key).resolve()
        # Defence in depth: keys are generated, but never trust a path that
        # escapes the root regardless of how it was constructed.
        if not path.is_relative_to(self._root):
            raise AppError.internal("Invalid storage key.")
        return path

    async def put(
        self, bucket: StorageBucket, data: bytes, *, content_type: str, owner_id: uuid.UUID
    ) -> StoredObject:
        key = build_key(bucket, owner_id)
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(key=key, size_bytes=len(data), content_type=content_type)

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise AppError.not_found("File")
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        path.unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> int:
        root = self._resolve(prefix)
        if not root.exists():
            return 0
        count = sum(1 for p in root.rglob("*") if p.is_file())
        shutil.rmtree(root, ignore_errors=True)
        return count

    async def list_prefix(self, prefix: str) -> list[str]:
        root = self._resolve(prefix)
        if not root.exists():
            return []
        return [str(p.relative_to(self._root)) for p in root.rglob("*") if p.is_file()]

    async def signed_url(self, key: str, *, expires_in: timedelta) -> str:
        # Local development serves through the API rather than a CDN, so the
        # download endpoint performs the authorization check itself.
        return f"{settings.public_api_url}/v1/files/{key}"


def build_storage() -> ObjectStorage:
    if settings.storage_backend == "s3":
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "S3 storage is configured but the backend is not installed.",
            detail={"hint": "install boto3 and configure VERITY_S3_* settings"},
        )
    return LocalObjectStorage()


storage: ObjectStorage = build_storage()
