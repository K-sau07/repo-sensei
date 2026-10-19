# syntax=docker/dockerfile:1
# The API image. Build from the repo root:
#   docker build -f infra/docker/api.Dockerfile -t repo-sensei/api .

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
    uv sync --frozen --no-dev --no-install-workspace --package sensei-api

COPY apps/api apps/api
COPY packages/core packages/core
COPY scripts/healthcheck.py scripts/healthcheck.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package sensei-api

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 sensei \
    && useradd --system --uid 10001 --gid sensei --home-dir /app --shell /usr/sbin/nologin sensei
WORKDIR /app
# The virtualenv holds the installed packages; alembic.ini and migrations ship for the release step.
COPY --from=build --chown=sensei:sensei /app/.venv /app/.venv
COPY --from=build --chown=sensei:sensei /app/packages/core/alembic.ini /app/packages/core/alembic.ini
COPY --from=build --chown=sensei:sensei /app/packages/core/migrations /app/packages/core/migrations
COPY --from=build --chown=sensei:sensei /app/scripts/healthcheck.py /app/scripts/healthcheck.py
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    SENSEI_API_WORKERS=2
USER sensei
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]
# Forwarded headers are not trusted (any client can send them); the client address comes from the
# header the platform's proxy sets itself (SENSEI_API_CLIENT_IP_HEADER, e.g. fly-client-ip).
CMD ["sh", "-c", "exec uvicorn sensei_api.app:create_app --factory --host 0.0.0.0 --port \"$PORT\" --workers \"$SENSEI_API_WORKERS\" --no-proxy-headers --no-server-header"]
