#!/usr/bin/env bash
# install.sh — stand up fleetmem on a fresh Debian/Ubuntu host (bare-metal, systemd + nginx + local
# Postgres). Run as root (or with sudo). Idempotent-ish: safe to re-run; skips what already exists.
#
# What it does (all local, nothing phones home):
#   1. apt: PostgreSQL + pgvector, Python, nginx, openssl
#   2. create the service OS user + PG role (owner) + database + the `vector` extension
#   3. deploy this dist to $PREFIX, install Python deps
#   4. run migrations, mint the local CA + server cert, apply RLS grants
#   5. install + start systemd units and the nginx mTLS front
#   6. tell you how to create your first (genesis) manager
#
# Config via env (all optional except BRAIN_HOST for a real TLS SAN):
#   PREFIX=/opt/fleetmem  SVC_USER=brain  PGDATABASE=brain  BRAIN_HOST=$(hostname -f)  BRAIN_IP=
set -euo pipefail
[ "$(id -u)" = "0" ] || { echo "run as root (sudo $0)"; exit 1; }

# Force UTF-8 so DB writes with non-ASCII text work even on a C/POSIX-locale host.
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PGCLIENTENCODING=UTF8

PREFIX="${PREFIX:-/opt/fleetmem}"
SVC_USER="${SVC_USER:-brain}"
PGDATABASE="${PGDATABASE:-brain}"
BRAIN_HOST="${BRAIN_HOST:-$(hostname -f 2>/dev/null || hostname)}"
BRAIN_IP="${BRAIN_IP:-}"
PG_VER="${PG_VER:-16}"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "== fleetmem install → $PREFIX (host=$BRAIN_HOST, db=$PGDATABASE, user=$SVC_USER) =="

# 1. packages ----------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y curl ca-certificates gnupg >/dev/null
# PostgreSQL APT (PGDG) repo — base Debian/Ubuntu repos lack PG16 + pgvector.
install -d /usr/share/postgresql-common/pgdg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
     -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
. /etc/os-release
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
     > /etc/apt/sources.list.d/pgdg.list
apt-get update -qq
# Python app deps are NOT taken from apt (distro versions lag the pins + have no `mcp`). apt provides
# only the interpreter + venv builder; the pinned deps go into $PREFIX/venv (section 3b) via pip.
apt-get install -y "postgresql-${PG_VER}" "postgresql-${PG_VER}-pgvector" \
                   python3 python3-venv nginx openssl >/dev/null

# 2. service user + PG role + database + extension ---------------------------
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"
runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$SVC_USER'" | grep -q 1 \
  || runuser -u postgres -- createuser "$SVC_USER"
runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='$PGDATABASE'" | grep -q 1 \
  || runuser -u postgres -- createdb -O "$SVC_USER" --encoding=UTF8 --template=template0 \
       --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8 "$PGDATABASE"   # explicit UTF8: on a C-locale host the default would be a SQL_ASCII DB that corrupts non-ASCII text
runuser -u postgres -- psql -d "$PGDATABASE" -c "CREATE EXTENSION IF NOT EXISTS vector"

# 3. deploy code + config -----------------------------------------------------
mkdir -p "$PREFIX"
cp -a "$SRC/." "$PREFIX/"
[ -f "$PREFIX/brain.env" ] || cp "$PREFIX/brain.env.example" "$PREFIX/brain.env"
chown -R "$SVC_USER:$SVC_USER" "$PREFIX"

# 3b. python venv with the PINNED deps ---------------------------------------
# One isolated venv drives every fleetmem process (API, MCP, migrate, hygiene timers). This is why
# the requirements.txt pins are real on any distro — not shadowed by whatever apt happens to ship.
VPY="$PREFIX/venv/bin/python"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"
chown -R "$SVC_USER:$SVC_USER" "$PREFIX/venv"

# 4. migrations + PKI + RLS grants -------------------------------------------
runuser -u "$SVC_USER" -- env PGDATABASE="$PGDATABASE" "$VPY" "$PREFIX/migrate.py" up
runuser -u "$SVC_USER" -- env PKI_DIR="$PREFIX/pki" BRAIN_HOST="$BRAIN_HOST" BRAIN_IP="$BRAIN_IP" \
     bash "$PREFIX/fleetmem-init-pki.sh"
runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='brain_app'" | grep -q 1 \
  || runuser -u postgres -- psql -c "CREATE ROLE brain_app LOGIN"
runuser -u postgres -- psql -c "ALTER ROLE brain_app LOGIN"   # must be LOGIN so the app can connect AS it
runuser -u postgres -- psql -d "$PGDATABASE" -f "$PREFIX/deploy/grants-brain_app.sql" || \
  echo "NOTE: review deploy/grants-brain_app.sql — the app role (brain_app) + grants set up RLS."

