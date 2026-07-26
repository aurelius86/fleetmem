"""0015 enrollment N-approver — replace the hardcoded approved_by_manager_a/approved_by_manager_b
booleans with an enrollment_approval child table: one row per manager decision. "Approved" is now
computed as (distinct manager 'approve' votes >= K) AND (no 'reject' votes), K=2 — which preserves
today's exact two-body security property while dropping the managers name-lock (any role=manager may
vote). Backfills the existing booleans into approve rows BEFORE dropping the columns; reversible.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contract import create_table  # noqa: E402

VERSION = "0015"
NAME = "enrollment_napprover"


def up(cur):
    cur.execute(create_table("enrollment_approval", "structure",
        columns=[
            "enrollment_id uuid NOT NULL REFERENCES enrollment(id) ON DELETE CASCADE",
            "approver text NOT NULL",                    # manager agent name (plain text = audit-survivable)
            "decision text NOT NULL DEFAULT 'approve' CHECK (decision IN ('approve','reject'))",
            "assign_role text",                          # role/groups this manager assigns at approval
            "assign_groups text[]",
        ],
        constraints=["UNIQUE (enrollment_id, approver)"]))   # one vote per manager per enrollment (idempotent)

    # backfill existing boolean approvals into approve rows BEFORE dropping the columns
    cur.execute("INSERT INTO enrollment_approval(enrollment_id, approver, decision) "
                "SELECT id, 'manager_a', 'approve' FROM enrollment WHERE approved_by_manager_a "
                "ON CONFLICT (enrollment_id, approver) DO NOTHING")
    cur.execute("INSERT INTO enrollment_approval(enrollment_id, approver, decision) "
                "SELECT id, 'manager_b', 'approve' FROM enrollment WHERE approved_by_manager_b "
                "ON CONFLICT (enrollment_id, approver) DO NOTHING")

    cur.execute("ALTER TABLE enrollment DROP COLUMN IF EXISTS approved_by_manager_a")
    cur.execute("ALTER TABLE enrollment DROP COLUMN IF EXISTS approved_by_manager_b")


def down(cur):
    cur.execute("ALTER TABLE enrollment ADD COLUMN IF NOT EXISTS approved_by_manager_a boolean NOT NULL DEFAULT false")
    cur.execute("ALTER TABLE enrollment ADD COLUMN IF NOT EXISTS approved_by_manager_b boolean NOT NULL DEFAULT false")
    cur.execute("UPDATE enrollment e SET approved_by_manager_a=true FROM enrollment_approval a "
                "WHERE a.enrollment_id=e.id AND a.approver='manager_a' AND a.decision='approve'")
    cur.execute("UPDATE enrollment e SET approved_by_manager_b=true FROM enrollment_approval a "
                "WHERE a.enrollment_id=e.id AND a.approver='manager_b' AND a.decision='approve'")
    cur.execute("DROP TABLE IF EXISTS enrollment_approval CASCADE")
