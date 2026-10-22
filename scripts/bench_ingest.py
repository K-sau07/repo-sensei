"""Benchmark ingestion: one full index job (clone → discover → merkle → finalize) of a public
GitHub repository against the local stack (``make up``), timed per stage.

The job runs in this process, the way a worker runs it (the chain, eagerly), from a cold cache
(a fresh temporary directory), on a repository connected by URL for the run and removed after
(database rows and stored objects), unless ``--keep``. ``--sha`` pins the commit; otherwise the
default branch's head is indexed. It records wall time per stage and overall, files and lines,
peak memory (this process and its git children) and what was stored in object storage::

    uv run python scripts/bench_ingest.py https://github.com/langflow-ai/langflow \\
        --sha 3ec974b7ee82d1c1ca630e68547edbde5c64a8a9 --json out.json

GitHub is called anonymously (60 requests an hour per IP; a run makes a few).
"""

from __future__ import annotations

import argparse
import re
import resource
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
from sqlalchemy import delete

from sensei_api.routers.constellation import (
    PAGE_SIZE,
    ConstellationOut,
    dirs_out,
    points_out,
    stored_generation,
)
from sensei_core.config import Settings
from sensei_core.github.client import Credential, as_object
from sensei_core.models import IndexJob, JobKind, JobStatus, Org, Repo, RepoSource, Snapshot
from sensei_core.storage import Bucket, artifact_key, repo_prefix
from sensei_engine.ingest import constellation, merkle
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.tasks.orchestrate import run_index_job

_URL = re.compile(r"^(?:https?://)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")
_SHA = re.compile(r"[0-9a-f]{40}")
STAGES = ("clone", "discover", "merkle", "finalize")


@dataclass
class Result:
    repository: str
    commit: str | None
    status: str
    error: dict[str, Any] | None
    wall_seconds: float
    stage_seconds: dict[str, float]
    files: int | None
    loc: int | None
    checkout_bytes: int | None
    peak_rss_mb: float
    peak_child_rss_mb: float
    stored: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    # What browsing costs the API once it is indexed (decoding the stored tree and
    # constellation, building the first page of points), in ms; bytes of that page as JSON.
    browse: dict[str, float] = field(default_factory=dict[str, float])


def _rss_mb(who: int) -> float:
    """Peak resident memory of ``who`` (``RUSAGE_SELF``/``RUSAGE_CHILDREN``), in MiB."""
    peak = resource.getrusage(who).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _seconds(entry: dict[str, Any]) -> float | None:
    started, finished = entry.get("started_at"), entry.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()


async def _connect(
    runtime: Runtime, owner: str, name: str, sha: str | None
) -> tuple[uuid.UUID, ...]:
    """An org of its own with the repository connected by URL, and a queued full job."""
    meta = as_object(
        await runtime.github_client.get_json(f"/repos/{owner}/{name}", Credential.anonymous())
    )
    if not meta or meta.get("private"):
        msg = f"{owner}/{name} isn't a public GitHub repository"
        raise SystemExit(msg)
    async with runtime.system_database.session() as db:
        org = Org(slug=f"bench-{uuid.uuid4().hex[:10]}", name="Ingestion benchmark")
        db.add(org)
        await db.flush()
        repo = Repo(
            org_id=org.id,
            source=RepoSource.PUBLIC_URL,
            github_repo_id=int(meta["id"]),
            owner=owner,
            name=name,
            full_name=f"{owner}/{name}",
            slug=name.lower(),
            # A commit id works as the "branch": GitHub resolves it to itself, and the clone
            # fetches exactly that commit.
            default_branch=sha or str(meta.get("default_branch") or "main"),
            visibility="public",
            size_kb=int(meta.get("size") or 0),
        )
        db.add(repo)
        await db.flush()
        job = IndexJob(org_id=org.id, repo_id=repo.id, kind=JobKind.FULL)
        db.add(job)
        await db.flush()
        return org.id, repo.id, job.id