# Let the service OS user ($SVC_USER) authenticate to Postgres AS brain_app over the local socket.
# The RLS drop-in makes brain-api connect as brain_app; local peer auth requires OS-user==role-name,
# and they differ ($SVC_USER vs brain_app), so add a pg_ident map + an hba line ABOVE the generic
# peer rule. WITHOUT this the API cannot connect at all — every request 500s (peer-auth FATAL).
HBA="$(runuser -u postgres -- psql -tAc 'SHOW hba_file')"
IDENT="$(runuser -u postgres -- psql -tAc 'SHOW ident_file')"
if ! grep -qE "^fleetmemmap[[:space:]]+$SVC_USER[[:space:]]+brain_app" "$IDENT" 2>/dev/null; then
  printf 'fleetmemmap   %s   brain_app\nfleetmemmap   postgres   brain_app\n' "$SVC_USER" >> "$IDENT"
fi
if ! grep -qE "brain_app[[:space:]]+peer[[:space:]]+map=fleetmemmap" "$HBA" 2>/dev/null; then
  sed -i "0,/^local[[:space:]]/s//local   all   brain_app   peer   map=fleetmemmap\n&/" "$HBA"
fi
runuser -u postgres -- psql -c "SELECT pg_reload_conf()" >/dev/null

# 5. systemd + nginx ----------------------------------------------------------
echo "-- installing systemd units + nginx (review paths in deploy/*.service, deploy/nginx-brain-api.conf) --"
cp "$PREFIX"/systemd/*.service "$PREFIX"/systemd/*.timer /etc/systemd/system/ 2>/dev/null || true
cp "$PREFIX"/deploy/brain-api.service "$PREFIX"/deploy/brain-mcp-http.service /etc/systemd/system/ 2>/dev/null || true
# RLS drop-in: make brain-api connect as the NON-owner role brain_app so Postgres Row-Level Security
# actually bites. Without it the app connects as the table owner, which BYPASSES RLS entirely.
install -D -m 644 "$PREFIX/deploy/brain-api.service.d-rls.conf" \
     /etc/systemd/system/brain-api.service.d/rls.conf 2>/dev/null || true
cp "$PREFIX/deploy/nginx-brain-api.conf" /etc/nginx/sites-available/fleetmem 2>/dev/null || true
ln -sf /etc/nginx/sites-available/fleetmem /etc/nginx/sites-enabled/fleetmem 2>/dev/null || true
# Template EVERY installed unit (the two core services + all hygiene timers) so their hardcoded
# build paths point at $PREFIX and their interpreter is the venv python (which has the pinned deps).
tmpl_units=(/etc/systemd/system/brain-api.service /etc/systemd/system/brain-mcp-http.service)
for u in "$PREFIX"/systemd/*.service; do tmpl_units+=("/etc/systemd/system/$(basename "$u")"); done
for f in "${tmpl_units[@]}" /etc/nginx/sites-available/fleetmem; do
  [ -f "$f" ] && sed -i \
     -e "s#/opt/brain-db/db#$PREFIX#g" -e "s#/opt/brain-db/pki#$PREFIX/pki#g" -e "s#/opt/brain-db#$PREFIX#g" \
     -e "s#/usr/bin/python3#$PREFIX/venv/bin/python#g" "$f"
done
systemctl daemon-reload
systemctl enable nginx >/dev/null 2>&1 || true
nginx -t 2>/dev/null && systemctl reload nginx || echo "NOTE: adjust nginx conf paths, then: systemctl reload nginx"
systemctl enable --now brain-api.service 2>/dev/null || echo "NOTE: start the API once its unit paths/env are set."
# brain-mcp-http (optional remote MCP-over-HTTP) needs client credentials (~/.fleetmem/*), which only
# exist AFTER you create the genesis manager — so DON'T start it now (it would crash-loop pre-enrol).
# It's installed; enable it once credentials exist (see the closing note).
# hygiene timers (retention prune / re-embed / edge classify / etc.) — enable so they run AND survive reboot
for t in brain-classify-edges brain-derive-infra-edges brain-golden-eval brain-memory-verify brain-reembed brain-retention-prune brain-validate-sweep; do
  systemctl enable --now "$t.timer" >/dev/null 2>&1 || true
done

# 6. genesis manager ----------------------------------------------------------
cat <<EOF

== fleetmem base install done. The database is EMPTY (no memories). ==
Create your FIRST (genesis) manager — you pick the name:

  runuser -u $SVC_USER -- env PGDATABASE=$PGDATABASE PKI_DIR=$PREFIX/pki \\
       FLEETMEM_MANAGER=<name> ENROLL_APPROVALS=<1-or-more> \\
       $PREFIX/venv/bin/python $PREFIX/fleetmem-bootstrap-manager.py

Then register the fleetmem MCP with your LLM agent (it prints the exact env). See AGENTS.md.

Optional: to run the central remote MCP-over-HTTP service, do it AFTER the genesis manager exists
(its ~/.fleetmem credentials must be present), then:  systemctl enable --now brain-mcp-http
EOF
