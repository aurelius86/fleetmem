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
1. Fetch the latest release into a staging dir (`git clone`/`git pull`, or download).
2. Copy the code over your install dir, **keeping your `brain.env` and `pki/`** (do not overwrite them).
3. Apply migrations + restart:
   ```sh
   runuser -u brain -- env PGDATABASE=brain python3 /opt/fleetmem/migrate.py up
   systemctl restart brain-api && systemctl reload nginx
   ```

## Check state / roll back
- Applied vs pending:  `python3 migrate.py status`
- Roll back one migration:  `python3 migrate.py down <version>`

Migrations are transactional and reversible, so an upgrade either fully applies or leaves the
schema unchanged — it won't half-migrate your data.
