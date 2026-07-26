"""0003 enrollment — the agent self-enrollment / onboarding queue.

A would-be agent calls the open /enroll endpoint, answers a questionnaire, and lands
here as a PENDING application (KIND=knowledge → untrusted/quarantined provenance, since
the answers are applicant-supplied). the required managers must approve (two-body) before the
brain provisions an identity + token + role. The one-time enroll_secret lets the applicant
poll for and pull its package once provisioned.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contract import create_table  # noqa: E402

VERSION = "0003"
NAME = "enrollment"


def up(cur):
    cur.execute(create_table("enrollment", "knowledge", columns=[
        "proposed_name text NOT NULL",
        "purpose text",
        "agent_host text",
        "requested_role text",
        "answers jsonb NOT NULL DEFAULT '{}'::jsonb",   # applicant questionnaire answers (untrusted data)
        "csr text",                                     # applicant CSR (private key never leaves it)
        "enroll_secret_hash text NOT NULL",             # one-time secret to pull the provisioned package
        "status text NOT NULL DEFAULT 'pending' "
        "CHECK (status IN ('pending','approved','rejected','provisioned'))",
        "approved_by_manager_a boolean NOT NULL DEFAULT false",
        "approved_by_manager_b boolean NOT NULL DEFAULT false",
        "assigned_role text",
        "assigned_groups text[] NOT NULL DEFAULT '{}'",
        "decided_at timestamptz",
        "provisioned_agent_id uuid REFERENCES agent(id) ON DELETE SET NULL",
        "token_hash text",                              # hash of the issued token (plaintext delivered once)
    ]))
    cur.execute("CREATE INDEX IF NOT EXISTS enrollment_status_idx ON enrollment(status) WHERE deleted_at IS NULL")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS enrollment CASCADE")
