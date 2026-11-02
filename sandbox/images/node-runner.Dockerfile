# syntax=docker/dockerfile:1
# The sandbox's Node image (8.2): where a JavaScript repository's tests run. Node, npm and the
# launcher; a repository's node_modules go into an image built on top of this one
# (sensei_engine.sandbox.node), never into this one. No secrets; runs get no network.
#   make sandbox-image
#   docker build -f sandbox/images/node-runner.Dockerfile -t repo-sensei/node-runner:22 sandbox/images

ARG NODE_VERSION=22

FROM node:${NODE_VERSION}-trixie-slim
# The image's own "node" user takes uid 1000; the sandbox user is 10001 like the Python image's.
RUN groupadd --system --gid 10001 sandbox \
    && useradd --system --uid 10001 --gid sandbox --home-dir /work --shell /usr/sbin/nologin sandbox \
    && mkdir -p /opt/deps
COPY --chmod=0755 sensei-node-run /usr/local/bin/sensei-node-run
ENV HOME=/work \
    npm_config_cache=/tmp/.npm \
    npm_config_update_notifier=false \
    NEXT_TELEMETRY_DISABLED=1 \
    CI=1
WORKDIR /src
USER sandbox
