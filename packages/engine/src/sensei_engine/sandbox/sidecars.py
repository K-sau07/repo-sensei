"""Services a repository's tests need, for one run (8.2): a throwaway Postgres.

The database runs in its own container on a fresh internal network shared with the run only
(``db`` on it), its data in memory; both go when the run is over. The credentials are
placeholders, the same everywhere (nothing secret: nothing outside can reach it). The run gets
the connection in its environment (``DATABASE_URL`` and the libpq ``PG*`` variables)::

    async with postgres(sandbox, networks) as services:
        await sandbox.run(root, cmd, network=services.network, env=services.env)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sensei_engine.sandbox.network import Networks
from sensei_engine.sandbox.runner import (
    DEADLINE_LABEL,
    LABEL,
    NAME_PREFIX,
    Sandbox,
    SandboxUnavailableError,
    docker_env,
)

POSTGRES_IMAGE = "postgres:17-alpine"
DB_ALIAS = "db"
USER = PASSWORD = "sandbox"  # noqa: S105 - placeholders: only the run can reach it
DATABASE = "app"
READY_SECONDS = 30.0
LIFETIME_SECONDS = 3600  # swept after this, should a run leave it behind


@dataclass(frozen=True, slots=True)
class Services:
    network: str
    env: dict[str, str] = field(default_factory=dict[str, str])


def postgres_env() -> dict[str, str]:
    url = f"postgresql://{USER}:{PASSWORD}@{DB_ALIAS}:5432/{DATABASE}"
    return {
        "DATABASE_URL": url,
        "PGHOST": DB_ALIAS,
        "PGPORT": "5432",
        "PGUSER": USER,
        "PGPASSWORD": PASSWORD,
        "PGDATABASE": DATABASE,
    }


def postgres_argv(binary: str, name: str, network: str, image: str, now: float) -> list[str]:
    """``docker run`` for the database: on the run's network only, data in memory, limited."""
    return [
        binary, "run", "--detach", f"--name={name}", f"--label={LABEL}=sidecar",
        f"--label={DEADLINE_LABEL}={int(now) + LIFETIME_SECONDS}", "--pull=never",
        f"--network={network}", f"--network-alias={DB_ALIAS}",
        "--tmpfs=/var/lib/postgresql/data:rw,size=512m", "--tmpfs=/var/run/postgresql:rw",
        "--tmpfs=/tmp:rw,size=64m", "--memory=1024m", "--pids-limit=256",
        # What its entrypoint needs to hand the data folder to the postgres user, nothing more.
        "--cap-drop=ALL", "--cap-add=CHOWN", "--cap-add=SETUID", "--cap-add=SETGID",
        "--cap-add=FOWNER", "--cap-add=DAC_OVERRIDE", "--security-opt=no-new-privileges",
        "--log-driver=none", f"--env=POSTGRES_USER={USER}", f"--env=POSTGRES_PASSWORD={PASSWORD}",
        f"--env=POSTGRES_DB={DATABASE}", image,
    ]  # fmt: skip


async def _docker(binary: str, *args: str) -> int:
    process = await asyncio.create_subprocess_exec(
        binary,
        *args,
        env=docker_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait()


@asynccontextmanager
async def postgres(
    sandbox: Sandbox, networks: Networks, *, image: str = POSTGRES_IMAGE
) -> AsyncGenerator[Services]:
    """A database for one run, ready when this yields, gone afterwards."""
    binary = sandbox.docker()
    run_id = uuid.uuid4().hex[:12]
    network = await networks.run_network(f"sensei-run-{run_id}")
    name = f"{NAME_PREFIX}db-{run_id}"
    try:
        if await _docker(*postgres_argv(binary, name, network, image, time.time())) != 0:
            raise SandboxUnavailableError(
                f"couldn't start the database ({image}; pull it with `make sandbox-image`)"
            )
        deadline = time.monotonic() + READY_SECONDS
        while True:
            # Over TCP, as the run will reach it (the entrypoint's own start is socket-only).
            ready = await _docker(
                binary, "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", USER, "-d", DATABASE
            )
            if ready == 0:
                break
            if time.monotonic() > deadline:
                raise SandboxUnavailableError("the database didn't start in time")
            await asyncio.sleep(0.5)
        yield Services(network=network, env=postgres_env())
    finally:
        with contextlib.suppress(OSError):
            await _docker(binary, "rm", "--force", name)
        await networks.remove(network)
