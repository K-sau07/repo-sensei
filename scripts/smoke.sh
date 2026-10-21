#!/usr/bin/env bash
# Smoke-check a deployed environment through the web origin (which forwards /api/* to the API).
#   scripts/smoke.sh https://staging.example.com
set -euo pipefail

BASE="${1:?usage: scripts/smoke.sh <base-url>}"
BASE="${BASE%/}"
failures=0

pass() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; failures=$((failures + 1)); }

check() {
  local name="$1"
  shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}

status_of() { curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$@"; }

echo "Smoke checks for $BASE"

check "API is alive (/api/v1/healthz = 200)" \
  test "$(status_of "$BASE/api/v1/healthz")" = 200

ready_body="$(curl -sS --max-time 10 "$BASE/api/v1/readyz" || true)"
check "API dependencies are ready (/api/v1/readyz)" \
  grep -q '"status":"ok"' <<<"$ready_body"

login_headers="$(curl -sS -D - -o /dev/null --max-time 10 "$BASE/api/v1/auth/github/login?next=%2F" || true)"
check "sign-in starts (302 from /api/v1/auth/github/login)" \
  grep -qiE '^HTTP/[0-9.]+ 302' <<<"$login_headers"
check "sign-in goes to GitHub (not 'not configured')" \
  grep -qiE '^location: https://github\.com/login/oauth/authorize' <<<"$login_headers"

page_headers="$(curl -sS -D - -o /dev/null --max-time 10 "$BASE/login")"
check "the login page renders (200)" grep -qiE '^HTTP/[0-9.]+ 200' <<<"$page_headers"
check "strict script policy (nonce + strict-dynamic)" \
  grep -qiE "^content-security-policy:.*script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'" <<<"$page_headers"
check "no inline scripts allowed" \
  bash -c "! grep -qiE \"^content-security-policy:.*script-src[^;]*'unsafe-inline'\" <<<\"\$1\"" _ "$page_headers"
check "HSTS is on" grep -qiE '^strict-transport-security: max-age=' <<<"$page_headers"
check "pages can't be framed" grep -qiE "^x-frame-options: DENY" <<<"$page_headers"

check "signed-out pages redirect to sign-in" \
  test "$(status_of "$BASE/settings/profile")" = 307

if ((failures > 0)); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "All checks passed"
