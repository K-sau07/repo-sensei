#!/usr/bin/env bash
# The restore drill (10.12), against the local stack (`make up`): our Postgres image archiving to
# the local S3 with WAL-G, a base backup, rows written before and after a moment T, then a new
# server restored from the backups to T. It passes when the restored server has every row from
# before T and none from after, its extensions, and takes writes (promoted). It touches only its
# own containers, volumes and bucket prefix, and removes them after.
#
#   scripts/restore-drill.sh            # builds repo-sensei/postgres:drill from infra/postgres
#   IMAGE=… NET=… scripts/restore-drill.sh
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-repo-sensei/postgres:drill}"
NET="${NET:-repo-sensei_default}"  # the compose network, where S3 is http://s3:8333
BUCKET="${BUCKET:-sensei-backups-drill}"
RUN="drill-$(date +%s)"
PRIMARY="sensei-drill-primary" RESTORED="sensei-drill-restored"

# The local S3's credentials (the compose defaults unless .env sets them), never printed.
env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
export AWS_ACCESS_KEY_ID="${SENSEI_S3_ACCESS_KEY:-$(env_value SENSEI_S3_ACCESS_KEY)}"
export AWS_SECRET_ACCESS_KEY="${SENSEI_S3_SECRET_KEY:-$(env_value SENSEI_S3_SECRET_KEY)}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-sensei}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-sensei-dev-secret}"
S3=(-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_REGION=us-east-1
    -e AWS_ENDPOINT=http://s3:8333 -e AWS_S3_FORCE_PATH_STYLE=true)

say() { printf '\n== %s\n' "$*"; }
psql_in() { docker exec "$1" gosu postgres psql -X -Atq -U sensei -d sensei -c "$2"; }
cleanup() {
  docker rm -f "$PRIMARY" "$RESTORED" >/dev/null 2>&1 || true
  docker volume rm sensei-drill-a sensei-drill-b >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

ready() {  # the server itself is up (not the entry point's first-setup one), out of recovery
  local name="$1" waited=0
  until [[ "$(psql_in "$name" "select current_setting('listen_addresses') <> '' and not pg_is_in_recovery()" 2>/dev/null)" == "t" ]]; do
    sleep 1
    waited=$((waited + 1))
    if (( waited > 300 )); then
      docker logs --tail 40 "$name" >&2
      echo "$name didn't come up" >&2
      exit 1
    fi
  done
}

if [[ "$IMAGE" == "repo-sensei/postgres:drill" ]]; then
  say "building $IMAGE"
  docker build -q -t "$IMAGE" infra/postgres >/dev/null
fi

say "bucket s3://$BUCKET, prefix $RUN"
docker run --rm --network "$NET" -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION=us-east-1 amazon/aws-cli:2.38.1 --endpoint-url http://s3:8333 \
  s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
  || docker run --rm --network "$NET" -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
       -e AWS_DEFAULT_REGION=us-east-1 amazon/aws-cli:2.38.1 --endpoint-url http://s3:8333 \
       s3 mb "s3://$BUCKET" >/dev/null

say "primary: archiving to s3://$BUCKET/$RUN/primary"
docker run -d --name "$PRIMARY" --network "$NET" -v sensei-drill-a:/var/lib/postgresql \
  -e POSTGRES_USER=sensei -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=sensei \
  -e SENSEI_ARCHIVE_TIMEOUT=5 -e "WALG_S3_PREFIX=s3://$BUCKET/$RUN/primary" "${S3[@]}" \
  "$IMAGE" >/dev/null
ready "$PRIMARY"
psql_in "$PRIMARY" "create table drill (n int primary key, at timestamptz not null default clock_timestamp())"
psql_in "$PRIMARY" "insert into drill (n) select generate_series(1, 100)"

say "waiting for the first base backup"
for _ in $(seq 1 120); do
  if docker exec "$PRIMARY" gosu postgres wal-g backup-list 2>/dev/null | grep -q base_; then break; fi
  sleep 2
done
docker exec "$PRIMARY" gosu postgres wal-g backup-list 2>/dev/null | grep -q base_ \
  || { docker logs --tail 40 "$PRIMARY" >&2; echo "no base backup" >&2; exit 1; }

say "rows 101-200, then T, then rows 201-300"
psql_in "$PRIMARY" "insert into drill (n) select generate_series(101, 200)"
psql_in "$PRIMARY" "select pg_switch_wal()" >/dev/null
sleep 2
T="$(psql_in "$PRIMARY" "select to_char(clock_timestamp() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")"
sleep 2
psql_in "$PRIMARY" "insert into drill (n) select generate_series(201, 300)"
psql_in "$PRIMARY" "select pg_switch_wal()" >/dev/null
for _ in $(seq 1 60); do  # until the segment holding rows 201-300 is archived
  if [[ "$(psql_in "$PRIMARY" "select archived_count >= 3 and failed_count = 0 from pg_stat_archiver")" == "t" ]]; then break; fi
  sleep 1
done
psql_in "$PRIMARY" "select 'archived ' || archived_count || ', failed ' || failed_count from pg_stat_archiver"
echo "T = $T"

say "restoring to T into a new server (archiving to its own prefix)"
started=$(date +%s)
docker run -d --name "$RESTORED" --network "$NET" -v sensei-drill-b:/var/lib/postgresql \
  -e POSTGRES_USER=sensei -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=sensei \
  -e SENSEI_RESTORE=LATEST -e "SENSEI_RESTORE_TARGET_TIME=$T" \
  -e "SENSEI_RESTORE_PREFIX=s3://$BUCKET/$RUN/primary" \
  -e "WALG_S3_PREFIX=s3://$BUCKET/$RUN/restored" "${S3[@]}" \
  "$IMAGE" >/dev/null
ready "$RESTORED"
took=$(( $(date +%s) - started ))

say "checking"
rows="$(psql_in "$RESTORED" "select count(*) || ' rows, max ' || max(n) from drill")"
extensions="$(psql_in "$RESTORED" "select string_agg(extname, ',' order by extname) from pg_extension where extname in ('vector', 'pg_textsearch', 'pg_trgm')")"
psql_in "$RESTORED" "insert into drill (n) values (1000)"
echo "restored: $rows; extensions: $extensions; took ${took}s; takes writes"
[[ "$rows" == "200 rows, max 200" ]] || { echo "FAIL: expected rows 1-200 only" >&2; exit 1; }
[[ "$extensions" == "pg_textsearch,pg_trgm,vector" ]] || { echo "FAIL: extensions" >&2; exit 1; }
echo "PASS: restored to T, nothing after it"
