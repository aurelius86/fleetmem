"""0026 drop the leftover _t98_backup_20260703 table.

It is a point-in-time `SELECT name, body FROM memory` snapshot from, created OUTSIDE
the migration system (violating migrate.py's no-ad-hoc-DDL rule). Audit A3-9 + re-verified:
281 rows, columns (name text, body text), ZERO code references anywhere, and every non-null name still
exists in the live `memory` table (0 orphans) — fully redundant. A pg_dump safety copy was taken first
and stored at reports/mac/brain-audit-2026-07-04-appendix/_t98_backup_pre-drop.sql (versioned + in the
off-box backup chain), so the data is recoverable if ever needed.

down() recreates the EMPTY table shell (the snapshot data is restorable from that pg_dump, not from here).
"""
VERSION = "0026"
NAME = "drop_t98_backup"

_T = "_t98_backup_20260703"


def up(cur):
    cur.execute("DROP TABLE IF EXISTS %s" % _T)


def down(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS %s (name text, body text)" % _T)
