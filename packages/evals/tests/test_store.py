"""Runs in ``eval_runs``: the system role writes them, the app role may only read them."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_evals.retrieval import OracleRetriever, RepoRef
from sensei_evals.runner import RunRecord, run_suite
from sensei_evals.schema import GoldenItem, GoldTarget, Layer, QueryClass, Suite
from sensei_evals.store import load_run, save_run

pytestmark = pytest.mark.integration

SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"


async def _run() -> RunRecord:
    item = GoldenItem(
        id="store-1",
        repo="demo",
        sha=SHA,
        query="where is the login handler",
        class_=QueryClass.SYMBOL,
        layers=[Layer.HANDLER],
        gold=[GoldTarget(layer=Layer.HANDLER, path="api/login.py", symbol="login", lines=(1, 9))],
    )
    suite = Suite(name="store-demo", version=1, items=[item])
    return await run_suite(
        suite, suite.items, OracleRetriever(suite.items), RepoRef("demo", SHA), git_sha="0" * 40
    )


async def test_system_role_stores_and_loads_a_run(system_session: AsyncSession) -> None:
    run = await _run()
    await save_run(system_session, run)
    system_session.expunge_all()
    loaded = await load_run(system_session, run.id)
    assert loaded == run


async def test_app_role_reads_but_cannot_write(db_session: AsyncSession) -> None:
    run = await _run()
    # The app role sees rows (staff routes read through it)...
    count = await db_session.scalar(text("SELECT count(*) FROM eval_runs"))
    assert count is not None
    # ...but writing is the system role's job.
    with pytest.raises(ProgrammingError, match="permission denied"):
        await save_run(db_session, run)
