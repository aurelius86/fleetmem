"""0038 refdoc — in-house library-docs corpus for `brain_docs_recall` (self-hosted Context7).

A SEPARATE table from `memory` (like `entity`/`session_turn`) so ingested library documentation never
pollutes canonical recall. Same pinned embedder (bge-m3, `vector(1024)`) = one vector space. Retrieval is
plain pgvector cosine (`embedding <=> query`); an HNSW index keeps it fast as the corpus grows. Docs are
PUBLIC/non-sensitive, so NO row-level security — just `brain_app` grants, exactly like the infra model (0021).

Idempotent: the table was created live during the Phase-A POC (owned by the brain service user after the deploy owner-fix);
this migration codifies it + adds the ANN index + grants. Reversible: `down()` drops the index + revokes the
grants (rows are kept — drop the table by hand if the data itself is unwanted).
"""
VERSION = "0038"
NAME = "refdoc"


def up(cur):
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS refdoc (
          id          bigserial PRIMARY KEY,
          library     text NOT NULL,
          version     text,
          source_url  text,
          title       text,
          chunk_idx   int,
          body        text NOT NULL,
          embedding   vector(1024),
          embed_model text,
          created_at  timestamptz DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS refdoc_library_idx ON refdoc(library)")
    # pgvector >= 0.5 (box runs 0.8.3); HNSW cosine to match the `embedding <=> query` recall
    cur.execute("CREATE INDEX IF NOT EXISTS refdoc_embedding_idx "
                "ON refdoc USING hnsw (embedding vector_cosine_ops)")
    # non-owner app role needs table + sequence grants (like the infra model, 0021); no RLS (public docs)
    cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON refdoc TO brain_app")
    cur.execute("GRANT USAGE, SELECT ON SEQUENCE refdoc_id_seq TO brain_app")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS refdoc_embedding_idx")
    cur.execute("REVOKE ALL ON refdoc FROM brain_app")
    # keep the table + rows on down(); drop by hand if the corpus itself should go
