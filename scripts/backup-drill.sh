#!/usr/bin/env bash
# Backup restore drill (`make backup-drill`): dump the local development database the way the
# nightly backup does, restore the dump into a new, throwaway database, and check that every table
# came back with the same rows. The throwaway database and the dump are deleted afterwards, whether
# the drill passes or not.
#
#   scripts/backup-drill.sh [database]    (default: the Compose postgres container's POSTGRES_DB)
#
# Everything runs in the Compose stack's postgres container (`make up`), with its own pg_dump,
# pg_restore, and psql, so the tools always match the server, as in the production CronJob
# (deploy/k8s/base/postgres-backup.yaml): `pg_dump --format=custom`, restored with pg_restore.
# The source database is only read. The throwaway database is created from template0 under a new
# name, and it is the only database the drill writes to or drops.
#
# The dump and the source's side of the comparison share one snapshot (pg_export_snapshot), so the
# check is exact even while the pipeline keeps writing: for every table, the row count and an MD5
# of all its rows (as text, sorted) must match. See docs/runbook.md, "Backups and restores".
set -euo pipefail

project=${COMPOSE_PROJECT_NAME:-sec-filings}
container=$(docker ps --format '{{.Names}}' \
  --filter "label=com.docker.compose.project=$project" \
  --filter label=com.docker.compose.service=postgres)
if [[ -z $container ]]; then
  echo "the Compose postgres container isn't running: start the stack with \`make up\`" >&2
  exit 1
fi
user=$(docker exec "$container" printenv POSTGRES_USER)
source_db=${1:-$(docker exec "$container" printenv POSTGRES_DB)}
drill_db="restore_drill_$(date -u +%Y%m%d_%H%M%S)_$$"
if [[ $drill_db == "$source_db" ]]; then
  echo "refusing to restore over the source database" >&2
  exit 1
fi

# For every table: "schema.table|rows|md5 of its rows". \gexec runs each generated query in turn.
CHECK_SQL=$(
  cat <<'SQL'
SELECT format(
         'SELECT %L, count(*), md5(coalesce(string_agg(t::text, %L ORDER BY t::text COLLATE "C"), %L)) FROM %I.%I AS t',
         n.nspname || '.' || c.relname, E'\n', '', n.nspname, c.relname)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
ORDER BY n.nspname, c.relname
\gexec
SQL
)
END_MARK="-- end of output $$ --"
TIMEOUT=600 # seconds to wait for one query's output in the snapshot session

# run_psql DATABASE: run the SQL on stdin in DATABASE, printing rows unaligned and nothing else.
run_psql() {
  docker exec -i "$container" psql -X -q -At -v ON_ERROR_STOP=1 -U "$user" -d "$1"
}

work=$(mktemp -d)
created=false
snapshot_open=false
cleanup() {
  if $snapshot_open; then
    exec {to_snapshot}>&- # psql stops at the end of its input, rolling the transaction back
  fi
  if $created; then
    if echo "DROP DATABASE IF EXISTS \"$drill_db\" WITH (FORCE);" | run_psql postgres; then
      echo "Dropped $drill_db."
    else
      echo "could not drop $drill_db: drop it by hand" >&2
    fi
  fi
  if [[ -f $work/drill.dump ]]; then
    echo "Deleted the dump."
  fi
  rm -rf "$work"
}
trap cleanup EXIT

# A psql session that holds the snapshot open while pg_dump uses it. Its output is read up to a
# marker after each query, so a failed query (psql stops at the first error) can't hang the drill.
# The coprocess's pipes move to descriptors of our own: bash unsets SNAPSHOT when it ends.
coproc SNAPSHOT { run_psql "$source_db"; }
session_pid=$SNAPSHOT_PID
exec {to_snapshot}>&"${SNAPSHOT[1]}" {from_snapshot}<&"${SNAPSHOT[0]}"
coproc_in=${SNAPSHOT[1]} coproc_out=${SNAPSHOT[0]}
exec {coproc_in}>&- {coproc_out}<&-
snapshot_open=true

# in_snapshot SQL: run SQL in the snapshot session and print what it returns. The write happens in
# a subshell: if the session has already ended, SIGPIPE ends the subshell rather than the drill,
# and the read below reports it.
in_snapshot() {
  local line
  (printf '%s\n\\echo %s\n' "$1" "$END_MARK" >&"$to_snapshot") 2>/dev/null || true
  while IFS= read -r -t "$TIMEOUT" line <&"$from_snapshot"; do
    if [[ $line == "$END_MARK" ]]; then
      return 0
    fi
    printf '%s\n' "$line"
  done
  echo "the session reading \"$source_db\" stopped early (see the error above)" >&2
  return 1
}

echo "Restore drill: \"$source_db\" in container $container"
in_snapshot 'BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;'
in_snapshot 'SELECT pg_export_snapshot();' >"$work/snapshot"
snapshot=$(<"$work/snapshot")
in_snapshot 'SELECT pg_size_pretty(pg_database_size(current_database()));' >"$work/db-size"

started=$SECONDS
docker exec "$container" pg_dump --username="$user" --dbname="$source_db" \
  --snapshot="$snapshot" --format=custom >"$work/drill.dump"
size=$(du -h "$work/drill.dump" | cut -f1)
echo "  dumped in $((SECONDS - started)) s: $size from a $(<"$work/db-size") database" \
  "(pg_dump --format=custom, snapshot $snapshot)"

in_snapshot "$CHECK_SQL" >"$work/source.txt"
in_snapshot 'ROLLBACK;'
exec {to_snapshot}>&-
snapshot_open=false
wait "$session_pid"

echo "CREATE DATABASE \"$drill_db\" TEMPLATE template0;" | run_psql postgres
created=true
started=$SECONDS
docker exec -i "$container" pg_restore --username="$user" --dbname="$drill_db" \
  --exit-on-error --single-transaction <"$work/drill.dump"
echo "  restored into $drill_db in $((SECONDS - started)) s"
run_psql "$drill_db" <<<"$CHECK_SQL" >"$work/restored.txt"

echo
awk -F'|' '
  NR == FNR { source[$1] = $2 "|" $3; next }
  {
    status = ($1 in source && source[$1] == $2 "|" $3) ? "same" : "DIFFERENT"
    printf "  %-28s %9s  %s\n", $1, $2, status
    tables++; rows += $2
  }
  END { printf "  %-28s %9s\n", tables " tables", rows }
' "$work/source.txt" "$work/restored.txt"
echo
if ! diff -u --label "$source_db (snapshot)" --label "$drill_db" \
  "$work/source.txt" "$work/restored.txt" >"$work/diff.txt"; then
  echo "FAILED: the restored database differs from the snapshot that was dumped:" >&2
  cat "$work/diff.txt" >&2
  exit 1
fi
echo "PASSED: every table's row count and checksum match the snapshot that was dumped."
