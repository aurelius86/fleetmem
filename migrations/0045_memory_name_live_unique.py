"""0045 memory_name_live_unique — one live row per name, enforced at the DB.

The always-injected MOCs (always_on_rules_moc, core_knowledge_moc) and every name-keyed lookup
assume a name resolves to a single live row, but nothing at the DB level guaranteed it — only the
RENAME path checked collisions in the app layer (curate_memory_edit `WHERE name=%s AND deleted_at
IS NULL AND id<>%s`). Insert/seed/edit paths didn't, so two live rows could share a name, and
`/bootstrap` would then inject an ARBITRARY one as the session's operating rules — nondeterministic,
silent, and able to differ per gunicorn worker. This adds a partial unique index so two live rows
can't share a name. (api.py now also selects `ORDER BY updated_at DESC` — defense in depth.)

Scope: NAMED live rows only. Live rows with a NULL or empty name are carved out — the fleet brain
has 25 such rows (real memories that lost their name; tracked as a data-quality task), and without
the carve-out the index could not build. Verified live: zero NAMED live duplicates, so
this builds cleanly on the fleet brain; on a fresh install the table is empty. If a build ever fails,
it means a duplicate live name slipped in — resolve it (soft-delete/rename the older row) and re-run.

Reversible: down() drops the index.
"""
VERSION = "0045"
NAME = "memory_name_live_unique"


def up(cur):
    # Plain (not CONCURRENTLY) so it runs inside the migration transaction; the memory table is small
    # enough that the brief lock is a non-issue, and a fresh install builds it over an empty table.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS memory_name_live_uniq "
        "ON memory (name) WHERE deleted_at IS NULL AND name IS NOT NULL AND name <> ''"
    )


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_name_live_uniq")
