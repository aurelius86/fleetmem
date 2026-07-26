"""0036 structure RLS: swap see-all from the manager ROLE to the access_scope.see_all capability.

Companion to 0035, which moved MEMORY's mem_sel off the manager role onto the `app.see_all`
capability (stamped by db() from access_scope.see_all) so a memory can be scoped to a SUBSET of
managers. 0035 did NOT touch the STRUCTURE tables (task/project/idea): their 0032 _VISIBLE predicate
(used by both _sel and _upd) still grants see-all by ROLE —
`current_setting('app.role') IN ('manager','viewer','approver')`.

Consequence (latent, not live-broken): the whole point of the capability model — issue a manager token
with see_all=false and have it restricted to its readers/groups — is honored for memory but NOT for
task/project/idea at the DB layer; any manager/viewer/approver role still sees every structure row via
RLS regardless of the capability. Harmless TODAY only because the manager bodies carry BOTH role=manager AND
see_all=true (0035's up() stamped see_all=true on every manager/viewer/approver agent), and
viewer/approver aren't used for scoped structure reads. But it's a security-model inconsistency that
must be reconciled before relying on capability-scoped structure visibility.

Fix: rewrite 0032's _VISIBLE first branch to read the see_all capability the SAME way mem_sel does
(`current_setting('app.see_all', true) = 'true'`), then re-create the _sel and _upd policies on all
three tables. The _ins policy is unchanged — its _WRITE_CHECK is manager WRITE authority (who may
create rows), a separate concern from see-all READ visibility. This matches the app layer, whose
access_where (api.py) already keys see-all on scope.see_all — so RLS stops being the lone
role-based holdout.

Behaviour preserved: every agent that has see-all today already carries see_all=true (0035), so the
visible set is identical; only a NEW manager-role token with see_all=false changes — it now respects
readers/groups for structure exactly as it already does for memory.

Reversible: down() restores the role-based _VISIBLE from 0032.
"""
VERSION = "0036"
NAME = "structure_see_all_capability"

_TABLES = ("task", "project", "idea")

# NEW: see-all keyed on the access_scope.see_all capability (mirrors 0035 mem_sel, line 1).
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

# OLD: the role-based see-all from 0032 (restored by down()).
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


def _swap(cur, visible):
    """Re-create the SELECT + UPDATE policies on each structure table with the given visibility.
    _ins (WRITE_CHECK, manager write authority) and _del (manager-only) are untouched."""
    for t in _TABLES:
        cur.execute("DROP POLICY IF EXISTS %s_sel ON %s" % (t, t))
        cur.execute("CREATE POLICY %s_sel ON %s FOR SELECT USING %s" % (t, t, visible))
        cur.execute("DROP POLICY IF EXISTS %s_upd ON %s" % (t, t))
        # UPDATE authorized by visibility (app rule), not authorship — see 0032 docstring.
        cur.execute("CREATE POLICY %s_upd ON %s FOR UPDATE USING %s WITH CHECK %s" % (t, t, visible, visible))


def up(cur):
    _swap(cur, _VISIBLE_NEW)


def down(cur):
    _swap(cur, _VISIBLE_OLD)
