#!/usr/bin/env bash
# The image's entry point (10.12): Postgres as the official image starts it, and
#
# - with a backup bucket (WALG_S3_PREFIX and WAL-G's credentials, endpoint and region): every
#   WAL segment archived as it fills (at most SENSEI_ARCHIVE_TIMEOUT seconds behind, 60 by
#   default) and a base backup every SENSEI_BACKUP_EVERY_SECONDS (a day), keeping the last
#   SENSEI_BACKUP_KEEP_FULL (7) and the WAL since the oldest: a point-in-time restore window;
# - with SENSEI_RESTORE (LATEST, or a backup's name) and an empty data directory: that backup
#   fetched first, then the archived WAL replayed, up to SENSEI_RESTORE_TARGET_TIME if given
#   (`2026-11-10 13:05:00+00`), then promoted. The backups are read from SENSEI_RESTORE_PREFIX
#   (WALG_S3_PREFIX if not given): restored, a server archives to a prefix of its own, not over
#   the history it came from. A data directory that isn't empty is started as it is (a restore
#   already done): nothing is ever restored over a database.
#
# Without either it is the development image as before. Arguments go to postgres as they are.
set -euo pipefail

export PGDATA="${PGDATA:-/var/lib/postgresql/18/docker}"
# WAL-G reaches the server over the local socket, as the superuser (trusted locally).
export PGHOST="${PGHOST:-/var/run/postgresql}"
export PGUSER="${PGUSER:-${POSTGRES_USER:-postgres}}"
export PGDATABASE="${PGDATABASE:-postgres}"

args=("$@")
say() { echo "sensei-postgres: $*" >&2; }

if [[ -n "${SENSEI_RESTORE:-}" ]]; then
  if [[ -s "$PGDATA/PG_VERSION" ]]; then
    say "SENSEI_RESTORE is set, but $PGDATA holds a database: starting it as it is."
  else
    say "restoring backup ${SENSEI_RESTORE} into $PGDATA"
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"
    from="${SENSEI_RESTORE_PREFIX:-${WALG_S3_PREFIX:-}}"
    if [[ -z "$from" ]]; then
      say "SENSEI_RESTORE needs SENSEI_RESTORE_PREFIX or WALG_S3_PREFIX: where the backups are."
      exit 1
    fi
    WALG_S3_PREFIX="$from" gosu postgres wal-g backup-fetch "$PGDATA" "$SENSEI_RESTORE"
    gosu postgres touch "$PGDATA/recovery.signal"
    args+=(-c "restore_command=WALG_S3_PREFIX='${from}' wal-g wal-fetch %f %p")
    if [[ -n "${SENSEI_RESTORE_TARGET_TIME:-}" ]]; then
      args+=(-c "recovery_target_time=${SENSEI_RESTORE_TARGET_TIME}"
             -c "recovery_target_action=promote")
    fi
  fi
fi

# The newest base backup's age in seconds (none: very old).
backup_age() {
  local newest
  newest="$(gosu postgres wal-g backup-list 2>/dev/null | awk 'NR > 1 { last = $2 } END { print last }')"
  if [[ -z "$newest" ]]; then
    echo 999999999
    return
  fi
  echo $(( $(date +%s) - $(date -d "$newest" +%s) ))
}

backup_loop() {
  local every="${SENSEI_BACKUP_EVERY_SECONDS:-86400}" keep="${SENSEI_BACKUP_KEEP_FULL:-7}"
  # The server itself, not the one the official entry point runs on the socket alone while it
  # first sets the database up (it listens nowhere else).
  until gosu postgres pg_isready -q \
      && [[ -n "$(gosu postgres psql -Atqc 'show listen_addresses' 2>/dev/null)" ]]; do
    sleep 5
  done
  # Not in recovery (a restore still replaying has nothing of its own to back up yet).
  until [[ "$(gosu postgres psql -Atqc 'select pg_is_in_recovery()')" == "f" ]]; do sleep 10; done
  while true; do
    local age
    age="$(backup_age)"
    if (( age >= every )); then
      if gosu postgres wal-g backup-push "$PGDATA"; then
        gosu postgres wal-g delete retain FULL "$keep" --confirm || say "pruning old backups failed"
      else
        say "base backup failed; trying again in an hour"
        sleep 3600
        continue
      fi
      age=0
    fi
    sleep $(( every - age ))
  done
}

if [[ -n "${WALG_S3_PREFIX:-}" ]]; then
  args+=(-c archive_mode=on
         -c "archive_command=wal-g wal-push %p"
         -c "archive_timeout=${SENSEI_ARCHIVE_TIMEOUT:-60}")
  backup_loop &
fi

exec docker-entrypoint.sh postgres "${args[@]}"
