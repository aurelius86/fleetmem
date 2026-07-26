#!/usr/bin/env python3
""" — MANUAL, source-grounded session rebuild.

  python3 rebuild_session.py <session_id>            # PREVIEW (writes nothing)
  python3 rebuild_session.py <session_id> --apply    # WRITE new memories + grounded edges

Given a session id, read its transcript and have the LLM extract the DURABLE memories AND their
relations, each grounded in a verbatim transcript quote.

--apply writes each NEW extracted memory as trust='quarantined', share_status='personal',
source_session=<sid>, origin_channel='agent-reasoning' (extracted by the rebuild LLM), tagged
'session-rebuild-t454' — i.e. the untrusted / needs-you pool, NOT auto-trusted; a manager validates
it lazily (approval-2.0). Before writing it DEDUPS each
memory vs what already exists: skip if a LIVE memory has the same name OR the same content_hash,
OR if cosine similarity to any existing memory of THIS session is >= DEDUP_COS. Then it creates the
grounded TYPED edges among the written+existing memories (created_by='session-rebuild-t454',
proposed_quote = the from-memory's grounding quote; rel_type validated against the graph.yaml
ontology, unknown -> default 'relates_to').

Fully reversible (edges-only + quarantined rows, nothing trusted is touched):
  DELETE FROM memory_relation WHERE created_by='session-rebuild-t454';
  UPDATE memory SET deleted_at=now() WHERE 'session-rebuild-t454' = ANY(tags);

Reuses autolearn.apply (build_memory_row / apply_proposal / ontology) + search.embed/vec_literal so
the embedding model, content-hash, reader defaults and INSERT shape match the rest of the brain.
"""
import os, sys, json, hashlib, urllib.request
import psycopg2, psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import search                      # pinned bge-m3 embed() + vec_literal()
from autolearn import apply as al  # build_memory_row / apply_proposal / ontology

APPLY = "--apply" in sys.argv
_pos = [a for a in sys.argv[1:] if not a.startswith("--")]
if not _pos:
    print("usage: rebuild_session.py <session_id> [--apply]"); sys.exit(64)
SID = _pos[0]
DEDUP_COS = float(os.environ.get("REBUILD_DEDUP_COS", "0.92"))
TAG = "session-rebuild-t454"          # identifier/edge-marker (unconstrained tags + edge created_by)
ORIGIN = "agent-reasoning"            # provenance vocab (memory_origin_channel_check allowlist)
ALLOWED_MTYPES = {"user", "feedback", "project", "reference", "memory"}  # memory_mtype_check allowlist

OLLAMA = os.environ.get("OLLAMA_GEN_URL", "http://127.0.0.1:11434/api/generate")
MODEL = os.environ.get("RERANK_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")
PER_TURN = 600
TOTAL_CAP = 12000

conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"))
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT role, text FROM session_turn WHERE session_id=%s ORDER BY idx", (SID,))
rows = cur.fetchall()
if not rows:
    print("no turns for session", SID); sys.exit(1)
buf, total = [], 0
for r in rows:
    t = (r["text"] or "")[:PER_TURN]
    seg = "%s: %s" % (r["role"].upper(), t)
    if total + len(seg) > TOTAL_CAP:
        break
    buf.append(seg); total += len(seg)
transcript = "\n".join(buf)

SYS = ("You extract DURABLE memories from a chat transcript and the relations between them. "
       "A durable memory = a fact / decision / preference / gotcha that stays TRUE beyond this chat "
       "and would help a FUTURE session. DROP transient noise (build steps, status updates, one-off "
       "command results, checksums). For each memory give: name (short snake_case slug), mtype "
       "(reference|feedback|project), description (one line), body (the durable fact), and "
       "quote (a SHORT verbatim substring of the transcript that grounds it). Then list RELATIONS "
       "among the memories you extracted as {from_name, to_name, rel_type} with rel_type in "
       "relates_to|depends_on|supersedes|conflicts_with|uses — only when the transcript clearly "
       "states that relation. Return JSON {\"memories\":[...],\"relations\":[...]} — be STRICT and "
       "accurate. Extract AT MOST 5 memories. Each body MUST be 1-2 short sentences "
       "(under 280 chars). Each quote under 120 chars. Prefer FEWER high-quality memories.")
SCHEMA = {"type": "object", "properties": {
    "memories": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "mtype": {"type": "string"}, "description": {"type": "string"},
        "body": {"type": "string"}, "quote": {"type": "string"}}, "required": ["name", "body", "quote"]}},
    "relations": {"type": "array", "items": {"type": "object", "properties": {
        "from_name": {"type": "string"}, "to_name": {"type": "string"}, "rel_type": {"type": "string"}}}}},
    "required": ["memories", "relations"]}

