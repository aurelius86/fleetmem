"""0017 body_active_session — server-authoritative per-body current chat session.

The remote-MCP session id used to ride a frozen X-Brain-Session header captured at connection-open;
that connection outlives a chat, so writes stamped a STALE source_session (measured: a fresh session's
remote writes carried the PREVIOUS session's id). The reliable signal is the SessionStart hook, which
POSTs the real session_id to /bootstrap at every session start (hooks DO get CLAUDE_CODE_SESSION_ID).
This table records the latest session per body from that POST; the live write paths stamp
source_session from it (server-authoritative) instead of trusting the header. Fail-soft: no row -> the
write paths fall back to whatever the client passed. Residual: two concurrent same-body sessions ->
last-start-wins (rare; documented). Reversible.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VERSION = "0017"
NAME = "body_active_session"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS body_active_session (
            body        text PRIMARY KEY,
            session_id  text NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)


def down(cur):
    cur.execute("DROP TABLE IF EXISTS body_active_session")
