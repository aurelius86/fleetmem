"""0030 rel_type dynamic allowlist — retire the hardcoded enum CHECK; govern the type
allowlist in config (graph.yaml), not the schema.

Migration 0024 froze memory_relation.rel_type to a fixed 8-type CHECK, so adding a domain type
(e.g. a `stored_in` curated in from discover_ontology.py) required a NEW migration even
though the write path (autolearn.apply.link_explicit_refs) already validates rel_type against the
graph.yaml ontology and falls back to relates_to for anything unknown. 0024 itself flagged that the
frozen CHECK "contradicts the open-ontology decision" (keep
rel_type as free text, govern via the ontology doc + writer, NOT the schema).

This drops the enum CHECK and replaces it with a FORMAT-only guard (lowercase snake_case, starts
with a letter, 1-40 chars). The specific allowlist — WHICH snake_case types are valid — is enforced
app-side against graph.yaml (the single source), so a curated new type flows discovery -> graph.yaml
-> storable with NO migration, while garbage / injection / typo-shaped-as-garbage is still rejected
at the DB. All 7 live rel_types match the format, so ADD CONSTRAINT does not fail on existing rows.

Reversible: down() restores 0024's frozen 8-type list.
"""
VERSION = "0030"
NAME = "rel_type_dynamic_allowlist"

# 0024's frozen list — restored on down() so a rollback returns to the prior guarantee exactly.
_LEGACY_ALLOWED = ["relates_to", "supersedes", "conflicts_with", "invalidated_by",
                   "accessed_via", "runs_on", "depends_on", "uses"]

# lowercase snake_case, starts with a letter, 1-40 chars. Does NOT freeze the vocabulary.
_FORMAT_RE = r"^[a-z][a-z0-9_]{0,39}$"


def up(cur):
    cur.execute("ALTER TABLE memory_relation DROP CONSTRAINT IF EXISTS memory_relation_rel_type_check")
    cur.execute("ALTER TABLE memory_relation ADD CONSTRAINT memory_relation_rel_type_check "
                "CHECK (rel_type ~ %s)", (_FORMAT_RE,))


def down(cur):
    arr = ",".join("'%s'" % t for t in _LEGACY_ALLOWED)   # fixed literals, no user input
    cur.execute("ALTER TABLE memory_relation DROP CONSTRAINT IF EXISTS memory_relation_rel_type_check")
    cur.execute("ALTER TABLE memory_relation ADD CONSTRAINT memory_relation_rel_type_check "
                "CHECK (rel_type = ANY(ARRAY[%s]::text[]))" % arr)
