#!/usr/bin/env bash
# repo-sensei on one server (10.12, $0: an Oracle Cloud Always Free Arm VM, Ubuntu 24.04). As root:
#
#   curl -fsSL https://raw.githubusercontent.com/K-sau07/repo-sensei/main/infra/single-server/setup.sh -o setup.sh
#   REF=main bash setup.sh            # 1. installs everything, writes .env with fresh secrets
#   (fill in .env and add github-app.pem, as it says)
#   REF=main bash setup.sh start      # 2. builds and starts it all
#
# Step 1 is the sandbox host's setup (Docker, gVisor as its runtime, a firewall letting only SSH
# in, the code at REF, the fix stages' worker as a systemd service), then this deployment's .env.
# Step 2 starts the containers (Postgres, Redis, storage, the API, workers, scheduler, the web
# app and the Cloudflare Tunnel), then the fix stages' worker on the host. Both can be run again.
set -euo pipefail

REF="${REF:?the repo-sensei tag, branch or commit to run}"
REPO_URL="${REPO_URL:-https://github.com/K-sau07/repo-sensei.git}"
SRC=/opt/repo-sensei/src
HERE="$SRC/infra/single-server"
ENV="$HERE/.env"
PEM="$HERE/github-app.pem"
say() { printf '\n== %s\n' "$*"; }

install() {
  say "the host: Docker, gVisor, the firewall, the code at $REF, the fix stages' worker"
  if [[ ! -d "$SRC/.git" ]]; then
    apt-get update -qq && apt-get install -y -qq git
    install -d /opt/repo-sensei
    git clone --quiet "$REPO_URL" "$SRC"
  fi
  git -C "$SRC" fetch --quiet --tags origin
  git -C "$SRC" checkout --quiet --detach "$REF"
  REF="$REF" REPO_URL="$REPO_URL" bash "$SRC/infra/sandbox-host/setup.sh"

  if [[ -f "$ENV" ]]; then
    say "$ENV is there already: kept as it is"
  else
    say "writing $ENV with fresh secrets"
    local pg app system key s3_key s3_secret
    pg="$(openssl rand -hex 24)"
    app="$(openssl rand -hex 24)"
    system="$(openssl rand -hex 24)"
    key="$(openssl rand -base64 32)"
    s3_key="$(openssl rand -hex 12)"
    s3_secret="$(openssl rand -hex 24)"
    sed \
      -e "s|^SENSEI_PG_PASSWORD=.*|SENSEI_PG_PASSWORD=$pg|" \
      -e "s|^SENSEI_DATABASE_APP_PASSWORD=.*|SENSEI_DATABASE_APP_PASSWORD=$app|" \
      -e "s|^SENSEI_DATABASE_SYSTEM_PASSWORD=.*|SENSEI_DATABASE_SYSTEM_PASSWORD=$system|" \
      -e "s|^SENSEI_AUTH_TOKEN_ENCRYPTION_KEY=.*|SENSEI_AUTH_TOKEN_ENCRYPTION_KEY=$key|" \
      -e "s|^SENSEI_S3_ACCESS_KEY=.*|SENSEI_S3_ACCESS_KEY=$s3_key|" \
      -e "s|^SENSEI_S3_SECRET_KEY=.*|SENSEI_S3_SECRET_KEY=$s3_secret|" \
      -e "s|APP_PASSWORD@|$app@|" -e "s|PG_PASSWORD@|$pg@|" -e "s|SYSTEM_PASSWORD@|$system@|" \
      "$HERE/.env.example" > "$ENV"
    chmod 600 "$ENV"
  fi
  cat <<EOF

Next:
  1. Fill in $ENV: SENSEI_WEB_ORIGIN, SENSEI_AUTH_ALLOWED_LOGINS, CLOUDFLARE_TUNNEL_TOKEN,
     the GitHub App's values, SENSEI_ANTHROPIC_API_KEY, SENSEI_VOYAGE_API_KEY (and, optional,
     backups, Resend and Sentry).
  2. Save the GitHub App's private key as $PEM.
  3. Run: REF=$REF bash setup.sh start
EOF
}

start() {
  [[ -f "$ENV" ]] || { echo "no $ENV: run 'REF=$REF bash setup.sh' first" >&2; exit 1; }
  [[ -s "$PEM" ]] || { echo "no $PEM: save the GitHub App's private key there" >&2; exit 1; }
  local missing
  missing="$(grep -E '^(SENSEI_WEB_ORIGIN|CLOUDFLARE_TUNNEL_TOKEN|SENSEI_GITHUB_APP_ID|SENSEI_GITHUB_CLIENT_ID|SENSEI_GITHUB_CLIENT_SECRET|SENSEI_ANTHROPIC_API_KEY|SENSEI_VOYAGE_API_KEY)=$' "$ENV" | cut -d= -f1 | tr '\n' ' ' || true)"
  [[ -z "$missing" ]] || { echo "still empty in $ENV: $missing" >&2; exit 1; }
  chmod 640 "$PEM"
  chown root:sensei "$PEM"

  say "building and starting the containers"
  (cd "$HERE" && docker compose up -d --build)

  say "the fix stages' worker, on the host (Postgres, Redis and storage on 127.0.0.1)"
  sed \
    -e "s|@postgres:5432|@127.0.0.1:5432|g" \
    -e "s|redis://redis:6379|redis://127.0.0.1:6379|g" \
    -e "s|http://s3:8333|http://127.0.0.1:8333|g" \
    -e "s|/run/secrets/github-app.pem|$PEM|g" \
    -e "/^CLOUDFLARE_TUNNEL_TOKEN=/d" -e "/^WALG_S3_PREFIX=/d" -e "/^BACKUP_/d" \
    "$ENV" > /etc/repo-sensei/worker.env
  chown root:sensei /etc/repo-sensei/worker.env
  chmod 640 /etc/repo-sensei/worker.env
  systemctl enable --now sensei-sandbox-worker
  systemctl restart sensei-sandbox-worker

  say "checking"
  local origin
  origin="$(grep -E '^SENSEI_WEB_ORIGIN=' "$ENV" | cut -d= -f2-)"
  for _ in $(seq 1 60); do
    if (cd "$HERE" && docker compose exec -T api python /app/scripts/healthcheck.py) >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  (cd "$HERE" && docker compose ps --format 'table {{.Service}}\t{{.Status}}')
  systemctl --no-pager --lines=0 status sensei-sandbox-worker | head -3
  echo
  echo "Open $origin (through the tunnel). Smoke-check: $SRC/scripts/smoke.sh $origin"
}

case "${1:-install}" in
  install) install ;;
  start) start ;;
  *) echo "usage: REF=<ref> bash setup.sh [install|start]" >&2; exit 2 ;;
esac
