"""0012 sessions — chat-transcript store in the brain ( / brain-transcripts-consolidation P1).

Moves transcripts into the ONE governed brain (Postgres, the brain host) so nothing transcript-related
stays on the old the legacy host stack. `session` = one row per Claude Code chat (keyed by source_session,
the same id memories carry, so a memory links to the chat that produced it). `session_turn` = the
redacted user/assistant turns, with FTS (tsv) + a bge-m3 embedding (vector(1024), matches memory)
for brain-native hybrid search. Access-gated like memory (sensitivity + readers + soft-delete):
raw chat is secret-bearing, so P4's read path applies the same role filter. Additive + reversible."""
VERSION = "0012"
NAME = "sessions"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          source_session text UNIQUE,                 -- Claude Code per-chat uuid (== memory.source_session)
          agent_body    text,                         -- an agent name
          project       text,
          title         text,
          started_at    timestamptz,
          ended_at      timestamptz,
          turn_count    int NOT NULL DEFAULT 0,
          sensitivity   text NOT NULL DEFAULT 'normal',
          readers       text[] NOT NULL DEFAULT '{}',
          origin_channel text DEFAULT 'chat-archive',
          created_at    timestamptz NOT NULL DEFAULT now(),
          deleted_at    timestamptz
        );""")
    cur.execute("CREATE INDEX IF NOT EXISTS session_agent_idx ON session (agent_body, started_at DESC)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_turn (
          id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          session_id uuid NOT NULL REFERENCES session(id) ON DELETE CASCADE,
          idx        int NOT NULL,
          role       text,
          ts         timestamptz,
          text       text NOT NULL DEFAULT '',
          tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(text,''))) STORED,
          embedding  vector(1024)
        );""")
    cur.execute("CREATE INDEX IF NOT EXISTS session_turn_sid_idx ON session_turn (session_id, idx)")
    cur.execute("CREATE INDEX IF NOT EXISTS session_turn_tsv_idx ON session_turn USING gin (tsv)")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS session_turn")
    cur.execute("DROP TABLE IF EXISTS session")
