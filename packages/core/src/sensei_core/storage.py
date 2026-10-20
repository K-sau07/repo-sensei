"""Object storage (S3-compatible: S3, R2, SeaweedFS locally) for snapshots and index artifacts.

Key layout, one prefix per repository so disconnecting purges it in one call::

    snapshots/{org}/{repo}/{sha}.tar.zst        (the snapshots bucket)
    artifacts/{org}/{repo}/{sha}/<name>...      (the artifacts bucket)

Keys are built only from ids and a validated commit sha, never from repository content, so a
file path inside a repository can't escape its prefix.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, cast

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from sensei_core.config import S3Settings

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")  # SHA-1 or SHA-256 object names
_PART = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
MAX_PRESIGN_SECONDS = 3600
_DELETE_BATCH = 1000  # S3's DeleteObjects limit


class Bucket(enum.StrEnum):
    SNAPSHOTS = "snapshots"
    ARTIFACTS = "artifacts"


def _sha(commit_sha: str) -> str:
    if not _SHA.match(commit_sha):
        msg = f"not a commit sha: {commit_sha!r}"
        raise ValueError(msg)
    return commit_sha


def repo_prefix(bucket: Bucket, org_id: uuid.UUID, repo_id: uuid.UUID) -> str:
    """Everything stored for one repository in ``bucket``."""
    return f"{bucket.value}/{org_id}/{repo_id}/"


def snapshot_key(org_id: uuid.UUID, repo_id: uuid.UUID, commit_sha: str) -> str:
    return f"{repo_prefix(Bucket.SNAPSHOTS, org_id, repo_id)}{_sha(commit_sha)}.tar.zst"


def artifact_key(org_id: uuid.UUID, repo_id: uuid.UUID, commit_sha: str, *parts: str) -> str:
    if not parts or not all(_PART.match(part) and part not in (".", "..") for part in parts):
        msg = f"artifact names must be plain path segments: {parts!r}"
        raise ValueError(msg)
    prefix = repo_prefix(Bucket.ARTIFACTS, org_id, repo_id)
    return f"{prefix}{_sha(commit_sha)}/{'/'.join(parts)}"


class ObjectStorage:
    """One long-lived S3 client per process: ``await storage.start()``, then ``close()``."""

    def __init__(self, settings: S3Settings) -> None:
        self._settings = settings
        self._session = aioboto3.Session()
        self._stack: AsyncExitStack | None = None
        self._s3: S3Client | None = None

    def bucket_name(self, bucket: Bucket) -> str:
        if bucket is Bucket.SNAPSHOTS:
            return self._settings.bucket_snapshots
        return self._settings.bucket_artifacts

    async def start(self) -> None:
        if self._s3 is not None:
            return
        stack = AsyncExitStack()
        client = cast(
            "AbstractAsyncContextManager[S3Client]",
            self._session.client(  # pyright: ignore[reportUnknownMemberType] - typed by the cast
                "s3",
                endpoint_url=self._settings.endpoint,
                region_name=self._settings.region,
                aws_access_key_id=self._settings.access_key.get_secret_value(),
                aws_secret_access_key=self._settings.secret_key.get_secret_value(),
                config=Config(
                    signature_version="s3v4",
                    # Path-style works everywhere (SeaweedFS and R2 need it for custom endpoints).
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 5, "mode": "standard"},
                ),
            ),
        )
        self._s3 = await stack.enter_async_context(client)
        self._stack = stack

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._s3 = None

    @property
    def _client(self) -> S3Client:
        if self._s3 is None:
            msg = "ObjectStorage.start() was not called"
            raise RuntimeError(msg)
        return self._s3

    async def put_file(
        self, bucket: Bucket, key: str, path: Path, *, content_type: str | None = None
    ) -> None:
        """Upload a local file (multipart for large ones)."""
        extra = {"ContentType": content_type} if content_type else None
        await self._client.upload_file(  # pyright: ignore[reportUnknownMemberType]
            str(path), self.bucket_name(bucket), key, ExtraArgs=extra
        )

    async def put_bytes(
        self, bucket: Bucket, key: str, data: bytes, *, content_type: str | None = None
    ) -> None:
        if content_type:
            await self._client.put_object(
                Bucket=self.bucket_name(bucket), Key=key, Body=data, ContentType=content_type
            )
        else:
            await self._client.put_object(Bucket=self.bucket_name(bucket), Key=key, Body=data)

    async def get_stream(
        self, bucket: Bucket, key: str, *, chunk_size: int = 1024 * 1024
    ) -> AsyncGenerator[bytes]:
        """The object's bytes in chunks, without holding it all in memory. Stopping early?
        ``aclose()`` it (or iterate to the end) to release the connection."""
        response = await self._client.get_object(Bucket=self.bucket_name(bucket), Key=key)
        body = response["Body"]
        # Entering the body hands back the raw HTTP response; the context only serves to release
        # the connection, even when the caller stops early. Chunks come from the body itself.
        async with body:
            async for chunk in body.iter_chunks(chunk_size):
                yield chunk

    async def size(self, bucket: Bucket, key: str) -> int | None:
        """Bytes stored at ``key``, or None if there is nothing there."""
        try:
            head = await self._client.head_object(Bucket=self.bucket_name(bucket), Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return int(head["ContentLength"])

    async def presign_get(self, bucket: Bucket, key: str, *, expires_in: int = 900) -> str:
        """A time-limited download URL (at most an hour)."""
        if not 0 < expires_in <= MAX_PRESIGN_SECONDS:
            msg = f"presigned URLs last between 1 second and {MAX_PRESIGN_SECONDS} seconds"
            raise ValueError(msg)
        return await self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name(bucket), "Key": key},
            ExpiresIn=expires_in,
        )

    async def delete_prefix(self, bucket: Bucket, prefix: str) -> int:
        """Delete every object under ``prefix``; returns how many. Only repository-level (or
        deeper) prefixes are accepted, so a bug can't empty a bucket."""
        segments = [segment for segment in prefix.split("/") if segment]
        if not prefix.endswith("/") or len(segments) < 3 or segments[0] != bucket.value:
            msg = f"refusing to delete under {prefix!r}: give a repository prefix or deeper"
            raise ValueError(msg)
        name = self.bucket_name(bucket)
        deleted = 0
        paginator = self._client.get_paginator("list_objects_v2")
        batch: list[str] = []
        async for page in paginator.paginate(Bucket=name, Prefix=prefix):
            for item in page.get("Contents", []):
                if "Key" in item:
                    batch.append(item["Key"])
                if len(batch) == _DELETE_BATCH:
                    deleted += await self._delete(name, batch)
                    batch = []
        if batch:
            deleted += await self._delete(name, batch)
        return deleted

    async def _delete(self, name: str, keys: list[str]) -> int:
        response = await self._client.delete_objects(
            Bucket=name, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True}
        )
        errors = response.get("Errors", [])
        if errors:
            msg = f"could not delete {len(errors)} objects (first: {errors[0].get('Key')})"
            raise RuntimeError(msg)
        return len(keys)
