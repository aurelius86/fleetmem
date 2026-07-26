#!/usr/bin/env python3
""" orphan-repair — LLM connects memories that have NO edges, to genuinely-related notes.
EDGES ONLY (created_by='orphan-repair-t453'): the memory bodies are NEVER touched (no re-embed/
re-hash/re-sign). Reversible: DELETE FROM memory_relation WHERE created_by='orphan-repair-t453'.
classify_edges.py types the new relates_to edges on its next run. Off the hot path.
  python3 repair_orphans.py --dry-run --limit 5      # print picks, write nothing
  python3 repair_orphans.py --limit 30               # apply to first 30 orphans
  python3 repair_orphans.py                           # all orphans
"""
import os, sys, json, urllib.request, collections
import psycopg2, psycopg2.extras

DRY = "--dry-run" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])
OLLAMA = os.environ.get("OLLAMA_GEN_URL", "http://127.0.0.1:11434/api/generate")
MODEL = os.environ.get("RERANK_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")
CAND_N = 8            # candidates shown to the LLM per orphan
MAX_EDGES = 5         # cap edges created per orphan
SYS = ("You connect a SOURCE note to genuinely-related notes. Given the SOURCE and a numbered list "
       "of CANDIDATES, return ONLY the numbers of candidates that are SPECIFICALLY related to the "
       "source — same system/task/decision/component, or a real dependency/cause/supersession/conflict. "
       "Be STRICT: if a candidate is merely on a broadly similar topic, DO NOT include it. "
       "Return JSON {\"related\":[numbers]} — empty list if none are specifically related.")
SCHEMA = {"type": "object", "properties": {"related": {"type": "array", "items": {"type": "integer"}}},
          "required": ["related"]}

conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"))
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("""SELECT m.id, m.name, coalesce(m.description,'') descr, left(coalesce(m.body,''),700) body
               FROM memory m
               WHERE m.deleted_at IS NULL AND m.invalid_at IS NULL AND m.name IS NOT NULL AND m.embedding IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM memory_relation r WHERE r.src_id=m.id OR r.dst_id=m.id)
               ORDER BY m.created_at DESC""")
orphans = cur.fetchall()
if LIMIT:
    orphans = orphans[:LIMIT]
print("orphans to repair: %d (model %s, dry=%s)" % (len(orphans), MODEL, DRY))

def ask(src, cands):
    lines = ["SOURCE: %s — %s" % (src["name"], src["descr"]), src["body"][:500], "", "CANDIDATES:"]
    for i, c in enumerate(cands, 1):
        lines.append("%d. %s — %s" % (i, c["name"], (c["descr"] or "")[:140]))
    payload = {"model": MODEL, "think": False, "system": SYS, "prompt": "\n".join(lines),
               "stream": False, "format": SCHEMA, "keep_alive": -1,
               "options": {"temperature": 0, "num_predict": 60}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    for _ in range(2):
        try:
            out = json.loads(urllib.request.urlopen(req, timeout=60).read()).get("response", "")
            picks = json.loads(out).get("related", [])
            return [int(x) for x in picks if isinstance(x, (int, float))]
        except Exception:
            continue
    return []

made = 0; touched = 0
for o in orphans:
    # candidates: top-N semantic neighbors + entity co-mentions, visible & not self
    cur.execute("""SELECT n.id, n.name, coalesce(n.description,'') descr
                   FROM memory n
                   WHERE n.deleted_at IS NULL AND n.invalid_at IS NULL AND n.name IS NOT NULL
                     AND n.embedding IS NOT NULL AND n.id <> %s
                   ORDER BY n.embedding <=> (SELECT embedding FROM memory WHERE id=%s) LIMIT %s""",
                (o["id"], o["id"], CAND_N))
    cands = cur.fetchall()
    if not cands:
        continue
    picks = ask(o, cands)
    picks = [p for p in picks if 1 <= p <= len(cands)][:MAX_EDGES]
    if not picks:
        continue
    touched += 1
    chosen = [cands[p-1] for p in picks]
    if DRY:
        print("• %s -> %s" % (o["name"], ", ".join(c["name"] for c in chosen)))
        continue
    for c in chosen:
        cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight) "
                    "VALUES (%s,%s,'relates_to','orphan-repair-t453','normal',1) "
                    "ON CONFLICT (src_id,dst_id,rel_type) DO NOTHING", (o["id"], c["id"]))
        made += cur.rowcount
    conn.commit()
print("orphans connected: %d ; edges created: %d%s" % (touched, made, " (dry-run: 0 written)" if DRY else ""))
cur.close(); conn.close()
