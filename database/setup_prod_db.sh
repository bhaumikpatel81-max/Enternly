#!/usr/bin/env bash
# One-time setup for a fresh PostgreSQL server: creates the `oneclickhire`
# database and applies the full schema (this folder's *.sql files, in
# filename order).
#
# Run this ON THE SERVER, as a user that can reach PostgreSQL directly
# (e.g. `sudo -u postgres ./setup_prod_db.sh`, or with DB_HOST=localhost).
# NOTE: `host.docker.internal` from .env.prod only resolves *inside* the
# backend container -- from the server's own shell, PostgreSQL is just
# localhost (or 127.0.0.1) on the configured port.
#
# Usage:
#   DB_PASSWORD='Pg#2026Admin!' ./setup_prod_db.sh
#
# Override any of these if your server differs from .env.prod:
#   DB_HOST (default localhost) DB_PORT (default 2433)
#   DB_NAME (default oneclickhire) DB_USER (default postgres)

set -euo pipefail

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-2433}"
DB_NAME="${DB_NAME:-oneclickhire}"
DB_USER="${DB_USER:-postgres}"
: "${DB_PASSWORD:?Set DB_PASSWORD (see .env.prod) before running this script}"

export PGPASSWORD="$DB_PASSWORD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Target: $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"

EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")

if [ "$EXISTS" = "1" ]; then
  echo "==> Database '$DB_NAME' already exists -- skipping CREATE DATABASE."
else
  echo "==> Creating database '$DB_NAME' ..."
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
    -c "CREATE DATABASE \"${DB_NAME}\" OWNER \"${DB_USER}\";"
fi

echo "==> Applying schema (database/*.sql, in filename order) ..."
for f in $(printf '%s\n' "$SCRIPT_DIR"/*.sql | sort); do
  echo "    -> $(basename "$f")"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> Done. '$DB_NAME' is ready."
