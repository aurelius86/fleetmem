"""0011 pg_trgm — trigram fuzzy matching for typo/OOV recall queries.

The keyword (FTS) arm is exact-token: a typo ('acme-routr') or an out-of-vocabulary term
finds nothing, so recall leans entirely on the dense arm. This enables Postgres pg_trgm so
/recall can run a trigram word-similarity FALLBACK when the FTS arm returns zero rows —
catching a near-miss spelling -> the canonical note. Additive + reversible (this migration introduced
the extension, so down removes it)."""
VERSION = "0011"
NAME = "pg_trgm"


def up(cur):
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # GIN trigram indexes (future-scale; the fallback query works on a seq scan at today's size)
    cur.execute("CREATE INDEX IF NOT EXISTS memory_desc_trgm ON memory USING gin (description gin_trgm_ops)")
    cur.execute("CREATE INDEX IF NOT EXISTS memory_name_trgm ON memory USING gin (name gin_trgm_ops)")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_desc_trgm")
    cur.execute("DROP INDEX IF EXISTS memory_name_trgm")
    cur.execute("DROP EXTENSION IF EXISTS pg_trgm")
