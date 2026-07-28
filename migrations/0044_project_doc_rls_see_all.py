"""0044 project_doc_rls_see_all — bring project_doc row-visibility in line with task/project/idea.

project_doc (added in 0033) copied its visibility rule VERBATIM from the 0032-era structure policy,
whose first clause is role-based: `app.role IN ('manager','viewer','approver')`. later moved the
structure tables (task/project/idea) OFF the role label onto an explicit `app.see_all` capability, but
project_doc — added afterwards — never got that update, leaving it the lone outlier
(rls_policy_asymmetry). This aligns it: the first clause becomes `app.see_all = 'true'`, identical to
task_sel/project_sel/idea_sel. Applies to BOTH policies 0033 built from the shared _VISIBLE template —
project_doc_sel (SELECT) and project_doc_upd (UPDATE, USING + WITH CHECK).

Impact (verified live, facts not prediction): behaviourally INERT for every current agent —
managers carry see_all=true so keep full access under both rules; workers
have see_all unset so were already excluded by the role-list and stay excluded. No viewer/approver
agents exist. It only changes a FUTURE viewer/approver created without see_all, who now correctly gets
capability-based access matching task/project instead of blanket plan access their role wouldn't grant
on tasks. The rest of the rule (author-personal, trusted+sensitivity+readers) is unchanged.
Reversible: down() restores the role-list first clause.
"""
VERSION = "0044"
NAME = "project_doc_rls_see_all"

# capability-based visibility — first clause matches task/project/idea; rest copied from 0033.
_VISIBLE_NEW = """(
  current_setting('app.see_all', true) = 'true'
  OR (created_by = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
  OR (
       share_status = 'trusted'
       AND sens_rank(sensitivity) <= sens_rank(
             (SELECT max_sensitivity FROM access_config WHERE role = current_setting('app.role', true)))
       AND ( current_setting('app.agent', true) = ANY(readers)
             OR readers && (current_setting('app.groups', true))::text[] )
     )
)"""

# the original 0033/0032-era role-based visibility (for reversibility — matches the live pre-0044 policy).
_VISIBLE_OLD = """(
  current_setting('app.role', true) IN ('manager','viewer','approver')
  OR (created_by = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
  OR (
       share_status = 'trusted'
       AND sens_rank(sensitivity) <= sens_rank(
             (SELECT max_sensitivity FROM access_config WHERE role = current_setting('app.role', true)))
       AND ( current_setting('app.agent', true) = ANY(readers)
             OR readers && (current_setting('app.groups', true))::text[] )
     )
)"""


def up(cur):
    cur.execute("ALTER POLICY project_doc_sel ON project_doc USING %s" % _VISIBLE_NEW)
    cur.execute("ALTER POLICY project_doc_upd ON project_doc USING %s WITH CHECK %s" % (_VISIBLE_NEW, _VISIBLE_NEW))


def down(cur):
    cur.execute("ALTER POLICY project_doc_sel ON project_doc USING %s" % _VISIBLE_OLD)
    cur.execute("ALTER POLICY project_doc_upd ON project_doc USING %s WITH CHECK %s" % (_VISIBLE_OLD, _VISIBLE_OLD))
