"""0004 viewer role — a read-all, write-nothing role for the dashboard.

The readable window (the dashboard host) must READ the whole brain but never write/approve.
Existing roles don't fit: manager=all (too much power), readonly=default-closed
(sees almost nothing). 'viewer' reads like a manager (access_where treats it as
all-rows) but every write/decide/enroll endpoint denies it. This migration only
relaxes the agent.role CHECK to admit 'viewer'.
"""
VERSION = "0004"
NAME = "viewer_role"


def up(cur):
    cur.execute("ALTER TABLE agent DROP CONSTRAINT IF EXISTS agent_role_check")
    cur.execute("ALTER TABLE agent ADD CONSTRAINT agent_role_check "
                "CHECK (role IN ('manager','worker','readonly','viewer'))")


def down(cur):
    # demote any viewer agents before tightening the constraint again
    cur.execute("UPDATE agent SET role='readonly' WHERE role='viewer'")
    cur.execute("ALTER TABLE agent DROP CONSTRAINT IF EXISTS agent_role_check")
    cur.execute("ALTER TABLE agent ADD CONSTRAINT agent_role_check "
                "CHECK (role IN ('manager','worker','readonly'))")
