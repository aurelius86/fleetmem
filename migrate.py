#!/usr/bin/env python3
"""Brain DB migration runner.

Gated, reversible, recorded. No ad-hoc DDL — schema changes happen ONLY through
numbered migration modules in ./migrations, applied here inside one transaction each,
recorded in `schema_migrations`, and logged to `action_log`.

Usage (run as the brain service user via local peer auth):
    python3 migrate.py status        # show applied vs pending
    python3 migrate.py up            # apply all pending migrations
    python3 migrate.py down <ver>    # roll back a single applied migration

Connection: local unix socket, dbname=brain (peer auth maps OS brain -> PG role brain).
Override with PGDATABASE / standard libpq env vars if needed.
"""
import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

DB = os.environ.get("PGDATABASE", "brain")
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATE_LOCK = 823041   #/A3-11: fixed advisory-lock key so concurrent `up` runs serialize


def connect():
    # empty host => unix socket => peer auth. Force UTF-8 client encoding so migrations carrying
    # non-ASCII text apply even on a C/POSIX-locale host (fresh minimal installs).
    conn = psycopg2.connect(dbname=DB, client_encoding="UTF8")
    conn.set_client_encoding("UTF8")
    return conn


def ensure_bootstrap(cur):
    """Create schema_migrations (system kind) if absent — the only table the runner
    creates directly, because it must exist before any migration can be recorded."""
    # The `vector` type is used from migration 0001 (memory.embedding vector(1024)) but the first
    # CREATE EXTENSION lived in 0029, so `up` from a TRULY EMPTY db died at 0001 ("type vector does
    # not exist"). The live brain never hit this — pgvector was created by hand at provision time —
    # but CI (empty pgvector service container) and any greenfield deploy do. The runner must
    # guarantee the extension its schema depends on, same as it bootstraps schema_migrations.
    # IF NOT EXISTS is a no-op (no privilege needed) where it already exists; editing 0001 instead
    # would trip the drift guard on the live brain, so the fix lives here in the (unchecksummed) runner.
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          created_at  timestamptz NOT NULL DEFAULT now(),
          version     text NOT NULL UNIQUE,
          name        text NOT NULL,
          checksum    text NOT NULL,
          applied_at  timestamptz NOT NULL DEFAULT now()
        );
    """)
    # action_log too (system kind) so the very first migration can be audited
    cur.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          created_at  timestamptz NOT NULL DEFAULT now(),
          actor       text,
          action      text NOT NULL,
          target_kind text,
          target_id   text,
          detail      jsonb,
          reversible  boolean NOT NULL DEFAULT true,
          reverted_at timestamptz
        );
    """)


def log_action(cur, action, target_kind=None, target_id=None, detail=None, reversible=True):
    cur.execute(
        "INSERT INTO action_log(actor, action, target_kind, target_id, detail, reversible)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (os.environ.get("USER", "brain"), action, target_kind, target_id,
         psycopg2.extras.Json(detail) if detail else None, reversible),
    )


def discover():
    """Return sorted [(version, name, module)] from ./migrations/NNNN_name.py."""
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9]*_*.py")):
        version = path.stem.split("_", 1)[0]
        name = path.stem.split("_", 1)[1]
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out.append((version, name, mod, path))
    return out


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def applied(cur):
    cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    return {v: c for v, c in cur.fetchall()}


def cmd_status():
    with connect() as conn, conn.cursor() as cur:
        ensure_bootstrap(cur)
        done = applied(cur)
        print("applied: %d" % len(done))
        for version, name, _mod, path in discover():
            mark = "APPLIED" if version in done else "pending"
            drift = ""
            if version in done and done[version] != checksum(path):
                drift = "  !! CHECKSUM DRIFT (file changed since applied)"
            print("  [%s] %s  %s%s" % (mark.rjust(7), version, name, drift))


def cmd_up():
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_bootstrap(cur)
            #/A3-11: serialize concurrent runners — a second `up` blocks here until the first
            # releases the lock (at process exit), so two can't race a migration into a half state.
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK,))
            done = applied(cur)
        #/A3-11: refuse to build on a drifted base — if an already-applied migration's file
        # changed since it ran, the recorded schema no longer matches the code. Abort loudly rather
        # than silently stacking new migrations on top (was: `up` ignored drift entirely).
        drifted = [v for v, name, mod, path in discover() if v in done and done[v] != checksum(path)]
        if drifted:
            sys.exit("ABORT: checksum drift on already-applied migration(s): %s — the file(s) changed "
                     "since they were applied; reconcile before running `up`." % ", ".join(drifted))
        for version, name, mod, path in discover():
            if version in done:
                continue
            with conn.cursor() as cur:
                print("applying %s_%s ..." % (version, name))
                mod.up(cur)
                cur.execute(
                    "INSERT INTO schema_migrations(version, name, checksum) VALUES (%s,%s,%s)",
                    (version, name, checksum(path)),
                )
                log_action(cur, "apply_migration", "schema_migrations", version,
                           {"name": name, "checksum": checksum(path)})
            conn.commit()
            print("  ok %s" % version)
    print("up: done")


def cmd_down(target):
    with connect() as conn:
        for version, name, mod, path in reversed(discover()):
            if version != target:
                continue
            if not hasattr(mod, "down"):
                sys.exit("migration %s has no down()" % version)
            with conn.cursor() as cur:
                print("rolling back %s_%s ..." % (version, name))
                mod.down(cur)
                cur.execute("DELETE FROM schema_migrations WHERE version=%s", (version,))
                log_action(cur, "revert_migration", "schema_migrations", version, {"name": name})
            conn.commit()
            print("  rolled back %s" % version)
            return
    sys.exit("migration %s not found" % target)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    elif cmd == "up":
        cmd_up()
    elif cmd == "down" and len(sys.argv) == 3:
        cmd_down(sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
