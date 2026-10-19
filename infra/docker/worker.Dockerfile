# syntax=docker/dockerfile:1
# Placeholder: the indexing worker arrives in P1 (same build pattern as api.Dockerfile).

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie
FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 sensei \
    && useradd --system --uid 10001 --gid sensei --shell /usr/sbin/nologin sensei
USER sensei
CMD ["python", "-c", "print('repo-sensei worker: arrives in P1'); import time; time.sleep(3600)"]
