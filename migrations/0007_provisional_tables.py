"""0007 provisional agent tables — sandboxed, anchored to a provisional memory.

the operator (provisional-tier-design.md §3). An agent creates a table DIRECTLY (no
manager gate at creation) for structured data (e.g. acme-router devices), but safely:
  - NOT raw SQL — the agent submits a STRUCTURED spec (name + typed columns from an
    allowlist); the API builds the DDL (api.py).
  - the table lives in an isolated `provisional` SCHEMA, author-only, never in core.
  - it is ANCHORED to a provisional memory. The anchor memory's fate decides the table's:
    graduate -> the table is promoted to the governed schema; delete/expire -> the table
    is cascade-dropped.

This migration creates the sandbox schema + the `provisional_artifact` registry that ties
each sandbox table to its anchor memory and tracks its lifecycle. The per-table DDL is built
at runtime by api.py (validated), never here.
"""
VERSION = "0007"
NAME = "provisional_tables"


def up(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS provisional")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS provisional_artifact (
          id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          author_body   text NOT NULL,
          kind          text NOT NULL DEFAULT 'table' CHECK (kind IN ('table')),
          object_name   text NOT NULL,          -- the sandbox table name, e.g. fox__devices
          display_name  text NOT NULL,          -- the name the agent gave, e.g. devices
          columns_spec  jsonb NOT NULL,         -- [{name,type}] — for inserts + graduation
          anchor_memory_id uuid NOT NULL,       -- the provisional memory it lives or dies with
          status        text NOT NULL DEFAULT 'provisional'
                          CHECK (status IN ('provisional','graduated','deleted')),
          created_at    timestamptz NOT NULL DEFAULT now(),
          expires_at    timestamptz             -- mirrors the anchor's TTL
        )
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS provisional_artifact_obj_live "
                "ON provisional_artifact (object_name) WHERE status <> 'deleted'")
    cur.execute("CREATE INDEX IF NOT EXISTS provisional_artifact_anchor_idx "
                "ON provisional_artifact (anchor_memory_id) WHERE status = 'provisional'")


def down(cur):
    # drop every sandbox table the registry still owns, then the registry + schema
    cur.execute("SELECT object_name FROM provisional_artifact WHERE status <> 'deleted'")
    for (obj,) in cur.fetchall():
        cur.execute('DROP TABLE IF EXISTS provisional."%s"' % obj.replace('"', ''))
    cur.execute("DROP TABLE IF EXISTS provisional_artifact")
    cur.execute("DROP SCHEMA IF EXISTS provisional CASCADE")
