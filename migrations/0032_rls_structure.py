"""0032 RLS for the structure content tables (task/project/idea) + scope brain_app DELETE grants.

Closes the audit gap "RLS only protects `memory`; task/project/idea rely solely on the app layer".
These three carry the SAME access columns as memory (readers[]/sensitivity/share_status) but with
`created_by` as the owner column, so the policies mirror 0020's `mem_*` with created_by substituted.

Two deliberate differences from memory:
  * SELECT/UPDATE "see all" covers manager/viewer/approver (matches app-layer access_where, which
    grants those three every row) — so RLS never blocks a read the app intends. (mem_sel is stricter,
    a memory-only quirk; not replicated here.)
  * UPDATE USING == the SELECT visibility (not author-only), because the app authorizes a structure
    UPDATE by "can you SEE it" (struct_read_where), e.g. a worker moving a visible task to in-progress.
    A memory-style author-only UPDATE policy would wrongly block that.

DELETE is manager-only (the sole app delete path is manager-gated structure_decide on ready_to_share).

Also scopes brain_app's DELETE privilege: the install grants DELETE on ALL tables, but the app never
hard-deletes memory/agent/enrollment/enrollment_approval/lesson/message/proposal (memory is
soft-deleted via UPDATE; the rest are update-only/append-only). Revoke DELETE there — least privilege,
verified against every `DELETE FROM` in the app. NO FORCE anywhere: the owner still bypasses so
migrations/hygiene jobs (which connect as the owner) are unaffected.

Reversible: down() drops the policies, disables RLS, and re-grants the revoked DELETEs.
"""
VERSION = "0032"
NAME = "rls_structure"

_TABLES = ("task", "project", "idea")
# app never issues a hard DELETE on these (verified via grep of every `DELETE FROM`): memory is
# soft-deleted (UPDATE deleted_at); the rest are update-only / append-only.
_NO_DELETE = ("memory", "agent", "enrollment", "enrollment_approval", "lesson", "message", "proposal")

# row is visible to the caller (mirrors app struct_read_where; created_by is the owner column)
_VISIBLE = """(
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

# a new/updated row may only be written by a manager, or by its author as a personal row
_WRITE_CHECK = """(
  current_setting('app.role', true) = 'manager'
  OR (created_by = current_setting('app.agent', true) AND share_status = 'personal')
)"""


def up(cur):
    for t in _TABLES:
        cur.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % t)   # NO FORCE (owner bypasses)
        cur.execute("CREATE POLICY %s_sel ON %s FOR SELECT USING %s" % (t, t, _VISIBLE))
        cur.execute("CREATE POLICY %s_ins ON %s FOR INSERT WITH CHECK %s" % (t, t, _WRITE_CHECK))
        # UPDATE authorized by visibility (app rule), not authorship — see module docstring.
        cur.execute("CREATE POLICY %s_upd ON %s FOR UPDATE USING %s WITH CHECK %s" % (t, t, _VISIBLE, _VISIBLE))
        cur.execute("CREATE POLICY %s_del ON %s FOR DELETE USING (current_setting('app.role', true) = 'manager')" % (t, t))

    # scope brain_app privileges: keep SELECT/INSERT/UPDATE/DELETE on the three RLS tables (RLS narrows
    # each), and REVOKE DELETE where the app never hard-deletes. Guarded so a fresh clone without the
    # role yet still applies.
    cur.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
        "  GRANT SELECT, INSERT, UPDATE, DELETE ON task, project, idea TO brain_app; "
        "  REVOKE DELETE ON %s FROM brain_app; "
        "END IF; END $$" % ", ".join(_NO_DELETE))


def down(cur):
    for t in _TABLES:
        for cmd in ("sel", "ins", "upd", "del"):
            cur.execute("DROP POLICY IF EXISTS %s_%s ON %s" % (t, cmd, t))
        cur.execute("ALTER TABLE %s DISABLE ROW LEVEL SECURITY" % t)
    cur.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
        "  GRANT DELETE ON %s TO brain_app; "
        "END IF; END $$" % ", ".join(_NO_DELETE))
