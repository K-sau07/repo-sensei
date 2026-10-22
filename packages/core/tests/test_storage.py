"""Object storage: key layout (unit) and the S3 operations against local SeaweedFS."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from sensei_core.config import Settings
from sensei_core.storage import (
    Bucket,
    ObjectStorage,
    artifact_key,
    repo_prefix,
    snapshot_key,
)

ORG = uuid.UUID("01a15600-0000-7000-8000-000000000001")
REPO = uuid.UUID("01a15600-0000-7000-8000-000000000002")
SHA = "a" * 40


# ------------------------------------------------------------------ keys


def test_key_layout() -> None:
    assert snapshot_key(ORG, REPO, SHA) == f"snapshots/{ORG}/{REPO}/{SHA}.tar.zst"
    assert artifact_key(ORG, REPO, SHA, "chunks", "part-0001.parquet") == (
        f"artifacts/{ORG}/{REPO}/{SHA}/chunks/part-0001.parquet"
    )
    assert repo_prefix(Bucket.ARTIFACTS, ORG, REPO) == f"artifacts/{ORG}/{REPO}/"
    assert snapshot_key(ORG, REPO, "b" * 64).endswith(f"{'b' * 64}.tar.zst")  # SHA-256 repos


@pytest.mark.parametrize(
    "sha", ["", "main", "A" * 40, "a" * 39, "a" * 41, "../" + "a" * 37, "a" * 40 + "\n"]
)
def test_only_commit_shas_name_snapshots(sha: str) -> None:
    with pytest.raises(ValueError, match="not a commit sha"):
        snapshot_key(ORG, REPO, sha)


@pytest.mark.parametrize("parts", [(), ("..",), (".", "x"), ("a/b",), ("x y",), ("",), ("x\n",)])
def test_artifact_names_cannot_escape(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="plain path segments"):
        artifact_key(ORG, REPO, SHA, *parts)


# ------------------------------------------------------------------ S3 (SeaweedFS)


@pytest.fixture
async def storage() -> AsyncIterator[ObjectStorage]:
    store = ObjectStorage(Settings(env="test").s3)
    await store.start()
    yield store
    await store.close()


def _ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


@pytest.mark.integration
async def test_put_get_and_size(storage: ObjectStorage, tmp_path: Path) -> None:
    org, repo = _ids()
    key = snapshot_key(org, repo, SHA)
    data = b"\x28\xb5\x2f\xfd" + bytes(range(256)) * 9000  # ~2.3 MB, several chunks
    path = tmp_path / "snapshot.tar.zst"
    path.write_bytes(data)
    try:
        await storage.put_file(Bucket.SNAPSHOTS, key, path, content_type="application/zstd")
        assert await storage.size(Bucket.SNAPSHOTS, key) == len(data)
        chunks = [c async for c in storage.get_stream(Bucket.SNAPSHOTS, key, chunk_size=512_000)]
        assert len(chunks) > 1
        assert b"".join(chunks) == data
        assert await storage.size(Bucket.SNAPSHOTS, f"{key}.missing") is None
    finally:
        await storage.delete_prefix(Bucket.SNAPSHOTS, repo_prefix(Bucket.SNAPSHOTS, org, repo))


@pytest.mark.integration
async def test_get_bytes_is_bounded(storage: ObjectStorage) -> None:
    org, repo = _ids()
    key = artifact_key(org, repo, SHA, "merkle.msgpack.zst")
    try:
        await storage.put_bytes(Bucket.ARTIFACTS, key, b"x" * 1000)
        assert await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=1000) == b"x" * 1000
        with pytest.raises(ValueError, match="more than 999"):
            await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=999)
        assert await storage.get_bytes(Bucket.ARTIFACTS, f"{key}.missing", max_bytes=1) is None
    finally:
        await storage.delete_prefix(Bucket.ARTIFACTS, repo_prefix(Bucket.ARTIFACTS, org, repo))


@pytest.mark.integration
async def test_presigned_urls_download_and_expire_quickly(storage: ObjectStorage) -> None:
    org, repo = _ids()
    key = artifact_key(org, repo, SHA, "manifest.json")
    try:
        await storage.put_bytes(
            Bucket.ARTIFACTS, key, b'{"ok": true}', content_type="application/json"
        )
        url = await storage.presign_get(Bucket.ARTIFACTS, key, expires_in=60)
        assert "X-Amz-Signature=" in url
        assert "X-Amz-Expires=60" in url
        async with httpx.AsyncClient() as http:
            response = await http.get(url)
            assert (response.status_code, response.content) == (200, b'{"ok": true}')
            tampered = await http.get(url.replace("manifest.json", "other.json"))
            assert tampered.status_code == 403  # the signature covers the key
        for seconds in (0, 3601):
            with pytest.raises(ValueError, match="between 1 second"):
                await storage.presign_get(Bucket.ARTIFACTS, key, expires_in=seconds)
    finally:
        await storage.delete_prefix(Bucket.ARTIFACTS, repo_prefix(Bucket.ARTIFACTS, org, repo))


@pytest.mark.integration
async def test_delete_prefix_removes_one_repository_only(storage: ObjectStorage) -> None:
    org, repo = _ids()
    _, neighbour = _ids()
    keys = [artifact_key(org, repo, SHA, "chunks", f"{n}.bin") for n in range(1203)]
    kept = artifact_key(org, neighbour, SHA, "chunks", "0.bin")
    for key in [*keys[:3], kept]:
        await storage.put_bytes(Bucket.ARTIFACTS, key, b"x")
    # The rest in bulk, to cross the 1000-key DeleteObjects batch.
    for key in keys[3:]:
        await storage.put_bytes(Bucket.ARTIFACTS, key, b"x")
    try:
        # 1,203 objects of one byte: more than one listing page, and only this repository's.
        usage = await storage.prefix_usage(
            Bucket.ARTIFACTS, repo_prefix(Bucket.ARTIFACTS, org, repo)
        )
        assert usage == (len(keys), len(keys))
        deleted = await storage.delete_prefix(
            Bucket.ARTIFACTS, repo_prefix(Bucket.ARTIFACTS, org, repo)
        )
        assert deleted == len(keys)
        assert await storage.size(Bucket.ARTIFACTS, keys[0]) is None
        assert await storage.size(Bucket.ARTIFACTS, kept) == 1
    finally:
        await storage.delete_prefix(Bucket.ARTIFACTS, repo_prefix(Bucket.ARTIFACTS, org, neighbour))


@pytest.mark.integration
@pytest.mark.parametrize(
    "prefix", ["", "artifacts/", f"artifacts/{ORG}/", "artifacts", "snapshots/a/b/"]
)
async def test_broad_prefixes_are_refused(storage: ObjectStorage, prefix: str) -> None:
    with pytest.raises(ValueError, match="refusing to delete"):
        await storage.delete_prefix(Bucket.ARTIFACTS, prefix)


@pytest.mark.integration
async def test_a_stream_abandoned_early_releases_its_connection(storage: ObjectStorage) -> None:
    org, repo = _ids()
    key = artifact_key(org, repo, SHA, "big.bin")
    await storage.put_bytes(Bucket.ARTIFACTS, key, b"x" * 3_000_000)
    try:
        for _ in range(15):  # more than the client's connection pool (10)
            stream = storage.get_stream(Bucket.ARTIFACTS, key, chunk_size=1024)
            assert len(await anext(stream)) > 0
            await stream.aclose()
        assert await storage.size(Bucket.ARTIFACTS, key) == 3_000_000
    finally:
        await storage.delete_prefix(Bucket.ARTIFACTS, repo_prefix(Bucket.ARTIFACTS, org, repo))
