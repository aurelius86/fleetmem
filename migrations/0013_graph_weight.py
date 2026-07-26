"""0013 graph_weight — typed+weighted knowledge-graph edges.

memory_relation already holds [[link]]-derived edges. This makes them typed+weighted:
a weight counter (increment on repeat, not duplicate rows) + a uniqueness key on
(src_id,dst_id,rel_type) so the classifier can upsert. rel_type stays FREE TEXT (open ontology) — governance is a config file, not a DB enum. Additive+reversible.
"""
VERSION = "0013"
NAME = "graph_weight"


def up(cur):
    cur.execute("ALTER TABLE memory_relation ADD COLUMN IF NOT EXISTS weight int NOT NULL DEFAULT 1")
    # collapse accidental exact dups (same src,dst,rel_type), keep earliest
    cur.execute("""
        WITH ranked AS (
          SELECT id, row_number() OVER (
                   PARTITION BY src_id, dst_id, rel_type ORDER BY created_at) AS rn
          FROM memory_relation)
        DELETE FROM memory_relation m USING ranked r
        WHERE m.id = r.id AND r.rn > 1
    """)
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS memory_relation_edge_uk
                   ON memory_relation (src_id, dst_id, rel_type)""")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_relation_edge_uk")
    cur.execute("ALTER TABLE memory_relation DROP COLUMN IF EXISTS weight")
