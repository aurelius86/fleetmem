#!/usr/bin/env bash
# fleetmem-git-sync.example.sh — OPTIONAL off-box markdown backup of your memories into YOUR git repo.
#
# fleetmem works fully without this. If you want a versioned, human-readable mirror of your memories,
# copy this file, set GIT_REMOTE to a repo YOU own, and run it on a timer (cron/systemd). Nothing is
# sent anywhere unless you configure a remote — this is opt-in and uses only your own repository.
#
# It exports each memory row to a markdown file and commits+pushes. It never exports tokens, certs,
# or keys (those are not in the memory table). Review what you push before pointing at a public repo.
set -euo pipefail

GIT_REMOTE="${GIT_REMOTE:?set GIT_REMOTE to your own git repo URL (e.g. git@github.com:you/fleetmem-backup.git)}"
WORKDIR="${WORKDIR:-$HOME/.fleetmem/backup}"
PGDATABASE="${PGDATABASE:-brain}"

mkdir -p "$WORKDIR"; cd "$WORKDIR"
[ -d .git ] || { git init -q; git remote add origin "$GIT_REMOTE"; }

mkdir -p memory
# One markdown file per live memory (name, description, body). A shell `read` loop would split on
# newlines INSIDE a body (most memories are multiline), so a small Python emitter does the export —
# it also forces UTF-8 so a C-locale host doesn't choke on non-ASCII content.
python3 - "$PGDATABASE" <<'PY'
import os, sys, psycopg2
conn = psycopg2.connect(dbname=sys.argv[1], client_encoding="UTF8")
cur = conn.cursor()
cur.execute("SELECT name, coalesce(description,''), body FROM memory "
            "WHERE deleted_at IS NULL ORDER BY name")
os.makedirs("memory", exist_ok=True)
for name, desc, body in cur.fetchall():
    # names are slugs; basename guards against any stray path separator
    with open(os.path.join("memory", os.path.basename(name) + ".md"), "w", encoding="utf-8") as f:
        f.write("---\nname: %s\ndescription: %s\n---\n\n%s\n" % (name, desc, body))
conn.close()
PY

git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "fleetmem memory backup $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git push -q origin HEAD
  echo "pushed memory backup to $GIT_REMOTE"
else
  echo "no changes to back up"
fi
