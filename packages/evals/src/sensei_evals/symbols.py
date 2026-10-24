"""The symbol retriever: step 2.4's lookup (:func:`~sensei_engine.symbols.lookup.find_symbols`)
over a benchmark checkout's symbol table.

The checkout (or its renamed copy) is indexed the way the indexer's discover and symbols stages
do it, in this process: its files and their definitions are saved as a snapshot of the pinned
commit, in a repository of the eval org (:data:`EVAL_ORG`). Each run indexes afresh, so a changed
extractor is never measured on an older symbol table; the repository's earlier eval snapshots
are deleted first. The index goes to its own throwaway database (:data:`EVAL_DATABASE`, made
by ``sensei-eval prepare-db``; ``--opt db=<name>`` picks another), on the settings' server
through the system role: never into a database that serves users. Runs are still stored in
the settings' database.

A golden query is a question, not a name. The names it asks about are what it quotes in
backticks, or else its identifier-like words other than question words (``where is the Graph
class defined?`` → ``Graph``); each is looked up and the hits are merged, best score first.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from sqlalchemy import delete, make_url, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sensei_core.config import get_settings, with_database
from sensei_core.db import make_database
from sensei_core.models import Org, Repo, RepoSource, Snapshot
from sensei_engine.ingest.classify import Flag
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.parse.stage import parse_files, parseable
from sensei_engine.symbols.lookup import SymbolHit, find_symbols
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_evals.retrieval import MAX_CORPUS_FILES, Hit, RepoRef, RetrieverContext

EVAL_DATABASE = "sensei_eval"
_EVAL_NAME = re.compile(rf"{EVAL_DATABASE}[a-z0-9_]*")
EVAL_ORG = "sensei-evals"
EVAL_REF = "eval"
# Files whose content was never read as text, as the parse stage skips them.
UNPARSEABLE_FLAGS = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})

_QUOTED = re.compile(r"`([^`]+)`")
_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
QUESTION_WORDS = frozenset(
    """
    a an the it this that is are was be of in on for to with and or what where which who how
    does do show
    me find get see look up defined declared implemented written located lives live code
    class function method model component hook module variable type interface constant
    """.split()  # noqa: SIM905 - a word list reads better as text
)

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def lookup_names(query: str) -> list[str]:
    """The names a query asks about, in order: its backticked code, or else its identifier-like
    words that aren't question words."""
    quoted = [part.strip() for part in _QUOTED.findall(query) if part.strip()]
    if quoted:
        return list(dict.fromkeys(quoted))
    words = [word for word in _NAME.findall(query) if word.lower() not in QUESTION_WORDS]
    return list(dict.fromkeys(words))


def _github_id(repo_id: str) -> int:
    """A stand-in GitHub id for a benchmark repository: negative, so never a real one."""
    return -int.from_bytes(hashlib.sha256(repo_id.encode()).digest()[:6], "big") - 1


async def _eval_repo(db: AsyncSession, repo_id: str) -> Repo:
    org = await db.scalar(select(Org).where(Org.slug == EVAL_ORG))
    if org is None:
        org = Org(slug=EVAL_ORG, name="Sensei evals")
        db.add(org)
        await db.flush()
    row = await db.scalar(
        select(Repo).where(Repo.org_id == org.id, Repo.slug == repo_id, Repo.deleted_at.is_(None))
    )
    if row is None:
        row = Repo(
            org_id=org.id,
            source=RepoSource.PUBLIC_URL,
            github_repo_id=_github_id(repo_id),
            owner=EVAL_ORG,
            name=repo_id,
            full_name=f"{EVAL_ORG}/{repo_id}",
            slug=repo_id,
            default_branch=EVAL_REF,
            visibility="private",
        )
        db.add(row)
        await db.flush()
    return row


async def index_checkout(db: AsyncSession, repo: RepoRef, checkout: Path) -> Snapshot:
    """Index ``checkout`` (at ``repo.sha``) as a fresh snapshot of the eval repository
    ``repo.id``: its files as discover saves them, and the symbols of every file the parse
    stage would parse. Earlier snapshots of that repository are deleted. Needs a session that
    may write every org's rows (the system role)."""
    row = await _eval_repo(db, repo.id)
    await db.execute(delete(Snapshot).where(Snapshot.repo_id == row.id))
    snapshot = Snapshot(org_id=row.org_id, repo_id=row.id, ref=EVAL_REF, commit_sha=repo.sha)
    db.add(snapshot)
    await db.flush()
    discovery = discover(checkout, max_files=MAX_CORPUS_FILES)
    await save_discovery(db, snapshot, discovery)
    pairs = [
        (file.path, file.language)
        for file in discovery.files
        if file.language is not None
        and parseable(file.language)
        and not UNPARSEABLE_FLAGS.intersection(file.flags)
    ]
    found = [
        FileSymbols(parsed.path, parsed.language, parsed.symbols)
        for parsed in parse_files(checkout, pairs)
        if parsed.symbols
    ]
    file_ids = {file.path: file.id for file in discovery.files}
    snapshot.symbol_count = await save_symbols(db, snapshot, file_ids, found)
    await db.flush()
    return snapshot


def _hit(found: SymbolHit) -> Hit:
    return Hit(
        path=found.path,
        lines=(found.start_line, found.end_line),
        score=found.score,
        symbol=found.qualified_name,
    )


