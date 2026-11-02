# syntax=docker/dockerfile:1
# The sandbox's Python image: where a repository's own code runs (route introspection, later its
# tests). It holds Python, uv and an empty virtualenv; a repository's dependencies go into an
# image built on top of this one (sensei_engine.sandbox.deps), never into this one. No secrets,
# and runs get no network (the runner passes --network none). Build one per Python version:
#   make sandbox-image
#   docker build -f sandbox/images/python-runner.Dockerfile --build-arg PYTHON_VERSION=3.12 \
#     -t repo-sensei/python-runner:3.12 sandbox/images

ARG PYTHON_VERSION=3.13
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1

FROM ${UV_IMAGE} AS uv

FROM python:${PYTHON_VERSION}-slim-trixie
COPY --from=uv /uv /uvx /bin/
# The virtualenv belongs to the installer (10002), who fills it when a dependencies image is made
# (never root): a run, as the sandbox user (10001), can import from it, not change it.
RUN groupadd --system --gid 10001 sandbox \
    && useradd --system --uid 10001 --gid sandbox --home-dir /work --shell /usr/sbin/nologin sandbox \
    && groupadd --system --gid 10002 installer \
    && useradd --system --uid 10002 --gid installer --home-dir /tmp --shell /usr/sbin/nologin installer \
    && uv venv --python /usr/local/bin/python /opt/venv \
    && chown -R installer:installer /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    HOME=/work \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1
WORKDIR /src
USER sandbox
