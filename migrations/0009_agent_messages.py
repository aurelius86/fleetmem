"""0008 agent inbox/chat — brain-native messaging between bodies.

Replaces the vault:8443 webhook bus for agent-to-agent notes. A message is addressed to
exactly one agent (to_agent = agent.name); the recipient reads its OWN inbox and marks
messages read. Access is by addressee (to_agent/from_agent must equal the caller), enforced
in api.py — DDL just stores it. No impersonation: the API stamps from_agent = caller.
"""
from contract import create_table

VERSION = "0009"
NAME = "agent_messages"


def up(cur):
    cur.execute(create_table("message", "structure",
        columns=[
            "from_agent text NOT NULL",
            "to_agent text NOT NULL",
            "subject text",
            "body text NOT NULL",
            "kind text NOT NULL DEFAULT 'msg'",   # msg | alert | task-handoff
            "read_at timestamptz",
        ]))
    # inbox query: recipient's unread/all, newest first
    cur.execute("CREATE INDEX IF NOT EXISTS message_inbox_idx ON message (to_agent, read_at, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS message_sent_idx ON message (from_agent, created_at DESC)")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS message")
