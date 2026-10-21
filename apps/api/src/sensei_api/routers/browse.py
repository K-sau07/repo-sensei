"""Browsing an indexed snapshot: directory listings, file contents and fuzzy path search.

``ref`` picks the snapshot (see :func:`sensei_core.indexing.snapshots.resolve_snapshot`);
without one it is the last index. Listings come from the snapshot's Merkle tree, so a
directory's ETag is its hash: unchanged directories answer ``304`` to ``If-None-Match``, across
commits too. A file's ETag is its content hash. Paths are checked before anything is looked up
(relative, normal, no ``..``), and only paths discovery recorded can be read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path as FilePath
from typing import Annotated, Any, BinaryIO, Literal

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Float, case, func, literal, select

from sensei_api.browse import TreeCache
from sensei_api.deps import DbSession
from sensei_api.repos import RepoDep
from sensei_core.errors import DomainError, InvalidError, NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import RepoFile, Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key, snapshot_key
from sensei_engine.ingest.classify import decode_text
from sensei_engine.ingest.merkle import DIR, FILE
from sensei_engine.snapshots import SnapshotCache, UnsafePathError, check_path, open_file

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["browse"])

MAX_SERVED_BYTES = 2 * 1024 * 1024  # larger files: metadata and a download link
DOWNLOAD_URL_SECONDS = 900
SEARCH_LIMIT = 50

PathParam = Annotated[str, Query(max_length=4096, description="Relative path; empty for the root.")]
RefParam = Annotated[
    str | None,
    Query(max_length=255, description="Branch or commit (or its first 7+ characters)."),
]


class FileTooLargeError(DomainError):
    status_code = 413
    code = "file_too_large"


class BinaryFileError(DomainError):
    status_code = 415
    code = "binary_file"


class BadPathError(DomainError):
    status_code = 400
    code = "invalid_path"


class TreeEntry(BaseModel):
    name: str
    path: str
    type: Literal["dir", "file"]
    language: str | None = None
    role: str | None = None
    size: int | None = None
    loc: int | None = None
    flags: list[str] | None = None
    child_count: int | None = Field(default=None, description="Entries in a directory.")


class TreeOut(BaseModel):
    """A directory listing. It depends only on what's in the directory (so its ETag holds across
    commits that didn't touch it); the commit it was read from is in ``X-Commit-Sha``."""

    path: str
    entries: list[TreeEntry]


class FileOut(BaseModel):
    """A file. Like a listing, it depends only on the content; the commit is in
    ``X-Commit-Sha``."""

    path: str
    language: str | None
    role: str
    size: int
    loc: int | None
    flags: list[str]
    sha: str = Field(description="Git's object id for the content.")
    encoding: str
    content: str


class SearchHit(BaseModel):
    path: str
    name: str
    language: str | None
    role: str
    size: int
    score: float


class SearchOut(BaseModel):
    commit_sha: str
    query: str
    results: list[SearchHit]


