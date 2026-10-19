# Staging on Fly.io + Vercel

The API and Postgres run on Fly.io; the web app runs on Vercel and forwards `/api/*` to the API,
so the browser only ever talks to one origin (cookies stay first-party).

## One-time setup

1. **Postgres** (`postgres.toml`): create the volume, set `POSTGRES_PASSWORD` as a secret, deploy.
   It has no public service; the API reaches it at `repo-sensei-db-staging.internal:5432`.
2. **Redis**: a managed Redis (Upstash or Fly Redis); note its `rediss://` URL.
3. **Object storage**: an S3-compatible bucket pair (snapshots, artifacts), e.g. Cloudflare R2.
4. **GitHub App** for staging (separate from production) with callback
   `https://<web-staging-domain>/api/v1/auth/github/callback`.
5. **API secrets** (`fly secrets set --app repo-sensei-api-staging ...`):
   - `SENSEI_DATABASE_URL` (role `sensei_app`), `SENSEI_DATABASE_MIGRATE_URL` (owner),
     `SENSEI_DATABASE_SYSTEM_URL` (`sensei_system`), `SENSEI_DATABASE_APP_PASSWORD`,
     `SENSEI_DATABASE_SYSTEM_PASSWORD`
   - `SENSEI_REDIS_URL`, `SENSEI_S3_*`
   - `SENSEI_AUTH_TOKEN_ENCRYPTION_KEY` (32 random bytes, base64; the app refuses the dev key here)
   - `SENSEI_WEB_ORIGIN=https://<web-staging-domain>`
   - `SENSEI_GITHUB_APP_ID`, `SENSEI_GITHUB_APP_SLUG`, `SENSEI_GITHUB_CLIENT_ID`,
     `SENSEI_GITHUB_CLIENT_SECRET`, `SENSEI_GITHUB_WEBHOOK_SECRET`, `SENSEI_GITHUB_PRIVATE_KEY_PATH`
   - optional: `SENSEI_SENTRY_DSN`, `SENSEI_OTEL_ENDPOINT`
6. **Deploy the API** (`api.toml`). The release command migrates the database before new machines
   start.
7. **Web on Vercel**: project root `apps/web`; set `SENSEI_API_PROXY_ORIGIN` to the API's URL
   (e.g. `https://repo-sensei-api-staging.fly.dev`). Next's rewrite forwards `/api/*` there.

## After every deploy

```sh
scripts/smoke.sh https://<web-staging-domain>
```
