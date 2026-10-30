"""Browsing an indexed snapshot: directory listings, file contents, fuzzy path search and exact
(literal or regex) search in the files.

``ref`` picks the snapshot (see :func:`sensei_core.indexing.snapshots.resolve_snapshot`);
without one it is the last index. Listings come from the snapshot's Merkle tree, so a
directory's ETag is its hash: unchanged directories answer ``304`` to ``If-None-Match``, across
commits too. A file's ETag is its content hash. Paths are checked before anything is looked up
(relative, normal, no ``..``), and only paths discovery recorded can be read or searched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Container
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Any, BinaryIO, Literal

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Float, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.browse import TreeCache
from sensei_api.deps import DbSession
from sensei_api.repos import RepoDep
from sensei_core.errors import DomainError, InvalidError, NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.logging import get_logger
from sensei_core.models import RepoFile, Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key, snapshot_key
from sensei_engine.ingest.classify import decode_text
from sensei_engine.ingest.merkle import DIR, FILE
from sensei_engine.search.grep import (
    DEFAULT_LIMIT,
    MAX_CONTEXT,
    MAX_GLOB_CHARS,
    MAX_GLOBS,
    MAX_LIMIT,
    MAX_PATTERN_CHARS,
    Case,
    ExactSearch,
    GrepBusyError,
    GrepQuery,
    GrepResult,
    GrepUnavailableError,
    InvalidQueryError,
)
from sensei_engine.snapshots import (
    READY_MARKER,
    SnapshotCache,
    UnsafePathError,
    check_path,
    open_file,
)

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["browse"])
log = get_logger(__name__)

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


class SearchUnavailableError(DomainError):
    status_code = 503
    code = "search_unavailable"


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


class GrepRequest(BaseModel):
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN_CHARS)
    regex: bool = Field(
        default=False, description="A regular expression (Rust syntax); else a literal string."
    )
    case: Case = Field(
        default=Case.SENSITIVE,
        description="``smart``: insensitive unless the pattern has an upper-case letter.",
    )
    path_globs: list[Annotated[str, Field(min_length=1, max_length=MAX_GLOB_CHARS)]] = Field(
        default_factory=list,
        max_length=MAX_GLOBS,
        description='Only paths matching these globs ("src/**", "*.py"); "!" in front excludes.',
    )
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    context: int = Field(default=0, ge=0, le=MAX_CONTEXT, description="Lines around each hit.")
    ref: str | None = Field(
        default=None, max_length=255, description="Branch or commit (or its first 7+ characters)."
    )


class GrepHitOut(BaseModel):
    path: str
    line: int
    col: int = Field(description="Where the first match starts: 1-based, in characters.")
    text: str = Field(description="The line, or part of a long one (starting after ``offset``).")
    offset: int = Field(description="Characters of the line before ``text``.")
    ranges: list[tuple[int, int]] = Field(
        description="Each match on the line: start column (1-based) and end column (exclusive)."
    )
    before: list[str] = Field(description="The lines just above, in order.")
    after: list[str] = Field(description="The lines just below.")


class GrepOut(BaseModel):
    commit_sha: str
    hits: list[GrepHitOut]
    truncated: bool = Field(description="There are more matches than ``limit``.")
    timed_out: bool = Field(description="The time limit stopped the search: there may be more.")
    long_lines: int = Field(description="Lines too long to read; a match on one isn't listed.")


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
    if snapshot.merkle_root is None:
        # A first run between discovery and the Merkle stage: listed from the file rows, and
        # not cached (the tree, with its ETags, is moments away).
        listing = await _tree_from_rows(db, snapshot, directory)
        response.headers["Cache-Control"] = "no-store"
        response.headers[COMMIT_HEADER] = snapshot.commit_sha
        return listing
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


async def _tree_from_rows(db: AsyncSession, snapshot: Snapshot, directory: str) -> TreeOut:
    """A directory's direct children from the snapshot's file rows, ordered like the tree's."""
    prefix = f"{directory}/" if directory else ""
    paths = list(
        await db.scalars(
            select(RepoFile.path).where(
                RepoFile.snapshot_id == snapshot.id,
                RepoFile.path.like(f"{_like_escape(prefix)}%", escape="\\"),
            )
        )
    )
    if not paths:
        if directory and await db.scalar(
            select(RepoFile.id).where(
                RepoFile.snapshot_id == snapshot.id, RepoFile.path == directory
            )
        ):
            raise InvalidError("That's a file, not a directory.", code="not_a_directory")
        if directory:
            raise NotFoundError("No such directory in this snapshot.", code="path_not_found")
    files: list[str] = []
    below: dict[str, set[str]] = {}  # subdirectory -> its direct children's names
    for full in paths:
        rest = full[len(prefix) :]
        head, sep, tail = rest.partition("/")
        if not sep:
            files.append(full)
            continue
        below.setdefault(head, set()).add(tail.partition("/")[0])
    rows = {
        row.path: row
        for row in await db.scalars(
            select(RepoFile).where(RepoFile.snapshot_id == snapshot.id, RepoFile.path.in_(files))
        )
    }
    entries = [
        TreeEntry(name=name, path=_join(directory, name), type="dir", child_count=len(below[name]))
        for name in sorted(below, key=lambda n: (n.casefold(), n))
    ]
    for full in sorted(files, key=lambda f: (f.rpartition("/")[2].casefold(), f)):
        row = rows[full]
        entries.append(
            TreeEntry(
                name=full.rpartition("/")[2],
                path=full,
                type="file",
                language=row.language,
                role=row.role,
                size=row.size,
                loc=row.loc,
                flags=list(row.flags),
            )
        )
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


