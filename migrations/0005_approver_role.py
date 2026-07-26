"""0005 approver role — a read-all, decide-only role for the dashboard's /approve view.

The readable window (the dashboard host) is a 'viewer' (read-all, write-nothing). Phase D needs
the operator's Keep/Drop verdicts to land in the live proposal table, but deciding is a
governance WRITE that 'viewer' is denied. Rather than hand the web box a 'manager'
token (full brain write power = the blast radius the viewer design avoids), 'approver'
is the minimal widening: it reads like a manager (access_where treats it as all-rows)
and may decide proposals, but every OTHER write — propose, enroll, memory — stays denied
(an approver still cannot author the proposals it judges, so author != validator holds).
This migration only relaxes the agent.role CHECK to admit 'approver'.
"""
VERSION = "0005"
NAME = "approver_role"


def up(cur):
    cur.execute("ALTER TABLE agent DROP CONSTRAINT IF EXISTS agent_role_check")
    cur.execute("ALTER TABLE agent ADD CONSTRAINT agent_role_check "
                "CHECK (role IN ('manager','worker','readonly','viewer','approver'))")


def down(cur):
    # demote any approver agents to viewer before tightening the constraint again
    cur.execute("UPDATE agent SET role='viewer' WHERE role='approver'")
    cur.execute("ALTER TABLE agent DROP CONSTRAINT IF EXISTS agent_role_check")
    cur.execute("ALTER TABLE agent ADD CONSTRAINT agent_role_check "
                "CHECK (role IN ('manager','worker','readonly','viewer'))")
