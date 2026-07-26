#!/usr/bin/env python3
""" graph lint — synonym-drift detector (box-ready, config-driven).

Reads the CANONICAL relation types from graph.yaml (single source of truth) and reports any
rel_type in memory_relation that is NOT canonical. A non-empty report = drift to fold back into
a canonical type (author-typed [[name|rel_type]] links are the only remaining drift source now
that the LLM classifier is enum-gated to the safe subset). Exit 0 = clean, 1 = drift found.

Run as the brain service user:  runuser -u brain -- python3 /opt/brain-db/db/graph_lint.py
"""
import os, sys, yaml, psycopg2

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph.yaml")
_DEFAULT = ["relates_to","supersedes","conflicts_with","accessed_via","runs_on","depends_on","uses"]
try:
    with open(CFG) as f:
        ont = (yaml.safe_load(f) or {}).get("ontology", {})
    canon = set(ont.get("types") or _DEFAULT)
except FileNotFoundError:
    canon = set(_DEFAULT)

conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")
cur = conn.cursor()
cur.execute("SELECT rel_type, count(*) FROM memory_relation GROUP BY rel_type ORDER BY 2 DESC")
rows = cur.fetchall()
drift = [(rt, n) for rt, n in rows if rt not in canon]
print("canonical types (graph.yaml): %s" % sorted(canon))
print("rel_types in DB: %s" % [(rt, n) for rt, n in rows])
if drift:
    print("DRIFT (non-canonical) -> fold into a canonical type:")
    for rt, n in drift:
        print("  %-24s %d" % (rt, n))
    sys.exit(1)
print("CLEAN: no synonym drift.")
sys.exit(0)