async def unpacked(request: Request, snapshot: Snapshot) -> FilePath:
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
    root = await unpacked(request, snapshot)
    try:
        return await asyncio.to_thread(open_file, root, path)
    except FileNotFoundError:
        pass  # evicted between finding the snapshot and reading it: unpack again, once
    try:
        return await asyncio.to_thread(open_file, await unpacked(request, snapshot), path)
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
        415: {"description": "Binary: metadata and a download URL"},
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
        metadata["download_url"] = await _download_url(request, snapshot, row)
        raise BinaryFileError("This file isn't text; download it instead.", details=metadata)
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


async def _grep(
    request: Request, snapshot: Snapshot, query: GrepQuery, files: Container[str]
) -> GrepResult:
    """The search over the unpacked snapshot; again (once) if the snapshot was evicted from
    disk before or while it ran, so a result never comes from part of the files."""
    search: ExactSearch = request.app.state.exact_search
    for _ in range(2):
        root = await unpacked(request, snapshot)
        # The marker holds when this unpack was made: the same before and after means the
        # search ran over one whole unpack (not one being evicted, or replaced, meanwhile).
        before = await asyncio.to_thread(_unpack_stamp, root)
        if before is None:
            continue
        try:
            result = await search.search(root, query, files=files)
        except FileNotFoundError:
            continue
        if await asyncio.to_thread(_unpack_stamp, root) == before:
            return result
    raise SearchUnavailableError("The snapshot's files kept moving; try again.")


def _unpack_stamp(root: FilePath) -> str | None:
    try:
        return (root / READY_MARKER).read_text()
    except OSError:
        return None


@dataclass(frozen=True, slots=True)
class _Without:
    """``files`` except ``left_out``, without copying a tree's worth of paths per search."""

    files: Container[str]
    left_out: frozenset[str]

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and path in self.files and path not in self.left_out


async def searchable(request: Request, db: AsyncSession, snapshot: Snapshot) -> _Without:
    """The files a search may report: what discovery recorded, binaries left out (rg would stop
    at their first NUL, but could still report a "match" before it)."""
    binaries = frozenset(
        await db.scalars(
            select(RepoFile.path).where(
                RepoFile.snapshot_id == snapshot.id, RepoFile.flags.contains(["binary"])
            )
        )
    )
    if snapshot.merkle_root is None:
        # A first run before its Merkle stage: the file rows are the list.
        recorded: Container[str] = frozenset(
            await db.scalars(select(RepoFile.path).where(RepoFile.snapshot_id == snapshot.id))
        )
    else:
        cache: TreeCache = request.app.state.tree_cache
        recorded = (await cache.get(_storage(request), snapshot)).files
    return _Without(recorded, binaries)


@router.post(
    "/grep",
    response_model=GrepOut,
    responses={
        422: {"description": "A pattern or glob that can't be used (``invalid_pattern``)"},
        503: {"description": "Search is busy or unavailable; try again"},
    },
)
async def grep(*, request: Request, repo: RepoDep, db: DbSession, body: GrepRequest) -> GrepOut:
    """Exact search (ripgrep) in the files discovery recorded: hits in path order, at most
    ``limit`` of them, each with ``context`` lines around it. A search stops after 3 seconds
    with what it found (``timed_out``)."""
    try:
        query = GrepQuery(
            pattern=body.pattern,
            regex=body.regex,
            case=body.case,
            globs=tuple(body.path_globs),
            limit=body.limit,
            context=body.context,
        )
    except InvalidQueryError as exc:
        raise InvalidError(str(exc), code="invalid_pattern") from None
    snapshot = await resolve_snapshot(db, repo, body.ref)
    files = await searchable(request, db, snapshot)
    try:
        result = await _grep(request, snapshot, query, files)
    except InvalidQueryError as exc:
        raise InvalidError(str(exc), code="invalid_pattern") from None
    except GrepBusyError:
        raise SearchUnavailableError(
            "Too many searches are running; try again in a moment.", code="search_busy"
        ) from None
    except GrepUnavailableError:
        log.exception("exact search failed")
        raise SearchUnavailableError("Search isn't available right now.") from None
    return GrepOut(
        commit_sha=snapshot.commit_sha,
        hits=[
            GrepHitOut(
                path=hit.path,
                line=hit.line,
                col=hit.col,
                text=hit.text,
                offset=hit.offset,
                ranges=hit.ranges,
                before=hit.before,
                after=hit.after,
            )
            for hit in result.hits
        ],
        truncated=result.truncated,
        timed_out=result.timed_out,
        long_lines=result.long_lines,
    )
