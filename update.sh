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

# 2. dependencies — fleetmem runs from a venv ($PREFIX/venv). Build it if missing (this covers the
#    old system-python layout -> venv re-platform), then install requirements. Fail loudly on a
#    missing venv module (ensurepip) instead of limping on with a broken interpreter.
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  echo "-- no venv at $PREFIX/venv — creating one (system-python -> venv re-platform) --"
  command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found."; exit 1; }
  python3 -m venv "$PREFIX/venv" \
    || { echo "ERROR: python3 'venv' module missing — install it (Debian: apt-get install python3-venv), then re-run."; exit 1; }
  chown -R "$SVC_USER:$SVC_USER" "$PREFIX/venv"
  NEW_VENV=1
fi
PY="$PREFIX/venv/bin/python"
runuser -u "$SVC_USER" -- "$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
[ -f "$PREFIX/requirements.txt" ] \
  && runuser -u "$SVC_USER" -- "$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check -r "$PREFIX/requirements.txt"

# 2b. if we just built the venv, rewire the systemd units from system-python/bare-gunicorn to the
#     venv interpreter (the pre-venv ExecStart pointed at /usr/bin/python3 or /usr/bin/gunicorn).
if [ "${NEW_VENV:-0}" = 1 ]; then
  for unit in /etc/systemd/system/brain-api.service /etc/systemd/system/brain-mcp-http.service; do
    [ -f "$unit" ] || continue
    if grep -qE 'ExecStart=(/usr/bin/python3?|/usr/bin/gunicorn)' "$unit"; then
      echo "-- rewiring $(basename "$unit") ExecStart -> $PREFIX/venv --"
      sed -i -E "s#ExecStart=/usr/bin/python3?#ExecStart=$PREFIX/venv/bin/python#; s#ExecStart=/usr/bin/gunicorn#ExecStart=$PREFIX/venv/bin/gunicorn#" "$unit"
      RELOAD=1
    fi
  done
  [ "${RELOAD:-0}" = 1 ] && systemctl daemon-reload
fi

# 3. apply any NEW migrations — idempotent, only pending ones run; existing data preserved
runuser -u "$SVC_USER" -- env PGDATABASE="$PGDATABASE" "$PY" "$PREFIX/migrate.py" up

# 4. restart services
systemctl restart brain-api.service 2>/dev/null || echo "NOTE: restart brain-api manually if needed."
systemctl reload nginx 2>/dev/null || true
systemctl try-restart brain-mcp-http.service 2>/dev/null || true

echo "== update done. Your memories, brain.env, and pki/ were preserved. =="
echo "   Check schema state:  python3 $PREFIX/migrate.py status"
