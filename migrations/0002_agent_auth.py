"""0002 agent auth — token + role columns on the agent table.

Adds the application-layer auth fields used by the governance API: a stored token
HASH (never plaintext), a non-secret token prefix for logging, instant-revoke
timestamp, and a CHECK-constrained role. cert_cn already exists from 0001 (the
mTLS transport identity); a request must satisfy BOTH cert_cn and token.
"""
VERSION = "0002"
NAME = "agent_auth"


def up(cur):
    cur.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS token_hash text")
    cur.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS token_prefix text")
    cur.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS revoked_at timestamptz")
    cur.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'readonly' "
                "CHECK (role IN ('manager','worker','readonly'))")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_token_hash_uniq "
                "ON agent(token_hash) WHERE token_hash IS NOT NULL")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS agent_token_hash_uniq")
    for col in ("token_hash", "token_prefix", "revoked_at", "role"):
        cur.execute("ALTER TABLE agent DROP COLUMN IF EXISTS %s" % col)
