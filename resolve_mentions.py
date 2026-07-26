#!/usr/bin/env python3
""" Phase 3 — resolve un-bracketed entity mentions into relates_to edges.

The payoff step. For each live note, scan its PROSE (wikilinks stripped) for un-bracketed mentions
of a known entity, resolve the mention to that entity's canonical ANCHOR note, and propose a
relates_to edge (note -> anchor). Proposed edges flow through the existing classify_edges typing + review gate — nothing here is auto-promoted beyond relates_to.

3-tier resolution ladder (2026-standard):
  a. exact alias hit   — entity_alias / infra_host(name,ip) / infra_service(name), word-boundary. Deterministic.
  b. (reserved)        — bge-m3 cosine for fuzzy mentions; not needed while the curated alias set carries recall.
  c. LLM tiebreak      — when ONE alias maps to >1 entity (ambiguous), qwen3 picks the right one or ABSTAINS,
                         judging only from the sentence (verbatim-grounded). Abstain => no edge.

Only entities WITH an anchor_memory can be edge targets. Edges that already exist (any rel_type,
src->anchor) are skipped (de-dup vs the live graph). Self-edges skipped.

Run ON the brain host:
  python3 resolve_mentions.py --dry-run          # propose + print + sample, write NOTHING
  python3 resolve_mentions.py --dry-run --sample 40
  python3 resolve_mentions.py                     # INSERT proposed relates_to edges (created_by='mention-resolver')
"""
import argparse, json, os, re, sys, urllib.request, collections
import psycopg2, psycopg2.extras

