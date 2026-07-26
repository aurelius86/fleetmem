"""0041 dates — a real deadline concept: `task.due_at` + `project.target_date`.

Before this, the brain had NO date concept at all: `task` and `project` carried only created_at/
updated_at, so nothing could answer "what is due this week" or "what is slipping" — every date we ever
discussed lived in prose, unqueryable. surfaced this while designing the project_doc starter.

TWO DIFFERENT TYPES, deliberately:
  - task.due_at      timestamptz — a task can be due at a specific TIME.
  - project.target_date date     — a project targets a DAY; it has no meaningful hour, and a bare date
                                   cannot drift across timezones the way a timestamptz can.

NULL is the NORM, not a gap. Most homelab projects are open-ended; only the business-facing work
(cash-journal, max-vendor-invoicing, d365-agent-control, dama-syria-feasibility) carries real dates.
No default, no backfill — an invented date is worse than a blank one, because a stale date gets
repeated confidently and never flags itself as old.

Multi-date MILESTONES are deliberately NOT modelled as rows: they stay prose in the project_doc
`timeline` section, which can also hold the fuzzy drivers a column cannot ("when the supplier
confirms", "end of the financial period"). Only the single hard target date is a column.

No new GRANT is needed — the table-level grants to `brain_app` (0001/0033) already cover columns added
later. Verified end-to-end by writing a due_at through the app role, not assumed.

Reversible: down() drops both columns. Additive and idempotent (ADD COLUMN IF NOT EXISTS), so re-running
is safe and no existing row is touched.
"""
VERSION = "0041"
NAME = "dates"


def up(cur):
    cur.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS due_at timestamptz")
    cur.execute("ALTER TABLE project ADD COLUMN IF NOT EXISTS target_date date")
    # Partial indexes: the overwhelming majority of rows are NULL (no deadline), so indexing only the
    # dated ones keeps these tiny while still serving the queries that motivated the columns —
    # "what is due soon" and "what is slipping".
    cur.execute("CREATE INDEX IF NOT EXISTS task_due_at_idx "
                "ON task (due_at) WHERE due_at IS NOT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS project_target_date_idx "
                "ON project (target_date) WHERE target_date IS NOT NULL")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS project_target_date_idx")
    cur.execute("DROP INDEX IF EXISTS task_due_at_idx")
    cur.execute("ALTER TABLE project DROP COLUMN IF EXISTS target_date")
    cur.execute("ALTER TABLE task DROP COLUMN IF EXISTS due_at")
