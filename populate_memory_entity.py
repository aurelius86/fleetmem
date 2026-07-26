#!/usr/bin/env python3
""" — populate memory_entity from curated entity registry + deterministic domain patterns.
Idempotent (ON CONFLICT). Reversible: TRUNCATE memory_entity. --dry-run counts only."""
import os, re, sys, collections
import psycopg2, psycopg2.extras
DRY = "--dry-run" in sys.argv
conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE","brain"))
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
# curated vocabulary: alias(lower)->(canonical name, kind)
vocab = {}
cur.execute("SELECT lower(a.alias) al, e.name, e.kind FROM entity_alias a JOIN entity e ON e.name=a.entity_name")
for r in cur.fetchall(): vocab.setdefault(r["al"], (r["name"], r["kind"]))
cur.execute("SELECT lower(name) al, name, kind FROM entity")
for r in cur.fetchall(): vocab.setdefault(r["al"], (r["name"], r["kind"]))
curated = sorted([a for a in vocab if len(a) >= 4], key=len, reverse=True)
cre = re.compile(r"\b(" + "|".join(re.escape(a) for a in curated) + r")\b", re.I) if curated else None
PAT = [(re.compile(r"\bT(\d{1,4})\b"), lambda m: ("t"+m.group(1), "ref")),
       (re.compile(r"\bLXC[ ]?(\d{2,3})\b", re.I), lambda m: ("lxc"+m.group(1), "ref")),
       (re.compile(r"\bPC([12])\b"), lambda m: ("pc"+m.group(1), "ref")),
       (re.compile(r"\b([a-z_]+\.py)\b"), lambda m: (m.group(1).lower(), "ref"))]
cur.execute("SELECT m.id, coalesce(m.name,'')||' '||coalesce(m.description,'')||' '||coalesce(m.body,'') txt "
            "FROM memory m WHERE m.deleted_at IS NULL AND m.invalid_at IS NULL")
rows = cur.fetchall()
pairs = []   # (memory_id, entity_name, kind, mentions)
for r in rows:
    cnt = collections.Counter(); kind = {}; t = r["txt"]
    if cre:
        for m in cre.finditer(t):
            nm, kd = vocab[m.group(1).lower()]; cnt[nm]+=1; kind[nm]=kd
    for rx, fn in PAT:
        for m in rx.finditer(t):
            nm, kd = fn(m); cnt[nm]+=1; kind[nm]=kd
    for nm, c in cnt.items():
        pairs.append((r["id"], nm, kind.get(nm,"other"), c))
print("memories:%d  entity-mention pairs:%d  distinct entities:%d" % (
      len(rows), len(pairs), len(set(p[1] for p in pairs))))
if DRY:
    print("(dry-run)"); sys.exit(0)
psycopg2.extras.execute_values(cur,
    "INSERT INTO memory_entity(memory_id,entity_name,kind,mentions,source) VALUES %s "
    "ON CONFLICT (memory_id,entity_name) DO UPDATE SET mentions=EXCLUDED.mentions",
    [(a,b,c,d,"deterministic") for (a,b,c,d) in pairs])
conn.commit()
cur.execute("SELECT count(*) n FROM memory_entity"); print("memory_entity rows now:", cur.fetchone()["n"])
cur.close(); conn.close()
