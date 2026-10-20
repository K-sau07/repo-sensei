"""Saving a discovery to Postgres: COPY through a staging table under row-level security."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
import xxhash
from asyncpg.exceptions import InsufficientPrivilegeError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import Repo, RepoFile, RepoSource, Snapshot
from sensei_core.orgs.service import OrgService
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_testing import make_user

pytestmark = pytest.mark.integration


async def _snapshot(db: AsyncSession) -> Snapshot:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=7,
        owner="o",
        name="r",
        full_name="o/r",
        slug="r",
        default_branch="main",
        visibility="public",
        languages={"Python": 1},
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="a" * 40)
    db.add(snapshot)
    await db.flush()
    return snapshot


def _tree(root: Path) -> Path:
    files = {
        "pyproject.toml": '[project]\ndependencies = ["fastapi>=0.115"]\n',
        "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "app/models.py": "x = 1\n",
        "tests/test_main.py": "def test(): ...\n",
        "README.md": "# Demo\n",
        "logo.png": "\0png",
    }
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    return root


async def test_files_summary_and_languages_are_saved(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session)
    found = discover(_tree(tmp_path), max_files=100)
    assert await save_discovery(db_session, snapshot, found) == 6

    rows = {f.path: f for f in (await db_session.scalars(select(RepoFile))).all()}
    assert sorted(rows) == [
        "README.md",
        "app/main.py",
        "app/models.py",
        "logo.png",
        "pyproject.toml",
        "tests/test_main.py",
    ]
    main = rows["app/main.py"]
    content = (tmp_path / "app/main.py").read_bytes()
    assert (main.language, main.role, main.loc, main.size) == ("python", "api", 2, len(content))
    assert main.content_hash == hashlib.sha256(content).digest()
    assert main.fast_hash == xxhash.xxh3_128_digest(content)
    assert main.framework_root == "."
    assert (main.org_id, main.repo_id, main.snapshot_id) == (
        snapshot.org_id,
        snapshot.repo_id,
        snapshot.id,
    )
    assert rows["logo.png"].flags == ["binary"]
    assert rows["logo.png"].loc is None

    await db_session.refresh(snapshot)
    assert (snapshot.file_count, snapshot.loc, snapshot.bytes) == (6, found.loc, found.bytes)
    assert snapshot.summary is not None
    assert snapshot.summary["roles"]["test"]["files"] == 1
    assert snapshot.summary["frameworks"][0]["framework"] == "fastapi"
    repo = await db_session.get(Repo, snapshot.repo_id)
    assert repo is not None
    await db_session.refresh(repo)
    assert repo.languages == {"Python": sum(rows[p].size for p in rows if p.endswith(".py"))}


async def test_saving_again_replaces_the_files(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot = await _snapshot(db_session)
    root = _tree(tmp_path)
    await save_discovery(db_session, snapshot, discover(root, max_files=100))
    (root / "app/models.py").unlink()
    await save_discovery(
        db_session, snapshot, discover(root, ignore_globs=["tests/"], max_files=100)
    )
    count = await db_session.scalar(
        select(func.count()).select_from(RepoFile).where(RepoFile.snapshot_id == snapshot.id)
    )
    assert count == 4


async def test_files_are_org_scoped(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot = await _snapshot(db_session)
    await save_discovery(db_session, snapshot, discover(_tree(tmp_path), max_files=100))
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert (await db_session.scalars(select(RepoFile))).all() == []


async def test_rows_cannot_be_written_into_another_org(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _snapshot(db_session)
    await set_org_context(db_session, uuid.uuid4())  # the session now belongs to another org
    with pytest.raises(InsufficientPrivilegeError, match="row-level security"):
        await save_discovery(db_session, snapshot, discover(_tree(tmp_path), max_files=100))
