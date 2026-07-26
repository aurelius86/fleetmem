"""0029 entity model — the canonical ENTITY registry for un-bracketed mention resolution.

The 2026-standard graph-memory pattern (Graphiti / Mem0g / Zep) resolves a plain-text mention to an
ENTITY NODE, not directly to a note: one canonical node per real-world thing, with aliases + an
embedding, so every mention and every note about "my-service" lands on the SAME node. This migration
adds that node layer for NON-INFRA entities (agent bodies, projects, models like bge-m3,
tools, concepts). Infra entities (hosts/IPs/services) already have their registry in
infra_host / infra_service (0021) — resolution reuses those directly; this is the deliberately
SEPARATE table for everything else (operator decision: keep the dashboard infra contract clean).

Resolution (built in a later phase) matches a mention to an entity in 3 tiers:
  a. exact alias hit (entity_alias.alias)            -> deterministic
  b. embedding cosine vs entity.embedding (bge-m3)   -> semantic
  c. LLM tiebreak/abstain when a & b disagree        -> qwen3:30b @ the model host Ollama
The resulting edge is born relates_to and flows through the existing classify_edges + review gate.

Non-sensitive structural metadata like the infra model — so NO row-level security (only `memory`
carries RLS); just brain_app grants + app-layer manager-gated writes. Reversible: down() drops
both tables (any seeded entities/aliases are lost -> resolution simply finds no entity layer).
"""
VERSION = "0029"
NAME = "entity_model"


def up(cur):
    # pgvector is already present (memory.embedding is vector(1024)); guarded for a fresh clone.
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entity (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name          text UNIQUE NOT NULL,               -- canonical key: agent-a, agent-b, bge-m3, brain-store-consolidation
          kind          text NOT NULL DEFAULT 'other'
                          CHECK (kind IN ('body','person','project','service','model','tool','concept','org','other')),
          display       text,
          anchor_memory text,                               -- the canonical reference_* note this entity resolves to
          description   text,
          embedding     vector(1024),                       -- bge-m3 (matches memory.embedding) for tier-b resolution
          embed_model   text,                               -- model@digest that produced the vector (swap-detectable)
          created_at    timestamptz NOT NULL DEFAULT now(),
          updated_at    timestamptz NOT NULL DEFAULT now()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entity_alias (
          id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          alias       text NOT NULL,                        -- a surface form (lowercased): 'my-service', 'my service'
          entity_name text NOT NULL,                        -- -> entity.name (an alias MAY map to >1 entity = ambiguous)
          source      text NOT NULL DEFAULT 'curated'
                        CHECK (source IN ('curated','dryrun','llm','infra')),
          created_at  timestamptz NOT NULL DEFAULT now(),
          UNIQUE (alias, entity_name)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS entity_alias_alias_idx ON entity_alias (lower(alias))")
    cur.execute("CREATE INDEX IF NOT EXISTS entity_kind_idx ON entity (kind)")
    # grants for the non-owner app role; guarded so a fresh clone without the role still applies
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON entity, entity_alias TO brain_app; "
                "END IF; END $$")


def down(cur):
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "REVOKE ALL ON entity, entity_alias FROM brain_app; END IF; END $$")
    cur.execute("DROP TABLE IF EXISTS entity_alias")
    cur.execute("DROP TABLE IF EXISTS entity")
