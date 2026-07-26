"""0001 core schema — the 10 core brain tables.

schema_migrations and action_log (both KIND=system) are bootstrapped by the runner;
this migration creates the other 8, each built THROUGH the Table-Contract scaffolder
so the contract holds from row one. No ad-hoc DDL.

Tables: project, task, idea, agent (structure) · memory (knowledge) ·
memory_relation (structure) · proposal, lesson (knowledge).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contract import create_table  # noqa: E402

VERSION = "0001"
NAME = "core_schema"


def up(cur):
    # --- structure: project (referenced by task/idea) ---
    cur.execute(create_table("project", "structure",
        columns=[
            "slug text NOT NULL",
            "title text NOT NULL",
            "status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','ongoing','paused','done','archived'))",
            "description text",
        ],
        constraints=["CONSTRAINT project_slug_uniq UNIQUE (slug)"]))

    # --- structure: task (id = surrogate uuid; T-number is the separate UNIQUE handle) ---
    cur.execute(create_table("task", "structure",
        columns=[
            "handle text NOT NULL",   # '' etc.
            "title text NOT NULL",
            "status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in-progress','blocked','done'))",
            "project_id uuid REFERENCES project(id) ON DELETE SET NULL",
            "assignee text",
            "task_tier int",
            "lane text",
            "notes text",
            "acceptance text",
            "verify text",
        ],
        constraints=["CONSTRAINT task_handle_uniq UNIQUE (handle)"]))

    # --- structure: idea (raw -> promoted -> project) ---
    cur.execute(create_table("idea", "structure",
        columns=[
            "body text NOT NULL",
            "status text NOT NULL DEFAULT 'raw' CHECK (status IN ('raw','promoted','dropped'))",
            "promoted_project_id uuid REFERENCES project(id) ON DELETE SET NULL",
        ]))

    # --- structure: agent (bodies + access scope + welcome) ---
    cur.execute(create_table("agent", "structure",
        columns=[
            "name text NOT NULL",
            "cert_cn text",                                  # verified mTLS CN -> identity
            "access_scope jsonb NOT NULL DEFAULT '{}'::jsonb",
            "welcome text",
            "lane text",
            "agent_tier int",
        ],
        constraints=[
            "CONSTRAINT agent_name_uniq UNIQUE (name)",
            "CONSTRAINT agent_cert_cn_uniq UNIQUE (cert_cn)",
        ]))

    # --- knowledge: memory (the facts) ---
    cur.execute(create_table("memory", "knowledge",
        columns=[
            "name text",                                     # slug handle
            "mtype text CHECK (mtype IN ('user','feedback','project','reference','memory'))",
            "mem_tier text NOT NULL DEFAULT 'semantic' CHECK (mem_tier IN ('semantic','episodic'))",
            "description text",
            "body text NOT NULL",
            "embedding vector(1024)",                        # pinned-model dense vector
            "embed_model text",                              # stamped per row; assert on rebuild
            "tsv tsvector GENERATED ALWAYS AS "
            "(to_tsvector('english', coalesce(description,'') || ' ' || coalesce(body,''))) STORED",
        ]))

    # --- structure: memory_relation (system-maintained links) ---
    cur.execute(create_table("memory_relation", "structure",
        columns=[
            "src_id uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE",
            "dst_id uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE",
            "rel_type text NOT NULL CHECK (rel_type IN ('supersedes','conflicts_with','relates_to','invalidated_by'))",
        ],
        constraints=[
            "CONSTRAINT memrel_uniq UNIQUE (src_id, dst_id, rel_type)",
            "CONSTRAINT memrel_no_self CHECK (src_id <> dst_id)",
        ]))

    # --- knowledge: proposal (the auto-learn / /approve queue) ---
    cur.execute(create_table("proposal", "knowledge",
        columns=[
            "name text",
            "mtype text",
            "proposed_body text NOT NULL",
            "description text",
            "target_memory_id uuid REFERENCES memory(id) ON DELETE SET NULL",  # edit/supersede target
            "status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','superseded'))",
            "decided_by text",
            "decided_at timestamptz",
            "reason text",
        ]))

    # --- knowledge: lesson (poison defense — consulted before acting) ---
    cur.execute(create_table("lesson", "knowledge",
        columns=[
            "title text",
            "pattern text",                                  # the rejection/poison signature
            "severity text NOT NULL DEFAULT 'normal' CHECK (severity IN ('low','normal','high','critical'))",
            "source_proposal_id uuid REFERENCES proposal(id) ON DELETE SET NULL",
        ]))

    # --- indexes ---
    # FTS (keyword side of hybrid retrieval)
    cur.execute("CREATE INDEX IF NOT EXISTS memory_tsv_gin ON memory USING gin (tsv);")
    # NO HNSW yet — exact scan until ~50k rows (locked decision); column only.
    cur.execute("CREATE INDEX IF NOT EXISTS memory_readers_gin ON memory USING gin (readers);")
    cur.execute("CREATE INDEX IF NOT EXISTS memory_live_idx ON memory (sensitivity, trust) WHERE deleted_at IS NULL AND invalid_at IS NULL;")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS memory_name_uniq ON memory (name) WHERE deleted_at IS NULL AND name IS NOT NULL;")
    cur.execute("CREATE INDEX IF NOT EXISTS proposal_status_idx ON proposal (status) WHERE deleted_at IS NULL;")
    cur.execute("CREATE INDEX IF NOT EXISTS task_status_idx ON task (status);")


def down(cur):
    for t in ("lesson", "proposal", "memory_relation", "memory",
              "agent", "idea", "task", "project"):
        cur.execute("DROP TABLE IF EXISTS %s CASCADE;" % t)
