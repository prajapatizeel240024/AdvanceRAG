#!/usr/bin/env bash
# Apply migrations in order, once each.
#
#   scripts/migrate.sh           apply anything not yet applied
#   scripts/migrate.sh --reset   drop and recreate the database first
#   scripts/migrate.sh --status  show what has and has not been applied
#
# Applied migrations are recorded in `schema_migrations` with the sha256 of the
# file. Re-running is therefore a no-op rather than an error, and editing a
# migration that has already been applied is caught instead of silently
# producing two databases that claim the same version with different schemas.
#
# Plain numbered SQL through psql, not Alembic. Most of this schema is things
# Alembic models poorly -- RLS policies, plpgsql functions, generated columns,
# triggers -- so autogeneration would be unusable and every migration would be a
# hand-written op.execute() of the SQL below. That is the same SQL with extra
# indirection, and it obscures the part of this project that matters most.
set -euo pipefail

DB="${DB_NAME:-travel_rag}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-apply}"

if [[ "$MODE" == "--reset" ]]; then
  echo "==> dropping and recreating $DB"
  psql -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS $DB;"
  psql -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE $DB;"
fi

# The ledger has to exist before it can record anything, and it is the one
# object that cannot itself be a tracked migration.
psql -d "$DB" -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename    text PRIMARY KEY,
  sha256      char(64) NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);
SQL

if [[ "$MODE" == "--status" ]]; then
  echo "==> migration status for $DB"
  for f in "$HERE"/migrations/*.sql; do
    name="$(basename "$f")"
    sha="$(shasum -a 256 "$f" | cut -d' ' -f1)"
    rec="$(psql -d "$DB" -tA -c \
      "SELECT sha256 FROM schema_migrations WHERE filename='$name'")"
    if [[ -z "$rec" ]]; then
      printf '  %-38s pending\n' "$name"
    elif [[ "$rec" != "$sha" ]]; then
      printf '  %-38s APPLIED BUT MODIFIED SINCE\n' "$name"
    else
      printf '  %-38s applied\n' "$name"
    fi
  done
  exit 0
fi

echo "==> applying migrations to $DB"
applied=0
for f in "$HERE"/migrations/*.sql; do
  name="$(basename "$f")"
  sha="$(shasum -a 256 "$f" | cut -d' ' -f1)"
  rec="$(psql -d "$DB" -tA -c \
    "SELECT sha256 FROM schema_migrations WHERE filename='$name'")"

  if [[ -n "$rec" ]]; then
    if [[ "$rec" != "$sha" ]]; then
      # Silently re-applying would be worse: the file on disk and the schema in
      # the database have diverged, and only a human knows which is right.
      printf '  %-38s ERROR: modified after being applied\n' "$name"
      echo "      Roll the change forward as a new migration, or use --reset." >&2
      exit 1
    fi
    printf '  %-38s skip (applied)\n' "$name"
    continue
  fi

  printf '  %-38s' "$name"
  if out=$(psql -d "$DB" -v ON_ERROR_STOP=1 -q -f "$f" 2>&1); then
    psql -d "$DB" -v ON_ERROR_STOP=1 -q -c \
      "INSERT INTO schema_migrations (filename, sha256) VALUES ('$name', '$sha')"
    echo "ok"
    applied=$((applied + 1))
  else
    echo "FAILED"
    echo "$out" | sed 's/^/      /'
    exit 1
  fi
done

echo "==> $applied applied, schema summary"
psql -d "$DB" -tA <<'SQL'
SELECT '  tables:    ' || count(*) FROM pg_tables  WHERE schemaname='public';
SELECT '  views:     ' || count(*) FROM pg_views   WHERE schemaname='public';
SELECT '  functions: ' || count(*) FROM pg_proc p
  JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='public' AND p.proname LIKE 'fn_%';
SELECT '  policies:  ' || count(*) FROM pg_policies WHERE schemaname='public';
SQL
