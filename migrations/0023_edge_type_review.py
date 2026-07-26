"""0023 edge-type review gate — a proposal queue for the infra relation types.

Background: the LLM edge classifier (classify_edges.py) reliably auto-promotes only the
lexically-explicit types (supersedes, conflicts_with). The 4 INFRA types (accessed_via, runs_on,
depends_on, uses) measured ~25% precision even behind the full 4-gate stack — a MODEL-CAPABILITY
ceiling, not a prompt bug. Rather than buy a bigger
local model (won't fit the model host) or silently drop these types to relates_to, we add a REVIEW GATE that
mirrors the memory-proposal queue: the classifier PROPOSES an infra edge type (only when it passes
grounding + citation + verify), a manager (managers) approves -> the edge is typed, rejects -> it
stays relates_to. Human is the final precision boost: 'LLM narrows the haystack, human confirms the
needle'.

Storage: three nullable columns ON memory_relation (no separate table). The edge KEEPS
rel_type='relates_to' while a proposal is pending, so recall/graph behaviour is unchanged until a
human approves. pending review = (proposed_type IS NOT NULL AND rel_type='relates_to'). Reversible.
"""
VERSION = "0023"
NAME = "edge_type_review"


def up(cur):
    cur.execute("ALTER TABLE memory_relation ADD COLUMN IF NOT EXISTS proposed_type text")
    cur.execute("ALTER TABLE memory_relation ADD COLUMN IF NOT EXISTS proposed_quote text")
    cur.execute("ALTER TABLE memory_relation ADD COLUMN IF NOT EXISTS proposed_at timestamptz")
    # partial index: the review queue is tiny relative to the edge table
    cur.execute("CREATE INDEX IF NOT EXISTS memory_relation_proposed ON memory_relation(proposed_at) "
                "WHERE proposed_type IS NOT NULL")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_relation_proposed")
    cur.execute("ALTER TABLE memory_relation DROP COLUMN IF EXISTS proposed_type")
    cur.execute("ALTER TABLE memory_relation DROP COLUMN IF EXISTS proposed_quote")
    cur.execute("ALTER TABLE memory_relation DROP COLUMN IF EXISTS proposed_at")