class SymbolRetriever:
    """Each name the query asks about looked up with ``find_symbols`` in one snapshot, the hits
    merged best first (a symbol found under two names counts once, at its best score).

    With ``checkout``, :meth:`prepare` indexes it first (see :func:`index_checkout`) and then
    calls ``analyze``; otherwise ``snapshot_id`` names a snapshot that has its symbols."""

    name = "symbols"

    def __init__(
        self,
        sessions: Sessions,
        *,
        snapshot_id: uuid.UUID | None = None,
        checkout: Path | None = None,
        close: Callable[[], Awaitable[None]] | None = None,
        analyze: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if snapshot_id is None and checkout is None:
            msg = "the symbol retriever needs a snapshot or a checkout to index"
            raise ValueError(msg)
        self._sessions = sessions
        self._snapshot_id = snapshot_id
        self._checkout = checkout
        self._close = close
        self._analyze = analyze
        self.symbol_count: int | None = None

    async def prepare(self, repo: RepoRef) -> None:
        """Index the checkout, once (the runner calls this before timing any search)."""
        if self._snapshot_id is not None or self._checkout is None:
            return
        async with self._sessions() as db:
            snapshot = await index_checkout(db, repo, self._checkout)
            self._snapshot_id = snapshot.id
            self.symbol_count = snapshot.symbol_count
        if self._analyze is not None:
            await self._analyze()

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        await self.prepare(repo)
        snapshot_id = self._snapshot_id
        if snapshot_id is None:  # pragma: no cover - prepare set it or raised
            return []
        best: dict[str, SymbolHit] = {}
        async with self._sessions() as db:
            for name in lookup_names(query):
                for found in await find_symbols(db, snapshot_id, name, limit=k):
                    kept = best.get(found.symbol_key)
                    if kept is None or found.score > kept.score:
                        best[found.symbol_key] = found
        # Stable for equal scores: the order the names and their hits came in.
        ranked = sorted(best.values(), key=lambda found: -found.score)
        return [_hit(found) for found in ranked[:k]]

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()


def eval_database_url(url: str, name: str = EVAL_DATABASE, *, reserved: Iterable[str] = ()) -> str:
    """``url`` (a server's, naming the app's database) pointed at the eval database ``name``.

    The eval database is dropped and recreated at will, so the name must say what it is (start
    with :data:`EVAL_DATABASE`, letters, digits and ``_`` only) and be neither the database
    ``url`` names nor any of ``reserved`` (the test database)."""
    current = make_url(url).database
    if not current:
        msg = "the database URL names no database: can't tell which one is the app's"
        raise ValueError(msg)
    if not _EVAL_NAME.fullmatch(name):
        msg = (
            f"an eval database's name starts with {EVAL_DATABASE!r} (letters, digits, _): {name!r}"
        )
        raise ValueError(msg)
    if name == current or name in set(reserved):
        msg = f"the eval index needs its own database, not {name!r}"
        raise ValueError(msg)
    return with_database(url, name)


def checkout_symbols(context: RetrieverContext) -> SymbolRetriever:
    """The ``symbols`` retriever of ``sensei-eval run``: the checkout indexed into the eval
    database (``--opt db=<name>``, default :data:`EVAL_DATABASE`), through the system role."""
    if context.repo.checkout is None:
        msg = "the symbols retriever needs a checkout of the repository (--checkout)"
        raise ValueError(msg)
    settings = get_settings().database
    name = context.options.get("db", EVAL_DATABASE)
    reserved = [settings.test_name]
    url = eval_database_url(settings.system_url.get_secret_value(), name, reserved=reserved)
    database = make_database(settings, url=url)
    owner_url = eval_database_url(settings.migrate_url.get_secret_value(), name, reserved=reserved)
    return SymbolRetriever(
        database.session,
        checkout=context.repo.checkout,
        close=database.dispose,
        analyze=lambda: analyze_tables(owner_url),
    )


async def analyze_tables(url: str) -> None:
    """Fresh planner statistics for the tables an index was just loaded into (through the
    schema owner: the system role may not ``ANALYZE``). Without them, lookups right after a
    big load pick the wrong index until autovacuum gets to it: langflow's prefix leg took
    60 ms instead of 1.4 ms."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ANALYZE nodes, files"))
    finally:
        await engine.dispose()


async def recreate_database(name: str = EVAL_DATABASE) -> str:
    """Drop and create the eval database ``name`` on the settings' server (through the schema
    owner's role); returns its URL, for :func:`migrate_database`."""
    database = get_settings().database
    migrate_url = database.migrate_url.get_secret_value()
    url = eval_database_url(migrate_url, name, reserved=[database.test_name])
    engine = create_async_engine(migrate_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            quoted = name.replace('"', '""')
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{quoted}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{quoted}"'))
    finally:
        await engine.dispose()
    return url


def migrate_database(alembic_ini: Path, url: str) -> None:
    """Migrate the database at ``url`` to the latest schema."""
    command = [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "-x", f"url={url}"]
    result = subprocess.run(  # noqa: S603 - our interpreter, our config
        [*command, "upgrade", "head"], capture_output=True, text=True, check=False, timeout=600
    )
    if result.returncode != 0:
        name = make_url(url).database
        msg = f"migrating {name} failed:\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)
