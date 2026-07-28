"""0043 memory_provenance — link a synthesized memory to the RAW session evidence it was
distilled from, recorded AT SYNTHESIS TIME by autolearn (not reconstructed later).

Why a purpose-built table and NOT a memory_relation edge or a session_turn FK:
  * memory_relation is memory->memory (src_id/dst_id are memory ids); the evidence is not a memory.
  * session_turn holds only redacted user/assistant CONVERSATIONAL turns, written by a SEPARATE path
    (/session/ingest) with its own indexing. The autolearn extractor instead cites tagged transcript
    SPANS — which include agent-reasoning (thinking), tool-output, web-fetch and file-read spans that
    have NO session_turn row at all, and are exactly the evidence an audit must check (a web-fetch is
    the poisoning vector). So provenance points at the actual cited spans, captured as evidence here.

One row per (memory, cited span). `span_idx` is the span's transcript index within `session_id`
(source_session); `channel` is its deterministic origin (provenance.py vocab); `evidence_text` is the
scrubbed span text the fact was distilled from (spans are scrubbed BEFORE extraction, so no secret
reaches this table). `audit_support` is the deterministic support score of the memory body against
this span at synthesis time ( audit pass); NULL if not scored.

Non-RLS like the infra/skill tables (0021/0039): the table itself carries no direct row-security; the
read path (api /provenance) JOINs `memory` and reuses the memory's OWN access check, so you only ever
see provenance for a memory you may already read. Reversible: down() drops the table + indexes.
"""
VERSION = "0043"
NAME = "memory_provenance"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory_provenance (
          id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          memory_id     uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE,   -- memory.id is uuid
          session_id    text,                         -- source_session the span came from
          span_idx      int NOT NULL,                 -- the cited span's transcript index
          channel       text,                         -- human-input / agent-reasoning / web-fetch / tool-output / file-read
          span_ts       timestamptz,
          evidence_text text NOT NULL DEFAULT '',     -- the scrubbed span text distilled into the memory
          audit_support real,                         -- deterministic body<->evidence support at synthesis (0..1); NULL = unscored
          created_at    timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT memory_provenance_uniq UNIQUE (memory_id, session_id, span_idx)
        );""")
    cur.execute("CREATE INDEX IF NOT EXISTS memory_provenance_mem_idx ON memory_provenance (memory_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS memory_provenance_session_idx ON memory_provenance (session_id)")
    cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memory_provenance TO brain_app")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_provenance_session_idx")
    cur.execute("DROP INDEX IF EXISTS memory_provenance_mem_idx")
    cur.execute("DROP TABLE IF EXISTS memory_provenance")
