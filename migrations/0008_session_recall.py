"""0008 session_recall — per-session recall log for usage-based memory<->memory linking.

the operator. The brain logs which memory ids each chat session RECALLED, keyed by the Claude
Code per-chat session_id (POST /recall writes here). When a new memory is later created in that same
session, api.py's link_new_memory() reads the most-recent recalled ids for that session and links the
new memory to them ('relates_to') — so the graph builds itself from real reasoning chains. Concurrent
chats stay isolated because every row is keyed by session_id. Append-only.
"""
VERSION = "0008"
NAME = "session_recall"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_recall (
          id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          session_id  text NOT NULL,
          memory_id   uuid NOT NULL,
          recalled_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS session_recall_session_idx "
                "ON session_recall (session_id, recalled_at DESC)")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS session_recall")
