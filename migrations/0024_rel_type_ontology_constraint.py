"""0024 widen memory_relation.rel_type CHECK to the full ontology ( fix).

Found during live-testing: the rel_type CHECK constraint from 0001_core_schema only allowed
{relates_to, supersedes, conflicts_with, invalidated_by}. The 4 INFRA types the ontology defines
(accessed_via / runs_on / depends_on / uses, added by in graph.yaml) were therefore NEVER
storable — an author-typed [[name|rel_type]] infra link OR an approved review-gate edge would fail
the constraint. That's why the table holds 0 infra edges. The hardcoded constraint also contradicts
the open-ontology decision (add a domain's types via config, no
schema change). This widens it to the current full ontology so real types are storable while garbage
/ typo'd types are still rejected. If a genuinely-new domain type is added to graph.yaml later, extend
this list in a follow-up migration (a guard is worth the one-line change). Reversible.
"""
VERSION = "0024"
NAME = "rel_type_ontology_constraint"

_ALLOWED = ["relates_to", "supersedes", "conflicts_with", "invalidated_by",
            "accessed_via", "runs_on", "depends_on", "uses"]


def up(cur):
    arr = ",".join("'%s'" % t for t in _ALLOWED)   # fixed literals, no user input
    cur.execute("ALTER TABLE memory_relation DROP CONSTRAINT IF EXISTS memory_relation_rel_type_check")
    cur.execute("ALTER TABLE memory_relation ADD CONSTRAINT memory_relation_rel_type_check "
                "CHECK (rel_type = ANY(ARRAY[%s]::text[]))" % arr)


def down(cur):
    cur.execute("ALTER TABLE memory_relation DROP CONSTRAINT IF EXISTS memory_relation_rel_type_check")
    cur.execute("ALTER TABLE memory_relation ADD CONSTRAINT memory_relation_rel_type_check "
                "CHECK (rel_type = ANY(ARRAY['supersedes','conflicts_with','relates_to','invalidated_by']::text[]))")