OLLAMA_URL = os.environ.get("OLLAMA_GEN_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("GEN_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")
OLLAMA_TIMEOUT = int(os.environ.get("GEN_TIMEOUT", "60"))
RE_WIKI = re.compile(r"\[\[[^\]]*\]\]")
MIN_ALIAS_LEN = 4


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


def load_alias_map(cur):
    """alias(lower) -> set((entity_key, anchor_note)). Sources: entity table + infra registry.
    Only rows with a resolvable anchor note are usable as edge targets."""
    amap = collections.defaultdict(set)
    # new entity registry
    cur.execute("SELECT a.alias, e.name, e.anchor_memory FROM entity_alias a "
                "JOIN entity e ON e.name=a.entity_name WHERE e.anchor_memory IS NOT NULL")
    for r in cur.fetchall():
        amap[r["alias"].lower()].add((r["name"], r["anchor_memory"]))
    # infra hosts: name + ip -> anchor
    cur.execute("SELECT name, ip, anchor_memory FROM infra_host WHERE anchor_memory IS NOT NULL")
    for r in cur.fetchall():
        amap[r["name"].lower()].add((r["name"], r["anchor_memory"]))
        if r["ip"]:
            amap[r["ip"].lower()].add((r["name"], r["anchor_memory"]))
    # infra services
    cur.execute("SELECT name, anchor_memory FROM infra_service WHERE anchor_memory IS NOT NULL")
    for r in cur.fetchall():
        amap[r["name"].lower()].add((r["name"], r["anchor_memory"]))
    return amap


def boundary_find(prose_low, alias):
    m = re.search(r"(?<![\w.-])" + re.escape(alias) + r"(?![\w.-])", prose_low)
    return m.start() if m else -1


def llm_pick(sentence, alias, candidates):
    """Tier-c: ambiguous alias -> pick one entity_key or abstain. candidates = [entity_key,...]."""
    schema = {"type": "object", "properties": {
        "choice": {"type": "string"}, "abstain": {"type": "boolean"}}, "required": ["choice", "abstain"]}
    prompt = (
        "In the SENTENCE, the phrase %r could refer to one of these entities: %s.\n"
        "SENTENCE: %r\n\nPick the ONE it refers to here, judging ONLY from the sentence. "
        "If the sentence doesn't make it clear, set abstain=true. choice must be one of the listed entities."
        % (alias, ", ".join(candidates), sentence[:400]))
    try:
        data = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": schema,
                           "options": {"temperature": 0, "num_predict": 128}}).encode()
        req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            v = json.loads(json.loads(r.read()).get("response", "{}"))
        if v.get("abstain") or v.get("choice") not in candidates:
            return None
        return v["choice"]
    except Exception as e:
        sys.stderr.write("  ! llm_pick failed: %s\n" % e)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--no-llm", action="store_true", help="skip tier-c (count ambiguous instead)")
    args = ap.parse_args()

    conn = connect(); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    amap = load_alias_map(cur)
    print("usable aliases (with anchor): %d" % len(amap))

    # note name -> id, and existing edge set
    cur.execute("SELECT id, name, COALESCE(body,'') body FROM memory WHERE deleted_at IS NULL AND name IS NOT NULL")
    notes = cur.fetchall()
    name2id = {n["name"]: n["id"] for n in notes}
    cur.execute("SELECT s.name src, d.name dst FROM memory_relation r "
                "JOIN memory s ON s.id=r.src_id JOIN memory d ON d.id=r.dst_id")
    existing = collections.defaultdict(set)
    for r in cur.fetchall():
        existing[r["src"]].add(r["dst"])

    proposals = []       # (src, anchor, alias, entity_key, snippet)
    amb_total = 0; amb_resolved = 0
    for n in notes:
        src = n["name"]
        prose = RE_WIKI.sub("  ", n["body"])
        low = prose.lower()
        seen_anchor = set()
        for alias, cands in amap.items():
            if len(alias) < MIN_ALIAS_LEN:
                continue
            idx = boundary_find(low, alias)
            if idx == -1:
                continue
            snippet = prose[max(0, idx - 35):idx + len(alias) + 35].replace("\n", " ").strip()
            # resolve to a single (entity_key, anchor)
            if len(cands) == 1:
                ent, anchor = next(iter(cands))
            else:
                amb_total += 1
                keys = sorted({c[0] for c in cands})
                if args.no_llm:
                    continue
                pick = llm_pick(snippet, alias, keys)
                if not pick:
                    continue
                amb_resolved += 1
                anchor = dict((c[0], c[1]) for c in cands)[pick]; ent = pick
            if anchor == src or anchor in seen_anchor:
                continue
            if anchor in existing.get(src, ()):        # edge already exists -> skip
                continue
            if anchor not in name2id:                   # anchor note not live
                continue
            seen_anchor.add(anchor)
            proposals.append((src, anchor, alias, ent, snippet))

    print("PROPOSED edges (new relates_to): %d" % len(proposals))
    print("ambiguous mentions: %d  (llm-resolved %d, abstained/skipped %d)"
          % (amb_total, amb_resolved, amb_total - amb_resolved))
    print("source notes gaining edges: %d" % len({p[0] for p in proposals}))
    import random; random.seed(11)
    for src, anchor, alias, ent, snip in random.sample(proposals, min(args.sample, len(proposals))):
        print("  %-34s --(%s)--> %-34s" % (src[:34], alias, anchor[:34]))
        print("      ...%s..." % snip[:90])

    out = "/tmp/t199_phase3_proposals.json"
    with open(out, "w") as f:
        json.dump([{"src": s, "anchor": a, "alias": al, "entity": e} for s, a, al, e, _ in proposals], f, indent=1)
    print("full set -> %s" % out)

    if args.dry_run:
        print("DRY RUN — no edges written."); return
    for src, anchor, alias, ent, snip in proposals:
        cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight) "
                    "VALUES (%s,%s,'relates_to','mention-resolver','normal',1) "
                    "ON CONFLICT (src_id,dst_id,rel_type) DO NOTHING",
                    (name2id[src], name2id[anchor]))
    conn.commit()
    print("WROTE %d relates_to edges (created_by=mention-resolver)." % len(proposals))


if __name__ == "__main__":
    main()
