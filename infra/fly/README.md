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
   - `SENSEI_WEB_ORIGIN`: the site's own URL (canonical links, social images, the sitemap). Set it
     for the build too: robots.txt is written then, and names the sitemap by it.
   - The public gallery (10.2): `SENSEI_EXPLORE_ORG` (and `SENSEI_EXPLORE_TURNSTILE_SECRET`) on
     the API, `NEXT_PUBLIC_TURNSTILE_SITE_KEY` on the web (no key: its Ask bar says questions
     aren't open). Through Vercel's rewrite the API sees Vercel's address, not the visitor's, so
     the per-address question limits are shared by everyone until `/api/*` reaches the API
     directly (the Turnstile check and the explore org's daily cap still hold).

## After every deploy

```sh
scripts/smoke.sh https://<web-staging-domain>
```

## Production

The same files, under the production apps' names and environment: pass `--app` and
`-e SENSEI_ENV=production` to every deploy, and set the production secrets (its own GitHub App,
database, Redis, buckets and keys; never staging's).

| What          | App                  | Deploy                                                                                                                                  |
| ------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Postgres      | `repo-sensei-db`     | `fly deploy infra/postgres --config infra/fly/postgres.toml --app repo-sensei-db`                                                       |
| API           | `repo-sensei-api`    | `fly deploy . --config infra/fly/api.toml --app repo-sensei-api -e SENSEI_ENV=production`                                               |
| Workers, beat | `repo-sensei-worker` | `fly deploy . --config infra/fly/worker.toml --app repo-sensei-worker -e SENSEI_ENV=production`, then `fly scale count worker=1 beat=1` |
| Fix stages    | the sandbox host     | `infra/sandbox-host/setup.sh` on a dedicated VM (Docker + gVisor), the `agent` and `sandbox` queues                                     |
| Web           | Vercel               | project root `apps/web`, `SENSEI_API_PROXY_ORIGIN=https://repo-sensei-api.fly.dev`                                                      |

### Backups and restores

Postgres archives its WAL to the backup bucket and takes a base backup a day (`postgres.toml`;
secrets `WALG_S3_PREFIX`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT`). Check
after the first deploy: `fly ssh console --app repo-sensei-db -C "gosu postgres wal-g backup-list"`
lists a backup within a few minutes, and `select * from pg_stat_archiver` shows no failures.

To restore to a moment (a bad migration, a deletion), into a **new** app, never over the
running one:

1. `fly apps create repo-sensei-db-restore` and a volume `pgdata` in the same region;
2. its secrets: the same bucket credentials, `SENSEI_RESTORE=LATEST` (or a backup's name),
   `SENSEI_RESTORE_PREFIX` = the old `WALG_S3_PREFIX`, `SENSEI_RESTORE_TARGET_TIME` (UTC, e.g.
   `2026-11-10 13:05:00+00`), and a new `WALG_S3_PREFIX` for the restored server's own history;
3. deploy it with `postgres.toml` and `--app repo-sensei-db-restore`; it fetches the backup,
   replays the WAL to the target and promotes itself;
4. check the data, then point the API's and workers' database URLs at it (or copy what was lost
   back), and remove `SENSEI_RESTORE` from its secrets.

`scripts/restore-drill.sh` runs the whole thing locally against the dev stack (a backup, rows
before and after a moment, a restore to it) and passes only when the restored server has exactly
the rows from before. Run it after changing the Postgres image.
