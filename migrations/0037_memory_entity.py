"""0037 memory<->entity junction ( Tier 1) — the structured entity-linking layer for recall.

The bipartite backbone the 2026 graph-memory research (Graphiti/HippoRAG) calls for: each memory is
associated with the salient ENTITIES it mentions (curated entity registry names + deterministic
domain refs: task-ids T###, containers LXC###, hosts PC#, LAN IPs, *.py files). Recall's dedicated
entity-expansion step reads this to pull in memories sharing a RARE entity with the query or
the content hits — a peer retrieval signal to the memory_relation graph, weighted by entity rarity
so hub entities (the brain host, api.py) don't spray noise.

Structural metadata like the entity/infra models -> NO row-level security (only `memory` carries RLS); recall JOINs back to `memory` so the RLS `where` clause still gates visibility. Just brain_app
grants. Reversible: down() drops the table (the entity registry + memory_relation are untouched).
"""
VERSION = "0037"
NAME = "memory_entity"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory_entity (
          memory_id   uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
          entity_name text NOT NULL,                       -- canonical lowercase: registry name OR ref (task-ref, host-name)
          kind        text NOT NULL DEFAULT 'other',
          source      text NOT NULL DEFAULT 'deterministic',
          mentions    int  NOT NULL DEFAULT 1,
          created_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (memory_id, entity_name)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS memory_entity_entity_idx ON memory_entity(entity_name)")
    # a materialized freq per entity is cheap to compute on the fly; index above covers the GROUP BY.
    cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memory_entity TO brain_app")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS memory_entity")
