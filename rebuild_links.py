#!/usr/bin/env python3
""" Step 3 — non-destructive rebuild of memory_relation from CURRENT memory bodies.

Clears the stale legacy edges (built once by the frozen import_legacy.py) and rebuilds
'relates_to' edges from each LIVE memory's [[wikilinks]], then an embedding nearest-neighbour
pass so no live non-provisional memory is left orphaned. NEVER touches the `memory` table —
only `memory_relation`. Run once as the brain service user (local peer auth). Reuses import_legacy's
exact name-normalisation + wikilink regex so matching is identical.
"""
import re
import psycopg2

NN = 3
conn = psycopg2.connect(dbname="brain", client_encoding="UTF8")
cur = conn.cursor()

cur.execute("SELECT count(*) FROM memory WHERE deleted_at IS NULL")
mem_before = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM memory_relation")
edges_before = cur.fetchone()[0]

cur.execute("SELECT id, name, body FROM memory WHERE deleted_at IS NULL")
rows = cur.fetchall()
norm = lambda s: s.strip().lower().replace(" ", "_").replace("-", "_")
norm2id = {norm(name): mid for mid, name, body in rows}
link_re = re.compile(r'\[\[([^\]|#]+)')

# clear edges only (NOT the memory table)
cur.execute("DELETE FROM memory_relation")

wikilink_edges = 0
for mid, name, body in rows:
    for m in set(link_re.findall(body or "")):
        tgt = norm2id.get(norm(m))
        if tgt and tgt != mid:
            cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type) "
                        "VALUES (%s,%s,'relates_to') ON CONFLICT DO NOTHING", (mid, tgt))
            wikilink_edges += cur.rowcount

# orphan pass: any live non-provisional memory with NO edge -> link 3 nearest neighbours
cur.execute("SELECT id FROM memory m WHERE deleted_at IS NULL AND embedding IS NOT NULL "
            "AND mem_tier <> 'provisional' "
            "AND NOT EXISTS (SELECT 1 FROM memory_relation r WHERE r.src_id=m.id OR r.dst_id=m.id)")
orphans = [r[0] for r in cur.fetchall()]
nn_edges = 0
for oid in orphans:
    cur.execute("SELECT id FROM memory WHERE id<>%s AND deleted_at IS NULL AND mem_tier<>'provisional' "
                "AND embedding IS NOT NULL ORDER BY embedding <=> (SELECT embedding FROM memory WHERE id=%s) "
                "LIMIT %s", (oid, oid, NN))
    for (nid,) in cur.fetchall():
        cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type) "
                    "VALUES (%s,%s,'relates_to') ON CONFLICT DO NOTHING", (oid, nid))
        nn_edges += cur.rowcount

cur.execute("SELECT count(*) FROM memory_relation")
edges_after = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM memory WHERE deleted_at IS NULL")
mem_after = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM memory m WHERE deleted_at IS NULL AND mem_tier<>'provisional' "
            "AND NOT EXISTS (SELECT 1 FROM memory_relation r WHERE r.src_id=m.id OR r.dst_id=m.id)")
orphans_remaining = cur.fetchone()[0]

conn.commit()
print("edges_before=%d edges_after=%d  wikilink_edges=%d nn_edges=%d" % (edges_before, edges_after, wikilink_edges, nn_edges))
print("orphans_found=%d orphans_remaining=%d  (remaining usually = notes with NULL embedding)" % (len(orphans), orphans_remaining))
print("memory rows: before=%d after=%d  (MUST be equal — memory table untouched)" % (mem_before, mem_after))
cur.close()
conn.close()
