# Upgrading fleetmem

fleetmem keeps **code**, **your data**, and **your identity** as separate layers, so upgrades never
touch your memories.

## Preserved across every upgrade
- **Database** — all memories, tasks, agents. Schema changes ride numbered, reversible migrations.
- **`brain.env`** — your config. Untracked; never overwritten.
- **`pki/`** — your local CA + server cert, and agent tokens. On disk; untouched.

## Upgrade — one command
From your fleetmem directory (e.g. `/opt/fleetmem`), as root:
```sh
sudo ./update.sh
```
It pulls the latest code, applies any new migrations (idempotent — only pending ones run), and
restarts the service.

## Manual upgrade (if you did NOT install from a git clone)
1. Fetch the latest release into a staging dir (`git clone`/`git pull`, or download + extract).
2. Copy the code over your install dir with **`cp -a`** (preserves perms; no extra tooling needed —
   `rsync` may not be installed), **keeping your `brain.env` and `pki/`**:
   ```sh
   cp -a staging/. /opt/fleetmem/          # brain.env + pki/ are yours; don't overwrite them
   ```
3. Rebuild deps + apply migrations + restart. fleetmem runs from a **venv** (`$PREFIX/venv`); if you are
   moving from an older system-python layout, `update.sh` builds the venv **and rewires the systemd
   `ExecStart`** for you — prefer it:
   ```sh
   sudo ./update.sh
   ```
   Or by hand (build the venv if absent — needs the `python3-venv` package):
   ```sh
   python3 -m venv /opt/fleetmem/venv && /opt/fleetmem/venv/bin/pip install -r /opt/fleetmem/requirements.txt
   runuser -u brain -- env PGDATABASE=brain /opt/fleetmem/venv/bin/python /opt/fleetmem/migrate.py up
   systemctl restart brain-api && systemctl reload nginx
   ```
4. Verify: **`python3 doctor.py`** (checks DB encoding, migration state, venv, and failed timers).

## Re-encoding a non-UTF8 (SQL_ASCII) database

If `migrate.py status` warns, or `/healthz` reports `encoding` other than `UTF8`, your database was
created `SQL_ASCII`/`C` and **silently rejects non-ASCII writes** (em-dash, Arabic, …).
`PGCLIENTENCODING=UTF8` does **not** fix this — the *server* database encoding is what rejects it.
Re-encode it once (lossless when the stored bytes are already valid UTF-8, which they are for a brain
that only ever failed on WRITE):

```sh
# as the postgres superuser, with brain-api stopped
systemctl stop brain-api
pg_dump -Fc brain > /tmp/brain.dump
createdb -O brain --encoding=UTF8 --template=template0 --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8 brain_utf8
pg_restore -d brain_utf8 /tmp/brain.dump
psql -c "ALTER DATABASE brain RENAME TO brain_sqlascii_bak;"
psql -c "ALTER DATABASE brain_utf8 RENAME TO brain;"
# PG15+ revokes CREATE on schema public from non-owners — restore it if you run with BRAIN_DB_USER:
psql -d brain -c "GRANT ALL ON SCHEMA public TO brain_app;"
systemctl start brain-api
# verify (migrate.py status + a non-ASCII write), then drop the backup:  dropdb brain_sqlascii_bak
```

## Check state / roll back
- Applied vs pending:  `python3 migrate.py status`
- Roll back one migration:  `python3 migrate.py down <version>`
- Re-hash a migration whose file was intentionally, schema-neutrally edited:  `python3 migrate.py reconcile` (shows the drift; add `--yes` to apply)
- Full health check (DB encoding, migration state, venv, failed timers):  `python3 doctor.py`  ·  machine-readable: `GET /healthz/detail`

Migrations are transactional and reversible, so an upgrade either fully applies or leaves the
schema unchanged — it won't half-migrate your data.
