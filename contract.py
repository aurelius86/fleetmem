"""Brain Table Contract — scaffolder.

Every table in the brain DB is created THROUGH this module so the Table Contract
(MASTER-PLAN) is enforced by construction, not by convention.

A table declares a KIND; the scaffolder injects that kind's required columns:

  knowledge  — facts/quarantinable content. access + provenance + temporal + soft-delete.
  structure  — durable objects (projects, tasks, agents). access + created_by + timestamps.
  system     — append-only internal logs/metadata. NO access columns, no updated_at.

Conventions: surrogate PK (uuid for knowledge/structure, identity bigint for system);
human handles (slug, T-number, name) are SEPARATE UNIQUE columns, never the identity (rule 2).
snake_case; enums as CHECK; standard timestamptz columns.
"""

import hashlib

KINDS = ("knowledge", "structure", "system")

# --- controlled vocabularies (enforced as CHECK constraints) ---
SENSITIVITY = ("public", "normal", "sensitive", "secret")
# deterministic, harness-stamped provenance channels (never LLM-guessed)
ORIGIN_CHANNELS = ("human-input", "agent-reasoning", "web-fetch",
                   "tool-output", "file-read", "legacy", "unknown")
TRUST = ("trusted", "quarantined", "rejected", "legacy")


def compute_content_hash(name, body):
    """THE ONE canonical content-hash formula for memory.content_hash — sha256 over
    (name + "\\n" + body). Every writer (api /propose, autolearn ingest gate, graduate/curate
    re-hash, provisional-table anchor; autolearn apply + extract) uses this so hash-based
    dedup (conflict.find_collisions) fires cross-path. Before there were 4 divergent
    formulas (agent+body / name+body / body-only) and dedup almost never matched across paths.
    Returns None for an empty body (nothing to dedup on). Keep this the SINGLE source."""
    if not body:
        return None
    return hashlib.sha256(((name or "") + "\n" + body).encode("utf-8")).hexdigest()


def _inlist(vals):
    return "(" + ", ".join("'%s'" % v for v in vals) + ")"


def _access_cols():
    # default-CLOSED: empty readers => only privileged/superuser path sees it
    return [
        "readers text[] NOT NULL DEFAULT '{}'",
        "sensitivity text NOT NULL DEFAULT 'normal' CHECK (sensitivity IN %s)" % _inlist(SENSITIVITY),
    ]


def _provenance_cols():
    return [
        "origin_channel text NOT NULL DEFAULT 'unknown' CHECK (origin_channel IN %s)" % _inlist(ORIGIN_CHANNELS),
        "trust text NOT NULL DEFAULT 'quarantined' CHECK (trust IN %s)" % _inlist(TRUST),
        "author_body text",        # which body authored it (agents)
        "source_session text",     # session/transcript id it traces to
        "content_hash text",       # sha256 of the canonical content (dedup + integrity)
    ]


def _temporal_cols():
    return [
        "valid_at timestamptz NOT NULL DEFAULT now()",
        "invalid_at timestamptz",  # set when superseded/invalidated (temporal logic)
    ]


def _softdelete_cols():
    return ["deleted_at timestamptz"]


def _timestamps():
    return [
        "created_at timestamptz NOT NULL DEFAULT now()",
        "updated_at timestamptz NOT NULL DEFAULT now()",
    ]


def scaffold(kind):
    """Return the contract-required column definitions (list of SQL fragments) for KIND."""
    if kind == "knowledge":
        return (["id uuid PRIMARY KEY DEFAULT gen_random_uuid()"]
                + _access_cols() + _provenance_cols() + _temporal_cols()
                + _softdelete_cols() + _timestamps())
    if kind == "structure":
        return (["id uuid PRIMARY KEY DEFAULT gen_random_uuid()"]
                + _access_cols() + ["created_by text"] + _timestamps())
    if kind == "system":
        # append-only, internal: no access cols, no soft-delete, no updated_at
        return ["id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                "created_at timestamptz NOT NULL DEFAULT now()"]
    raise ValueError("unknown KIND %r (expected one of %s)" % (kind, KINDS))


def create_table(name, kind, columns=None, constraints=None):
    """Build a CREATE TABLE that begins with the KIND's required columns,
    then the table-specific `columns`, then table-level `constraints`."""
    if kind not in KINDS:
        raise ValueError("unknown KIND %r" % kind)
    body = scaffold(kind) + list(columns or []) + list(constraints or [])
    return "CREATE TABLE IF NOT EXISTS %s (\n  %s\n);" % (name, ",\n  ".join(body))
