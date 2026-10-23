# syntax=docker/dockerfile:1
# The worker image (Celery). Build from the repo root:
#   docker build -f infra/docker/worker.Dockerfile -t repo-sensei/worker .
# One image serves every queue group; SENSEI_WORKER_QUEUES picks the queues a machine consumes.

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached until the lockfile changes), then our code.
COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/
COPY packages/core/pyproject.toml packages/core/
COPY packages/engine/pyproject.toml packages/engine/
COPY packages/evals/pyproject.toml packages/evals/
COPY packages/testing/pyproject.toml packages/testing/
COPY workers/pyproject.toml workers/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package sensei-workers

COPY packages/core packages/core
COPY packages/engine packages/engine
COPY workers workers
COPY scripts/worker_healthcheck.py scripts/worker_healthcheck.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package sensei-workers

FROM ${PYTHON_IMAGE} AS runtime
# git for cloning (the clone stage); nothing else from the distribution.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 sensei \
    && useradd --system --uid 10001 --gid sensei --home-dir /app --shell /usr/sbin/nologin sensei \
    && mkdir -p /data/repos && chown -R sensei:sensei /data
WORKDIR /app
COPY --from=build --chown=sensei:sensei /app/.venv /app/.venv
COPY --from=build --chown=sensei:sensei /app/scripts/worker_healthcheck.py /app/scripts/worker_healthcheck.py
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SENSEI_WORKER_QUEUES=default,webhooks,ingest,parse,embed,llm,graph,agent,sandbox,maintenance \
    SENSEI_WORKER_CONCURRENCY=2 \
    SENSEI_INGEST_CACHE_DIR=/data/repos \
    TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken
# The tokenizer that sizes chunks, fetched now: workers shouldn't download it at run time.
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
USER sensei
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "/app/scripts/worker_healthcheck.py"]
# Warm shutdown on SIGTERM: running tasks finish; unacknowledged ones return to the queue.
CMD ["sh", "-c", "exec celery -A sensei_workers.app worker --loglevel=INFO --queues=\"$SENSEI_WORKER_QUEUES\" --concurrency=\"$SENSEI_WORKER_CONCURRENCY\" --hostname=\"worker@%h\" --without-gossip --without-mingle"]
