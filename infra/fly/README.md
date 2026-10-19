# Staging on Fly.io + Vercel

The API and Postgres run on Fly.io; the web app runs on Vercel and forwards `/api/*` to the API,
so the browser only ever talks to one origin (cookies stay first-party).

## One-time setup

1. **Postgres** (`postgres.toml`): create the volume, set `POSTGRES_PASSWORD` as a secret, deploy.
   It has no public service; the API reaches it at `repo-sensei-db-staging.internal:5432`.
2. **Redis**: a managed Redis (Upstash or Fly Redis); note its `rediss://` URL.
3. **Object storage**: an S3-compatible bucket pair (snapshots, artifacts), e.g. Cloudflare R2.
4. **GitHub App** for staging (separate from production):
   - callback URL `https://<web-staging-domain>/api/v1/auth/github/callback`;
   - setup URL `https://<web-staging-domain>/api/v1/github/setup`, "Redirect on update" on;
     leave "Request user authorization (OAuth) during installation" off (it disables the setup
     URL);
   - webhook URL `https://<web-staging-domain>/api/v1/webhooks/github` with the webhook secret;
   - permissions: Contents, Metadata, Pull requests, Issues: read; Email addresses: read;
     Members (organization): read, so linking an org's installation can check the user is an
     admin of that organization.
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
   - `SENSEI_API_CLIENT_IP_HEADER` is already set in `api.toml` (`fly-client-ip`, which Fly's
     proxy sets on every request). Forwarded headers are not trusted.
6. **Deploy the API** (`api.toml`). The release command migrates the database before new machines
   start.
7. **Workers** (`worker.toml`): set the same secrets on `repo-sensei-worker-staging`, deploy, then
   `fly scale count worker=1 beat=1`. Beat must run exactly once (it schedules the webhook sweep).
8. **Web on Vercel**: project root `apps/web`; set `SENSEI_API_PROXY_ORIGIN` to the API's URL
   (e.g. `https://repo-sensei-api-staging.fly.dev`). Next's rewrite forwards `/api/*` there.

## After every deploy

```sh
scripts/smoke.sh https://<web-staging-domain>
```
