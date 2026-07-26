#!/usr/bin/env python3
""" Phase 2 — LLM entity seeding for the `entity` / `entity_alias` registry (0029).

The 2026-standard step: a local LLM (qwen3:30b @ the model host Ollama) turns raw recurring surface forms
into a clean ENTITY registry — deciding which candidates are REAL entities (not generic concepts),
merging their alias variants, choosing each one's canonical anchor note. Non-infra only: infra
entities (IPs, boxes, services) already live in infra_host/infra_service, so we skip them here.

Runs ON the brain host (self-contained; Ollama is the only external call, same as classify_edges.py).
  python3 seed_entities.py --dry-run            # judge + print the proposed registry, write NOTHING
  python3 seed_entities.py --dry-run --min 4    # only candidates recurring in >=4 notes
  python3 seed_entities.py                       # seed entity + entity_alias (+ bge-m3 embeddings)

Idempotent: entity upserts on name, aliases upsert on (alias, entity_name). Re-runnable.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import collections

import psycopg2
import psycopg2.extras

from search import embed, vec_literal   # bge-m3 embedder (reused, pinned)

OLLAMA_URL = os.environ.get("OLLAMA_GEN_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("GEN_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")
OLLAMA_TIMEOUT = int(os.environ.get("GEN_TIMEOUT", "90"))
KINDS = ["body", "person", "project", "service", "model", "tool", "concept", "org", "other"]

RE_IP    = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
RE_BOXID = re.compile(r"\b(?:LXC|CT|PC)\s?\d{1,4}\b", re.I)
RE_HOST  = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+){1,3}\b")
RE_WIKI  = re.compile(r"\[\[[^\]]*\]\]")
ENGLISH = {"read-only","end-to-end","built-in","add-on","per-host","fleet-wide","drop-in",
           "self-signed","re-embed","top-level","comma-separated","user-agent","non-openai",
           "site-packages","interactive-login","well-known","real-time","long-lived","re-propose",
           "high-level","low-level","free-tier","reverse-proxy","lead-in","first-class",
           "employee-visible","llm-identity","cross-reference","up-to-date","use-case","use-cases"}


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


def norm(s):
    return re.sub(r"[\s\-_]", "", (s or "").lower())


def build_candidates(cur, min_freq):
    """Return [{term, freq, aliases[], anchors[], contexts[]}] for non-infra recurring surface forms."""
    cur.execute("SELECT id, name, COALESCE(description,'') d, COALESCE(body,'') b "
                "FROM memory WHERE deleted_at IS NULL AND name IS NOT NULL")
    notes = cur.fetchall()
    names_norm = {norm(re.sub(r"^[a-z]+_", "", n["name"])): n["name"] for n in notes}

    mentions = collections.defaultdict(set)      # surface form -> {note names}
    ctx = collections.defaultdict(list)          # surface form -> [snippet,...]
    for n in notes:
        prose = RE_WIKI.sub("  ", n["b"])
        low = prose.lower()
        forms = {t for t in RE_HOST.findall(low) if len(t) >= 6 and t not in ENGLISH}
        for sf in forms:
            mentions[sf].add(n["name"])
            if len(ctx[sf]) < 3:
                i = low.find(sf)
                ctx[sf].append(prose[max(0, i - 40):i + len(sf) + 40].replace("\n", " ").strip())

    # cluster variants by normalized key
    clusters = collections.defaultdict(lambda: {"forms": set(), "notes": set(), "ctx": []})
    for sf, ns in mentions.items():
        k = norm(sf)
        c = clusters[k]
        c["forms"].add(sf); c["notes"] |= ns
        if len(c["ctx"]) < 3:
            c["ctx"] += ctx[sf][:3 - len(c["ctx"])]

    out = []
    for k, c in clusters.items():
        if len(c["notes"]) < min_freq:
            continue
        # anchor candidates: notes whose slug contains this key
        anchors = [nm for kk, nm in names_norm.items() if k and (k in kk or kk in k)][:5]
        out.append({"term": sorted(c["forms"])[0], "key": k, "freq": len(c["notes"]),
                    "aliases": sorted(c["forms"]), "anchors": anchors, "contexts": c["ctx"][:3]})
    out.sort(key=lambda x: -x["freq"])
    return out


def _schema():
    return {"type": "object", "properties": {"results": {"type": "array", "items": {
        "type": "object", "properties": {
            "n": {"type": "integer"},
            "is_entity": {"type": "boolean"},
            "canonical_name": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "anchor_note": {"type": "string"},
        }, "required": ["n", "is_entity", "canonical_name", "kind", "anchor_note"]}}},
        "required": ["results"]}


def judge_batch(batch):
    """One structured qwen3 call over a batch of candidates. Returns {n -> verdict} or {} on failure."""
    lines = []
    for i, c in enumerate(batch):
        anc = ", ".join(c["anchors"]) or "(none)"
        ex = " | ".join(c["contexts"]) or "(no context)"
        lines.append("%d. term=%r  seen in %d notes\n   candidate anchor notes: %s\n   contexts: %s"
                     % (i + 1, c["term"], c["freq"], anc, ex[:400]))
    prompt = (
        "You are curating a knowledge-graph ENTITY registry for a homelab 'brain'. Each item below is a "
        "recurring phrase found in notes. Decide, JUDGING ONLY FROM THE CONTEXTS SHOWN:\n"
        "1. is_entity: true only if the phrase names a SPECIFIC durable THING — a service/tool/model/"
        "project/body/person/org (e.g. a program, a model name, a named project). false for generic "
        "concepts, verbs, adjectives, or vague jargon (e.g. 'read only', 'per host', 'high level').\n"
        "2. canonical_name: a short lowercase slug for the entity (hyphens ok), e.g. 'my-service'. Use the "
        "SAME canonical_name for variants of the one thing so they merge.\n"
        "3. kind: one of body,person,project,service,model,tool,concept,org,other.\n"
        "4. anchor_note: pick the ONE candidate anchor note that is MOST about this entity, or \"\" if "
        "none fits. Choose only from the listed candidate anchor notes.\n"
        "Most short generic phrases are NOT entities — it is correct for many answers to be is_entity=false.\n\n"
        "ITEMS:\n%s\n\nReturn one result object per item by its number n." % "\n".join(lines))
    try:
        data = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                           "format": _schema(),
                           "options": {"temperature": 0, "num_predict": 2048}}).encode()
        req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            parsed = json.loads(json.loads(r.read()).get("response", "{}"))
    except Exception as e:
        sys.stderr.write("  ! judge batch failed: %s\n" % e)
        return {}
    out = {}
    for v in (parsed.get("results") or []):
        try:
            out[int(v["n"]) - 1] = v
        except (KeyError, TypeError, ValueError):
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min", type=int, default=3, help="min note-frequency for a candidate")
    ap.add_argument("--limit", type=int, default=0, help="cap candidates (0=all)")
    ap.add_argument("--batch", type=int, default=12)
    args = ap.parse_args()

    conn = connect(); conn.autocommit = False
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cands = build_candidates(cur, args.min)
    if args.limit:
        cands = cands[:args.limit]
    print("candidates (freq>=%d, non-infra): %d" % (args.min, len(cands)))

    # judge in batches -> merge by canonical_name
    reg = {}   # canonical_name -> {kind, anchor, aliases:set, freq}
    rejected = []
    for i in range(0, len(cands), args.batch):
        batch = cands[i:i + args.batch]
        verdicts = judge_batch(batch)
        for j, c in enumerate(batch):
            v = verdicts.get(j)
            if not v or not v.get("is_entity"):
                rejected.append(c["term"]); continue
            cn = (v.get("canonical_name") or c["key"]).strip().lower()
            kind = v.get("kind") if v.get("kind") in KINDS else "other"
            anchor = (v.get("anchor_note") or "").strip()
            e = reg.setdefault(cn, {"kind": kind, "anchor": "", "aliases": set(), "freq": 0})
            e["aliases"] |= set(c["aliases"]); e["freq"] += c["freq"]
            if anchor and anchor in c["anchors"] and not e["anchor"]:
                e["anchor"] = anchor
        print("  judged %d/%d ..." % (min(i + args.batch, len(cands)), len(cands)))

    print("\n=== ENTITY REGISTRY (LLM-curated) ===")
    print("accepted entities : %d" % len(reg))
    print("rejected (concepts): %d" % len(rejected))
    tot_alias = sum(len(e["aliases"]) for e in reg.values())
    print("aliases total      : %d" % tot_alias)
    kc = collections.Counter(e["kind"] for e in reg.values())
    print("by kind            : %s" % dict(kc))
    for cn, e in sorted(reg.items(), key=lambda kv: -kv[1]["freq"])[:40]:
        print("  %-22s [%s] freq=%-3d anchor=%-40s aliases=%s"
              % (cn[:22], e["kind"], e["freq"], (e["anchor"] or "-")[:40], sorted(e["aliases"])[:4]))
    if rejected:
        print("\nrejected sample: %s" % ", ".join(rejected[:30]))

    if args.dry_run:
        print("\nDRY RUN — no writes.")
        return

    # seed live
    with conn.cursor() as cur:
        seeded = 0
        skipped = 0
        for cn, e in reg.items():
            if e["kind"] == "concept":            # Gap-2 filter: concepts are not entities -> skip
                skipped += 1
                continue
            desc = ""
            if e["anchor"]:
                cur.execute("SELECT COALESCE(description,'') FROM memory WHERE name=%s AND deleted_at IS NULL", (e["anchor"],))
                row = cur.fetchone(); desc = row[0] if row else ""
            vec = vec_literal(embed(cn + " " + desc))
            cur.execute(
                "INSERT INTO entity(name,kind,anchor_memory,description,embedding,embed_model) "
                "VALUES (%s,%s,%s,%s,%s::vector,%s) ON CONFLICT (name) DO UPDATE SET "
                "kind=EXCLUDED.kind, anchor_memory=COALESCE(NULLIF(EXCLUDED.anchor_memory,''),entity.anchor_memory), "
                "embedding=EXCLUDED.embedding, updated_at=now()",
                (cn, e["kind"], e["anchor"] or None, desc[:500], vec, "bge-m3"))
            for a in e["aliases"]:
                cur.execute("INSERT INTO entity_alias(alias,entity_name,source) VALUES (%s,%s,'llm') "
                            "ON CONFLICT (alias,entity_name) DO NOTHING", (a.lower(), cn))
            seeded += 1
        conn.commit()
    print("\nSEEDED %d entities + aliases (embedded with bge-m3); skipped %d concept-kind." % (seeded, skipped))


if __name__ == "__main__":
    main()