def _path(raw: str) -> str:
    try:
        return check_path(raw)
    except UnsafePathError:
        raise BadPathError("That isn't a path inside the repository.") from None


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _not_modified(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match")
    if not header:
        return False
    tags = {tag.strip().removeprefix("W/") for tag in header.split(",")}
    return etag in tags or "*" in tags


COMMIT_HEADER = "X-Commit-Sha"


def _cache_headers(response: Response, etag: str, snapshot: Snapshot) -> None:
    response.headers["ETag"] = etag
    # Private (it's behind sign-in), and always revalidated: the ETag makes that cheap.
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers[COMMIT_HEADER] = snapshot.commit_sha


def _not_modified_response(etag: str, snapshot: Snapshot) -> Response:
    # A 304 updates the cached response's headers, so the commit stays current with it.
    return Response(
        status_code=status.HTTP_304_NOT_MODIFIED,
        headers={
            "ETag": etag,
            "Cache-Control": "private, no-cache",
            COMMIT_HEADER: snapshot.commit_sha,
        },
    )


def _storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage = request.app.state.storage
    return storage


_COMMIT_HEADER_DOC = {
    "X-Commit-Sha": {
        "description": "The commit the response was read from.",
        "schema": {"type": "string"},
    }
}


@router.get(
    "/tree",
    response_model=TreeOut,
    responses={
        200: {"headers": _COMMIT_HEADER_DOC},
        304: {"description": "Not modified", "headers": _COMMIT_HEADER_DOC},
    },
)
async def tree(
    *,
    request: Request,
    response: Response,
    repo: RepoDep,
    db: DbSession,
    path: PathParam = "",
    ref: RefParam = None,
) -> TreeOut | Response:
    """The direct children of a directory, directories first. ETag: the directory's hash (its
    ``t1`` version changes whenever what a listing shows does); ``X-Commit-Sha``: the commit."""
    directory = _path(path)
    snapshot = await resolve_snapshot(db, repo, ref)
    cache: TreeCache = request.app.state.tree_cache
    merkle = await cache.get(_storage(request), snapshot)
    dir_hash = merkle.dir_hash(directory)
    if dir_hash is None:
        if directory in merkle.files:
            raise InvalidError("That's a file, not a directory.", code="not_a_directory")
        raise NotFoundError("No such directory in this snapshot.", code="path_not_found")
    etag = f'"t1-{dir_hash.hex()}"'
    if _not_modified(request, etag):
        return _not_modified_response(etag, snapshot)

    children = merkle.children[directory]
    file_paths = [_join(directory, e.name) for e in children if e.kind == FILE]
    rows = {
        row.path: row
        for row in await db.scalars(
            select(RepoFile).where(
                RepoFile.snapshot_id == snapshot.id, RepoFile.path.in_(file_paths)
            )
        )
    }
    entries: list[TreeEntry] = []
    for entry in sorted(children, key=lambda e: (e.kind != DIR, e.name.casefold(), e.name)):
        child = _join(directory, entry.name)
        if entry.kind == DIR:
            count = len(merkle.children.get(child, []))
            entries.append(TreeEntry(name=entry.name, path=child, type="dir", child_count=count))
            continue
        row = rows.get(child)
        entries.append(
            TreeEntry(
                name=entry.name,
                path=child,
                type="file",
                language=row.language if row else None,
                role=row.role if row else None,
                size=row.size if row else None,
                loc=row.loc if row else None,
                flags=list(row.flags) if row else None,
            )
        )
    _cache_headers(response, etag, snapshot)
    return TreeOut(path=directory, entries=entries)


def _metadata(snapshot: Snapshot, row: RepoFile) -> dict[str, Any]:
    return {
        "commit_sha": snapshot.commit_sha,
        "path": row.path,
        "language": row.language,
        "role": row.role,
        "size": row.size,
        "loc": row.loc,
        "flags": list(row.flags),
        "sha": row.blob_sha,
    }


async def _unpacked(request: Request, snapshot: Snapshot) -> FilePath:
    storage = _storage(request)
    cache: SnapshotCache = request.app.state.snapshot_cache
    key = snapshot_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha)

    async def download(out: FilePath) -> None:
        if await storage.size(Bucket.SNAPSHOTS, key) is None:
            raise NotFoundError(
                "This snapshot's files are missing. Reindex the repository.",
                code="snapshot_unavailable",
            )
        with out.open("wb") as handle:
            async for chunk in storage.get_stream(Bucket.SNAPSHOTS, key):
                handle.write(chunk)

    return await cache.unpacked(snapshot.id, download)


async def _open(request: Request, snapshot: Snapshot, path: str) -> BinaryIO:
    """The snapshot's file, opened safely (see ``open_file``), unpacking the snapshot first."""
    root = await _unpacked(request, snapshot)
    try:
        return await asyncio.to_thread(open_file, root, path)
    except FileNotFoundError:
        pass  # evicted between finding the snapshot and reading it: unpack again, once
    try:
        return await asyncio.to_thread(open_file, await _unpacked(request, snapshot), path)
    except FileNotFoundError:
        # Recorded by discovery, yet not in the archive.
        raise NotFoundError("No such file in this snapshot.", code="path_not_found") from None


async def _read(request: Request, snapshot: Snapshot, path: str, limit: int) -> bytes:
    with await _open(request, snapshot, path) as handle:
        data = await asyncio.to_thread(handle.read, limit + 1)
    if len(data) > limit:
        msg = f"{path} is larger than {limit} bytes"
        raise ValueError(msg)
    return data