payload = {"model": MODEL, "think": False, "system": SYS, "prompt": "TRANSCRIPT:\n" + transcript,
           "stream": False, "format": SCHEMA, "keep_alive": -1,
           "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4000}}
req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
resp = json.loads(urllib.request.urlopen(req, timeout=240).read())
out = resp.get("response", "")
try:
    data = json.loads(out)
except Exception:
    print("PARSE FAIL done_reason=%s len=%d; raw tail:" % (resp.get("done_reason"), len(out)))
    print(out[-300:]); sys.exit(2)
mems = data.get("memories", []); rels = data.get("relations", [])


def _norm(s):
    return (s or "").strip().lower().replace(" ", "_").replace("-", "_")


def grounded(m):
    q = m.get("quote", "")
    return bool(q) and q[:40].lower() in transcript.lower()


# name(normalised) -> memory id  (written this run OR resolved existing-dup) ; and -> grounding quote
name_id = {}
name_quote = {_norm(m.get("name")): m.get("quote", "") for m in mems}

print("=== session %s : %d turns fed (%d chars)  [%s] ===" % (
    SID[:8], len(buf), total, "APPLY" if APPLY else "PREVIEW"))
print("--- %d MEMORIES ---" % len(mems))

if not APPLY:
    # ---- PREVIEW: print only, write nothing (byte-compatible with the original tool) ----
    for m in mems:
        print("• [%s] %s — %s  {grounded:%s}" % (
            m.get("mtype", "?"), m.get("name"),
            (m.get("description") or m.get("body", ""))[:90], grounded(m)))
    print("--- %d RELATIONS ---" % len(rels))
    for r in rels:
        print("• %s --%s--> %s" % (r.get("from_name"), r.get("rel_type"), r.get("to_name")))
    cur.close(); conn.close()
    sys.exit(0)

# ---- APPLY: dedup + write NEW quarantined/personal memories, then grounded edges ----
ont_types, default_rel = al.ontology()
written = skipped = edges = 0
try:
    for m in mems:
        name = (m.get("name") or "").strip()
        body = (m.get("body") or "").strip()
        nn = _norm(name)
        if not name or not body:
            skipped += 1; continue
        chash = hashlib.sha256((name + "\n" + body).encode("utf-8")).hexdigest()
        # 1) global exact dedup: same name OR same content_hash already live
        cur.execute("SELECT id FROM memory WHERE deleted_at IS NULL AND (name=%s OR content_hash=%s) "
                    "ORDER BY (name=%s) DESC LIMIT 1", (name, chash, name))
        hit = cur.fetchone()
        if hit:
            name_id[nn] = hit["id"]; skipped += 1
            print("• SKIP(exists) %s" % name); continue
        # 2) cosine dedup vs existing memories of THIS session
        vlit = search.vec_literal(search.embed(body))
        cur.execute("SELECT id, name, 1-(embedding <=> %s::vector) AS sim FROM memory "
                    "WHERE source_session=%s AND deleted_at IS NULL AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT 1", (vlit, SID, vlit))
        near = cur.fetchone()
        if near and near["sim"] is not None and float(near["sim"]) >= DEDUP_COS:
            name_id[nn] = near["id"]; skipped += 1
            print("• SKIP(cos %.3f~%s) %s" % (float(near["sim"]), near["name"], name)); continue
        # 3) WRITE new quarantined + personal memory
        mt = (m.get("mtype") or "reference").strip().lower()
        mt = mt if mt in ALLOWED_MTYPES else "reference"   # guard: unknown LLM mtype -> reference
        proposal = {
            "name": name, "mtype": mt,
            "description": (m.get("description") or ""), "body": body,
            "trust": "quarantined", "share_status": "personal",
            "origin_channel": ORIGIN, "source_session": SID, "content_hash": chash,
            "tags": [TAG],
        }
        new_id = al.apply_proposal(cur, proposal, embed_fn=search.embed,
                                   vec_fn=search.vec_literal, trust="quarantined")
        name_id[nn] = new_id; written += 1
        print("• WRITE %s  {grounded:%s}" % (name, grounded(m)))

    print("--- %d RELATIONS ---" % len(rels))
    for r in rels:
        fn, tn = _norm(r.get("from_name")), _norm(r.get("to_name"))
        src = name_id.get(fn); dst = name_id.get(tn)
        # resolve endpoints not in this run to a LIVE memory by normalised name
        if src is None:
            cur.execute("SELECT id FROM memory WHERE deleted_at IS NULL AND "
                        "lower(replace(replace(name,' ','_'),'-','_'))=%s LIMIT 1", (fn,))
            g = cur.fetchone(); src = g["id"] if g else None
        if dst is None:
            cur.execute("SELECT id FROM memory WHERE deleted_at IS NULL AND "
                        "lower(replace(replace(name,' ','_'),'-','_'))=%s LIMIT 1", (tn,))
            g = cur.fetchone(); dst = g["id"] if g else None
        if not src or not dst or src == dst:
            print("• skip-edge %s->%s (unresolved)" % (r.get("from_name"), r.get("to_name"))); continue
        rt = (r.get("rel_type") or "").strip().lower()
        rt = rt if rt in ont_types else default_rel
        quote = name_quote.get(fn) or ""
        cur.execute(
            "INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight,proposed_quote) "
            "VALUES (%s,%s,%s,%s,'normal',1,%s) ON CONFLICT (src_id,dst_id,rel_type) DO NOTHING",
            (src, dst, rt, TAG, quote[:240] or None))
        if cur.rowcount:
            edges += 1
            print("• EDGE %s --%s--> %s" % (r.get("from_name"), rt, r.get("to_name")))
    conn.commit()
    print("=== APPLIED: %d written, %d skipped(dup), %d edges ===" % (written, skipped, edges))
except Exception as e:
    conn.rollback()
    print("ROLLBACK — no changes written. error: %r" % e); raise
finally:
    cur.close(); conn.close()
