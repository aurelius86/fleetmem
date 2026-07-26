#!/usr/bin/env bash
# update.sh — update an fleetmem install IN PLACE, preserving your data, config, and PKI.
# Run as root (or with sudo) from your fleetmem directory (e.g. /opt/fleetmem).
#
# What it touches:  code + database schema (via idempotent migrations) + a service restart.
# What it NEVER touches:  your database contents (memories), your brain.env, your pki/ (local CA
# + certs), or agent tokens. Those are on disk / untracked and are left exactly as they are.
set -euo pipefail
[ "$(id -u)" = "0" ] || { echo "run as root (sudo $0)"; exit 1; }

PREFIX="${PREFIX:-$(cd "$(dirname "$0")" && pwd)}"
SVC_USER="${SVC_USER:-brain}"
PGDATABASE="${PGDATABASE:-brain}"
cd "$PREFIX"

echo "== fleetmem update in $PREFIX (user=$SVC_USER, db=$PGDATABASE) =="

# 1. fetch new code (git checkout installs update in place; copy-installs: see UPGRADING.md)
if [ -d "$PREFIX/.git" ]; then
  echo "-- git pull --ff-only --"
  runuser -u "$SVC_USER" -- git -C "$PREFIX" pull --ff-only \
    || echo "NOTE: git pull failed (local changes or network?) — resolve, then re-run."
else
  echo "NOTE: $PREFIX is not a git checkout. Fetch the new release into a staging dir and copy the"
  echo "      code over $PREFIX, KEEPING your brain.env + pki/ — then re-run this. (See UPGRADING.md)"
fi

# 2. apply any NEW migrations — idempotent, only pending ones run; existing data preserved
runuser -u "$SVC_USER" -- env PGDATABASE="$PGDATABASE" python3 "$PREFIX/migrate.py" up

# 3. restart services
systemctl restart brain-api.service 2>/dev/null || echo "NOTE: restart brain-api manually if needed."
systemctl reload nginx 2>/dev/null || true
systemctl try-restart brain-mcp-http.service 2>/dev/null || true

echo "== update done. Your memories, brain.env, and pki/ were preserved. =="
echo "   Check schema state:  python3 $PREFIX/migrate.py status"
