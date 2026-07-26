"""0035 decouple see-all from the manager ROLE -> an explicit access_scope.see_all capability.

Before: the memory SELECT policy (mem_sel, migration 0020) gave EVERY manager see-all
(`app.role = 'manager'`), so a memory could NOT be scoped to a subset of managers — all managers
saw every trusted row. the operator's requirement: "share per agent and per group; even with 10 managers,
share with only 2."

After: the see-all branch keys on `app.see_all = 'true'` (stamped by db() from the agent's
access_scope.see_all). A privileged agent WITHOUT see_all falls through to the normal readers match
(`app.agent = ANY(readers) OR readers && app.groups`) with the per-role sensitivity ceiling — so a
memory scoped to readers=['mgrA','mgrB'] is invisible to a third manager that lacks see_all.

To PRESERVE current behaviour, every agent that has see-all today (role manager/viewer/approver) is
granted access_scope.see_all=true. NEW agents default to false (enrollment does not set it), so they
respect readers — subset scoping works out of the box. The explicit review/audit routes
(/provisional/pending, /personal/inspect) are separate manager-gated queries and are unaffected.

Reversible: down() restores the role-based see-all policy (leaves the see_all flags, which are inert
under the old policy).
"""
VERSION = "0035"
NAME = "see_all_capability"

_NEW = """
CREATE POLICY mem_sel ON memory FOR SELECT USING (
  current_setting('app.see_all', true) = 'true'
  OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
  OR (
       share_status = 'trusted'
       AND sens_rank(sensitivity) <= sens_rank(
             (SELECT max_sensitivity FROM access_config WHERE role = current_setting('app.role', true)))
       AND ( current_setting('app.agent', true) = ANY(readers)
             OR readers && (current_setting('app.groups', true))::text[] )
     )
  OR (
       current_setting('app.role', true) = 'organizer'
       AND share_status IN ('trusted','ready_to_share')
       AND sens_rank(sensitivity) <= sens_rank(
             (SELECT max_sensitivity FROM access_config WHERE role = 'organizer'))
     )
)
"""

_OLD = """
CREATE POLICY mem_sel ON memory FOR SELECT USING (
  current_setting('app.role', true) = 'manager'
  OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
  OR (
       share_status = 'trusted'
       AND sens_rank(sensitivity) <= sens_rank(
             (SELECT max_sensitivity FROM access_config WHERE role = current_setting('app.role', true)))
       AND ( current_setting('app.agent', true) = ANY(readers)
             OR readers && (current_setting('app.groups', true))::text[] )
     )
  OR (
       current_setting('app.role', true) = 'organizer'
       AND share_status IN ('trusted','ready_to_share')
       AND sens_rank(sensitivity) <= sens_rank(
             (SELECT max_sensitivity FROM access_config WHERE role = 'organizer'))
     )
)
"""


def up(cur):
    # 1. preserve current see-all for every agent that has it today, BEFORE swapping the policy.
    cur.execute("UPDATE agent SET access_scope = jsonb_set(COALESCE(access_scope,'{}'::jsonb),'{see_all}','true'::jsonb) "
                "WHERE role IN ('manager','viewer','approver') "
                "AND COALESCE((access_scope->>'see_all')::boolean, false) = false")
    # 2. swap the SELECT policy to the see_all-capability form.
    cur.execute("DROP POLICY IF EXISTS mem_sel ON memory")
    cur.execute(_NEW)


def down(cur):
    cur.execute("DROP POLICY IF EXISTS mem_sel ON memory")
    cur.execute(_OLD)
