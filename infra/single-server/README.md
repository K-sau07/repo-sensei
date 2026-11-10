# repo-sensei on one server, for $0

Everything on one machine: an Oracle Cloud Always Free Arm VM (2 OCPUs, 12 GB RAM, 200 GB disk) or
any Ubuntu 24.04 box with 4 GB of RAM or more. The only way in is a Cloudflare Tunnel: no port is
open to the internet but SSH. Every feature works, Fix included: a repository's tests run in gVisor
on the same machine.

| Piece                                              | Where                                                                          |
| -------------------------------------------------- | ------------------------------------------------------------------------------ |
| Web app, API, workers, scheduler                   | containers (`docker-compose.yml`)                                              |
| Postgres (pgvector, BM25), Redis, snapshot storage | containers, on this disk, reachable from this machine only                     |
| The fix stages (agent, sandbox)                    | a systemd service on the host, beside Docker and gVisor (`infra/sandbox-host`) |
| HTTPS and your domain                              | Cloudflare Tunnel (`cloudflared`)                                              |
| Backups (optional)                                 | WAL-G to a Cloudflare R2 bucket: a base backup a day, the WAL as it is written |

## What you need

- A domain on Cloudflare (free plan).
- The server: Ubuntu 24.04, and SSH to it as a user who can `sudo`.
- A GitHub App (see `.env.example` for its URLs and permissions) and its private key (`.pem`).
- Anthropic and Voyage API keys.
- Optional: an R2 bucket for backups; Resend (email) and Sentry (errors), both free tiers.

## Steps

1. **A tunnel.** Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel
   (Cloudflared). Copy its token. Add a public hostname, `sensei.<your domain>`, with the service
   `http://web:3000`.
2. **Install** (on the server, as root):

   ```sh
   curl -fsSL https://raw.githubusercontent.com/K-sau07/repo-sensei/main/infra/single-server/setup.sh -o setup.sh
   REF=main bash setup.sh
   ```

   This installs Docker and gVisor, sets the firewall (only SSH in), checks out the code in
   `/opt/repo-sensei/src`, builds the sandbox's images, and writes
   `/opt/repo-sensei/src/infra/single-server/.env` with fresh passwords and keys.

3. **Fill in** that `.env`:
   - `SENSEI_WEB_ORIGIN` (`https://sensei.<your domain>`), `SENSEI_AUTH_ALLOWED_LOGINS` (your GitHub
     login: only you can sign in), `CLOUDFLARE_TUNNEL_TOKEN`;
   - the GitHub App's values, `SENSEI_ANTHROPIC_API_KEY`, `SENSEI_VOYAGE_API_KEY`;
   - optional: the backup bucket, Resend, Sentry.

   Save the GitHub App's private key as `infra/single-server/github-app.pem` next to it.

4. **Start:**

   ```sh
   REF=main bash setup.sh start
   ```

   It builds the images (the first time takes a while on 2 CPUs), starts the containers, runs the
   migrations, and starts the fix stages' worker. Then open your site.

## Updating

```sh
cd /opt/repo-sensei/src && git fetch && git checkout --detach <new ref>
cd infra/single-server && docker compose up -d --build
systemctl restart sensei-sandbox-worker
```

Migrations run on their own (`migrate` runs before the API starts).

## Backups and restores

With `WALG_S3_PREFIX` and the bucket's keys set, Postgres archives its WAL within a minute and
takes a base backup a day, keeping 7. Check it: `docker compose exec postgres gosu postgres wal-g
backup-list`. To restore to a moment, see `infra/fly/README.md` ("Backups and restores"): the same
image and settings (`SENSEI_RESTORE`, `SENSEI_RESTORE_PREFIX`, `SENSEI_RESTORE_TARGET_TIME`), on an
empty `pgdata` volume. `scripts/restore-drill.sh` shows the whole thing working locally.

## Limits to know

- An idle Always Free VM can be reclaimed by Oracle; upgrading the account to Pay As You Go
  (staying within the free limits) is reported to prevent it. Keep backups on either way.
- One machine: no failover. Indexing a large repository takes a while on 2 CPUs.
- Model spend is capped in `.env` (`SENSEI_LIMITS_GLOBAL_DAILY_SPEND_CAP_USD`): past it, the
  service is read-only until midnight UTC.
