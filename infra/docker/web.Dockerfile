# syntax=docker/dockerfile:1
# The web app (Next.js) for a single server (infra/single-server). Build from the repo root:
#   docker build -f infra/docker/web.Dockerfile --build-arg API_ORIGIN=http://api:8000 -t repo-sensei/web .
# /api/* is forwarded to API_ORIGIN, fixed at build time (Next writes its rewrites then); so is
# WEB_ORIGIN (canonical links, the sitemap, robots.txt).

ARG NODE_IMAGE=node:22.23.2-slim

FROM ${NODE_IMAGE} AS build
ARG API_ORIGIN=http://api:8000
ARG WEB_ORIGIN=http://localhost:3000
ARG TURNSTILE_SITE_KEY=""
ENV NEXT_TELEMETRY_DISABLED=1 \
    SENSEI_API_PROXY_ORIGIN=${API_ORIGIN} \
    SENSEI_WEB_ORIGIN=${WEB_ORIGIN} \
    NEXT_PUBLIC_TURNSTILE_SITE_KEY=${TURNSTILE_SITE_KEY}
RUN corepack enable
WORKDIR /app
# Dependencies first (cached until the lockfile changes), then the app.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc* ./
COPY patches patches
COPY apps/web/package.json apps/web/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter web...
COPY apps/web apps/web
RUN pnpm --filter web build

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000
RUN corepack enable \
    && groupadd --system --gid 10001 sensei \
    && useradd --system --uid 10001 --gid sensei --home-dir /app --shell /usr/sbin/nologin sensei
WORKDIR /app
COPY --from=build --chown=sensei:sensei /app /app
USER sensei
EXPOSE 3000
WORKDIR /app/apps/web
CMD ["sh", "-c", "exec node_modules/.bin/next start --port \"$PORT\" --hostname 0.0.0.0"]
