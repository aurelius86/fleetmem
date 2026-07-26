"""0039 skill — fleetmem skill corpus for `brain_skill_recall` / `brain_skill_get`.

A generic, on-demand skill library: full SKILL.md bodies stored in fleetmem and pulled when needed, instead of
bloating every session's always-on context (cache-law). Holds the Superpowers skills (MIT, obra/Jesse Vincent)
now and our own skills later. SEPARATE table from `memory` (like `refdoc`/`entity`) so skills never pollute
`brain_recall`. Same pinned embedder (bge-m3, `vector(1024)`) over the name+description, so a "which skill fits
this situation" query lands the right skill; the full `body` is fetched by name on demand.

Non-sensitive (methodology text), so NO row-level security — just `brain_app` grants, like the infra model (0021).
Reversible: `down()` drops the table + index.
"""
VERSION = "0039"
NAME = "skill"


def up(cur):
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS skill (
          id          bigserial PRIMARY KEY,
          name        text NOT NULL UNIQUE,
          source      text,
          description text,
          body        text NOT NULL,
          embedding   vector(1024),
          embed_model text,
          created_at  timestamptz DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS skill_embedding_idx "
                "ON skill USING hnsw (embedding vector_cosine_ops)")
    cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON skill TO brain_app")
    cur.execute("GRANT USAGE, SELECT ON SEQUENCE skill_id_seq TO brain_app")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS skill_embedding_idx")
    cur.execute("DROP TABLE IF EXISTS skill")
