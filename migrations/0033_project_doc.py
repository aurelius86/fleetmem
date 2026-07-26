"""0033 project_doc — sectioned living per-project design plan + task.plan_section link.

Gives every project a SECTIONED living design document ("the project plan", distinct from a task's
throwaway execution plan). One row per SECTION, addressed by (project_id, section_key), so an edit
touches ONE row — never a load-rewrite of the whole document. This is the storage behind the
convention (project plan vs execution plan): when a feature's flow is
discussed, the owning section is updated in place.

Why a table and not a `plan` TEXT column on project: a single column forces every edit to reload +
rewrite the entire doc (wasteful, clobber-prone) and, if put in project.description, bloats every
project listing. Rows give surgical edits, ordered rendering, and make the `invariant` rows a
machine-readable source for the drift-check.

Access columns (readers[]/sensitivity/share_status/created_by) mirror the structure tables
(task/project/idea), so RLS reuses 0032's policies VERBATIM (created_by = owner column). No new
security surface — the plan inherits the same visibility rules as the project it belongs to.

Also adds task.plan_section: an OPTIONAL, NULLABLE soft pointer from a task to the project_doc
section it builds/changes. Deliberately NOT a foreign key — a task may name a section before that
section is written (you plan the work before authoring the design), and a hard FK would block that.
Loosely validated at the app layer.

Reversible: down() drops the task column, the RLS policies, and the table (any authored sections are
lost — the plan simply reverts to "no project_doc layer").
"""
VERSION = "0033"
NAME = "project_doc"

# row is visible to the caller — copied VERBATIM from 0032 (mirrors app struct_read_where;
# created_by is the owner column).
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

# a new/updated row may only be written by a manager, or by its author as a personal row — copied
# VERBATIM from 0032.
_WRITE_CHECK = """(
  current_setting('app.role', true) = 'manager'
  OR (created_by = current_setting('app.agent', true) AND share_status = 'personal')
)"""


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS project_doc (
          id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          project_id   uuid NOT NULL REFERENCES project(id) ON DELETE CASCADE,
          section_key  text NOT NULL,                     -- surgical-edit address within the project
          title        text,                              -- human heading for the section
          kind         text NOT NULL DEFAULT 'flow'
                         CHECK (kind IN ('overview','flow','feature','invariant','note')),
          body         text,                              -- markdown for THIS section only
          position     integer NOT NULL DEFAULT 0,        -- render order
          -- access columns mirror the structure tables (task/project/idea) so RLS matches 0032
          readers      text[]  NOT NULL DEFAULT '{}',
          sensitivity  text    NOT NULL DEFAULT 'normal',
          share_status text    NOT NULL DEFAULT 'trusted',
          created_by   text,
          created_at   timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now(),
          UNIQUE (project_id, section_key)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS project_doc_project_idx ON project_doc (project_id, position)")

    # optional soft link: which plan section a task builds/changes (NOT an FK — section may not exist yet)
    cur.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS plan_section text")

    # RLS mirroring 0032 (NO FORCE — owner bypasses so migrations/hygiene jobs are unaffected)
    cur.execute("ALTER TABLE project_doc ENABLE ROW LEVEL SECURITY")
    cur.execute("CREATE POLICY project_doc_sel ON project_doc FOR SELECT USING %s" % _VISIBLE)
    cur.execute("CREATE POLICY project_doc_ins ON project_doc FOR INSERT WITH CHECK %s" % _WRITE_CHECK)
    # UPDATE authorized by visibility (app rule), not authorship — matches 0032's structure policy.
    cur.execute("CREATE POLICY project_doc_upd ON project_doc FOR UPDATE USING %s WITH CHECK %s" % (_VISIBLE, _VISIBLE))
    cur.execute("CREATE POLICY project_doc_del ON project_doc FOR DELETE USING (current_setting('app.role', true) = 'manager')")

    # non-owner app role grants (guarded so a fresh clone without the role still applies)
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON project_doc TO brain_app; "
                "END IF; END $$")


def down(cur):
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "REVOKE ALL ON project_doc FROM brain_app; END IF; END $$")
    for cmd in ("sel", "ins", "upd", "del"):
        cur.execute("DROP POLICY IF EXISTS project_doc_%s ON project_doc" % cmd)
    cur.execute("ALTER TABLE project_doc DISABLE ROW LEVEL SECURITY")
    cur.execute("ALTER TABLE task DROP COLUMN IF EXISTS plan_section")
    cur.execute("DROP TABLE IF EXISTS project_doc")