async def _download_url(request: Request, snapshot: Snapshot, row: RepoFile) -> str:
    """A short-lived link to the file's bytes (stored once per content)."""
    storage = _storage(request)
    key = artifact_key(
        snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, "files", row.blob_sha
    )
    if await storage.size(Bucket.ARTIFACTS, key) is None:
        # Streamed from the unpacked snapshot: large files never sit in memory.
        with await _open(request, snapshot, row.path) as handle:
            await storage.put_stream(Bucket.ARTIFACTS, key, handle)
    return await storage.presign_get(Bucket.ARTIFACTS, key, expires_in=DOWNLOAD_URL_SECONDS)


@router.get(
    "/file",
    response_model=FileOut,
    responses={
        200: {"headers": _COMMIT_HEADER_DOC},
        304: {"description": "Not modified", "headers": _COMMIT_HEADER_DOC},
        413: {"description": "Over 2 MB: metadata and a download URL instead"},
        415: {"description": "Binary: metadata only"},
    },
)
async def file(
    *,
    request: Request,
    response: Response,
    repo: RepoDep,
    db: DbSession,
    path: PathParam,
    ref: RefParam = None,
) -> FileOut | Response:
    """A text file's content. ETag: the content's hash; ``X-Commit-Sha``: the commit."""
    wanted = _path(path)
    if not wanted:
        raise BadPathError("Give the path of a file.")
    snapshot = await resolve_snapshot(db, repo, ref)
    row = await db.scalar(
        select(RepoFile).where(RepoFile.snapshot_id == snapshot.id, RepoFile.path == wanted)
    )
    if row is None:
        raise NotFoundError("No such file in this snapshot.", code="path_not_found")
    etag = f'"f1-{row.content_hash.hex()}"'
    if _not_modified(request, etag):
        return _not_modified_response(etag, snapshot)
    metadata = _metadata(snapshot, row)
    if "binary" in row.flags:
        raise BinaryFileError("This file isn't text.", details=metadata)
    if row.size > MAX_SERVED_BYTES:
        metadata["download_url"] = await _download_url(request, snapshot, row)
        raise FileTooLargeError(
            "This file is too large to show here; download it instead.", details=metadata
        )
    decoded = decode_text(await _read(request, snapshot, wanted, MAX_SERVED_BYTES))
    if decoded is None:
        raise BinaryFileError("This file isn't text.", details=metadata)
    content, encoding = decoded
    _cache_headers(response, etag, snapshot)
    return FileOut(
        path=row.path,
        language=row.language,
        role=row.role,
        size=row.size,
        loc=row.loc,
        flags=list(row.flags),
        sha=row.blob_sha,
        encoding=encoding,
        content=content,
    )


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/files/search", response_model=SearchOut)
async def search_files(
    repo: RepoDep,
    db: DbSession,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    ref: RefParam = None,
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT)] = SEARCH_LIMIT,
) -> SearchOut:
    """Fuzzy path search (for ⌘K): a path matches if it contains the query's characters in
    order, or looks like it by trigrams; file names that match rank first."""
    query = q.strip()
    if not query:
        raise InvalidError("Type something to search for.", code="invalid_query")
    snapshot = await resolve_snapshot(db, repo, ref)
    name = func.regexp_replace(RepoFile.path, r"^.*/", "")
    lowered = query.lower()
    in_order = "%" + "%".join(_like_escape(ch) for ch in lowered) + "%"
    score: ColumnElement[float] = (
        func.word_similarity(lowered, func.lower(name)) * 2
        + func.word_similarity(lowered, func.lower(RepoFile.path))
        + case((func.lower(name).startswith(lowered, autoescape=True), 1.0), else_=0.0)
        + case((func.lower(RepoFile.path).contains(lowered, autoescape=True), 0.5), else_=0.0)
    ).cast(Float)
    rows = await db.execute(
        select(RepoFile.path, name, RepoFile.language, RepoFile.role, RepoFile.size, score)
        .where(
            RepoFile.snapshot_id == snapshot.id,
            RepoFile.path.ilike(in_order, escape="\\")
            | RepoFile.path.op("%>")(literal(lowered)),  # trigram word similarity
        )
        .order_by(score.desc(), func.length(RepoFile.path), RepoFile.path)
        .limit(limit)
    )
    results = [
        SearchHit(path=p, name=n, language=lang, role=role, size=size, score=round(s, 4))
        for p, n, lang, role, size, s in rows
    ]
    return SearchOut(commit_sha=snapshot.commit_sha, query=query, results=results)