async def _measure(
    runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[IndexJob, Snapshot | None, dict[str, dict[str, int]]]:
    async with runtime.system_database.session() as db:
        job = await db.get(IndexJob, job_id)
        if job is None:
            msg = "the job is gone"
            raise RuntimeError(msg)
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
    storage = await runtime.storage()
    stored: dict[str, dict[str, int]] = {}
    for bucket in Bucket:
        objects, size = await storage.prefix_usage(bucket, repo_prefix(bucket, org_id, repo_id))
        stored[bucket.value] = {"objects": objects, "bytes": size}
    return job, snapshot, stored


async def _browse(runtime: Runtime, snapshot: Snapshot) -> dict[str, float]:
    """Time what the API does to show the tree and the constellation (from object storage,
    nothing cached), as ``GET …/tree`` and the first ``GET …/constellation`` page would."""
    storage = await runtime.storage()
    org, repo, sha = snapshot.org_id, snapshot.repo_id, snapshot.commit_sha
    timings: dict[str, float] = {}

    started = time.perf_counter()
    tree_bytes = await storage.get_bytes(
        Bucket.ARTIFACTS, artifact_key(org, repo, sha, merkle.ARTIFACT_NAME), max_bytes=1 << 30
    )
    if tree_bytes is not None:
        tree = merkle.MerkleTree.from_bytes(tree_bytes)
        timings["tree_load_ms"] = (time.perf_counter() - started) * 1000
        timings["root_entries"] = len(tree.children.get("", []))

    generation = stored_generation(snapshot)
    if generation is not None:
        started = time.perf_counter()
        sky_bytes = await storage.get_bytes(
            Bucket.ARTIFACTS,
            artifact_key(org, repo, sha, *constellation.artifact_parts(generation)),
            max_bytes=constellation.MAX_STORED_BYTES,
        )
        if sky_bytes is not None:
            sky = constellation.Constellation.from_bytes(sky_bytes)
            timings["constellation_load_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            page = ConstellationOut(
                status="complete",
                commit_sha=sha,
                generation=sky.generation,
                total=len(sky.points),
                available=len(sky.points),
                since=0,
                next=min(PAGE_SIZE, len(sky.points)),
                dirs=dirs_out(sky.dirs),
                points=points_out(sky.points[:PAGE_SIZE]),
            ).model_dump_json()
            timings["first_page_ms"] = (time.perf_counter() - started) * 1000
            timings["first_page_kib"] = len(page) / 1024
    return {key: round(value, 1) for key, value in timings.items()}


async def _remove(runtime: Runtime, org_id: uuid.UUID, repo_id: uuid.UUID) -> None:
    storage = await runtime.storage()
    for bucket in Bucket:
        await storage.delete_prefix(bucket, repo_prefix(bucket, org_id, repo_id))
    async with runtime.system_database.session() as db:
        await db.execute(delete(Org).where(Org.id == org_id))


def bench(url: str, sha: str | None, *, keep: bool) -> Result:
    match = _URL.match(url.strip())
    if not match:
        msg = f"not a GitHub repository URL: {url}"
        raise SystemExit(msg)
    if sha is not None and not _SHA.fullmatch(sha):
        msg = f"--sha must be a full 40-character commit id, not {sha!r}"
        raise SystemExit(msg)
    owner, name = match["owner"], match["name"]
    with tempfile.TemporaryDirectory(prefix="bench-ingest-") as cache:
        base = Settings()
        settings = base.model_copy(
            update={"ingest": base.ingest.model_copy(update={"cache_dir": Path(cache)})}
        )
        runtime = Runtime(settings)
        set_runtime(runtime)
        try:
            org_id, repo_id, job_id = runtime.run(_connect(runtime, owner, name, sha))
            kwargs = {"job_id": str(job_id), "org_id": str(org_id), "repo_id": str(repo_id)}
            started = time.perf_counter()
            run_index_job.apply(kwargs=kwargs).get()
            wall = time.perf_counter() - started
            job, snapshot, stored = runtime.run(_measure(runtime, org_id, repo_id, job_id))
            stages = job.stages or {}
            details = stages.get("discover", {}).get("details", {})
            timed = {s: t for s in STAGES if (t := _seconds(stages.get(s, {}))) is not None}
            result = Result(
                repository=f"{owner}/{name}",
                commit=snapshot.commit_sha if snapshot else None,
                status=job.status.value,
                error=job.error,
                wall_seconds=round(wall, 2),
                stage_seconds={s: round(t, 2) for s, t in timed.items()},
                files=details.get("files"),
                loc=details.get("loc"),
                checkout_bytes=snapshot.bytes if snapshot else None,
                peak_rss_mb=round(_rss_mb(resource.RUSAGE_SELF), 1),
                peak_child_rss_mb=round(_rss_mb(resource.RUSAGE_CHILDREN), 1),
                stored=stored,
                browse=runtime.run(_browse(runtime, snapshot)) if snapshot else {},
            )
            if not keep:
                runtime.run(_remove(runtime, org_id, repo_id))
            return result
        finally:
            runtime.close()
            set_runtime(None)


def _report(result: Result) -> str:
    mb = 1024 * 1024
    lines = [
        f"{result.repository} @ {(result.commit or '?')[:12]}: {result.status}"
        + (f" ({result.error.get('code')})" if result.error else ""),
        f"  wall {result.wall_seconds:.1f}s  "
        + "  ".join(f"{s} {t:.1f}s" for s, t in result.stage_seconds.items()),
        f"  files {result.files or 0:,}  lines {result.loc or 0:,}  "
        f"checkout {(result.checkout_bytes or 0) / mb:,.1f} MiB",
        f"  peak memory {result.peak_rss_mb:,.0f} MiB "
        f"(git children {result.peak_child_rss_mb:,.0f})",
        "  stored "
        + ", ".join(
            f"{bucket} {usage['objects']} objects {usage['bytes'] / mb:,.1f} MiB"
            for bucket, usage in result.stored.items()
        ),
        "  browse " + "  ".join(f"{key} {value:,}" for key, value in result.browse.items()),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark ingestion of one GitHub repository.")
    parser.add_argument("url", help="https://github.com/<owner>/<name>")
    parser.add_argument("--sha", help="the commit to index (default: the default branch's head)")
    parser.add_argument("--json", type=Path, help="also write the result here")
    parser.add_argument("--keep", action="store_true", help="keep the rows and stored objects")
    args = parser.parse_args(argv)
    result = bench(args.url, args.sha, keep=args.keep)
    print(_report(result))
    if args.json:
        args.json.write_bytes(orjson.dumps(asdict(result), option=orjson.OPT_INDENT_2))
    return 0 if result.status == JobStatus.SUCCEEDED.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
