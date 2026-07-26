"""0014 edge_classified — mark which graph edges the LLM pass has already examined.

The whole-note classifier (classify_edges.py) types plain `relates_to` edges. It needs an
"examined" flag so it never re-spends an LLM call on the same edge — WITHOUT overloading
`created_by` (which carries provenance: 'explicit-ref' vs 'graph-classifier' vs co-use, and
drives resync_explicit_refs' prune). So: a dedicated nullable timestamp. NULL = pending,
set = examined (whether or not it got promoted to a typed relation). Additive + reversible.
"""
VERSION = "0014"
NAME = "edge_classified"


def up(cur):
    cur.execute("ALTER TABLE memory_relation ADD COLUMN IF NOT EXISTS classified_at timestamptz")
    # partial index: the classifier only ever scans the pending 'relates_to' backlog
    cur.execute("""CREATE INDEX IF NOT EXISTS memory_relation_pending_idx
                   ON memory_relation (rel_type)
                   WHERE classified_at IS NULL""")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_relation_pending_idx")
    cur.execute("ALTER TABLE memory_relation DROP COLUMN IF EXISTS classified_at")
