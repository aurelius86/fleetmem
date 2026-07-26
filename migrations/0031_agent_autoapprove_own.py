"""0031 agent.autoapprove_own — add the per-agent auto-approve flag that the code
already reads (api.py + autolearn: a manager whose autoapprove_own=true has its own session-end
captures applied live as trusted instead of queued).

Reported by an external tester (fleetmem v0.1.0 fresh install): the column was used but NO migration
created it, so `POST /autolearn/extract` 500'd with "column agent.autoapprove_own does not exist" on
any clean install. ADD COLUMN IF NOT EXISTS so this is also a no-op on instances that already added
the column out-of-band. Reversible: down() drops it.
"""
VERSION = "0031"
NAME = "agent_autoapprove_own"


def up(cur):
    cur.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS autoapprove_own boolean NOT NULL DEFAULT false")


def down(cur):
    cur.execute("ALTER TABLE agent DROP COLUMN IF EXISTS autoapprove_own")
