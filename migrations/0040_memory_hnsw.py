"""0040 HNSW index on memory.embedding — make recall's dense arm an index scan, not a Seq Scan.

The dense recall arm (search.py / api.py recall) runs `ORDER BY embedding <=> query` over every live
memory row — an exact scan, O(n) per query. Migration 0001 deferred the ANN index ("NO HNSW yet — exact
scan until ~50k rows"), fine for the home corpus but a shipped instance's corpus size is unbounded.
refdoc (0038) and skill (0039) already ship an HNSW cosine index; this mirrors it for `memory` so recall
stays fast as the corpus grows.

pgvector >= 0.5 (the box runs 0.8.x). Plain CREATE INDEX (blocking), matching 0038/0039 — migrate.py
runs each migration inside a transaction, so CREATE INDEX CONCURRENTLY (which can't run in a txn) is not
used; on a large existing table this build takes a one-off lock at deploy time. IF NOT EXISTS makes it
re-runnable. vector_cosine_ops matches the `<=>` cosine distance recall uses.

Reversible: down() drops the index.
"""
VERSION = "0040"
NAME = "memory_hnsw"


def up(cur):
    cur.execute("CREATE INDEX IF NOT EXISTS memory_embedding_hnsw "
                "ON memory USING hnsw (embedding vector_cosine_ops)")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_embedding_hnsw")
