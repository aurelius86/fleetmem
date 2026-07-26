#!/usr/bin/env python3
"""discover_ontology.py — Stage 0 of the knowledge-graph edge pipeline.

The FRONT-END the brain was missing. classify_edges.py (Stages 2-4) applies a FIXED relation
vocabulary read from graph.yaml — a list we hand-earned from our own homelab notes. A stranger
who installs the brain for a different domain (accounting, research, archiving) has no such list,
and we refuse to hardcode one for them. This script DISCOVERS a starter relation ontology from the
user's OWN notes, following the research-verified EDC pattern (arXiv:2404.03868):

  EXTRACT      — sample note bodies; the local LLM pulls free-form (subject, relation, object)
                 triples. The relation phrase is UNCONSTRAINED (no enum) — that is the whole point
                 of discovery: we learn the vocabulary, we don't impose it.
  DEFINE       — for each distinct relation phrase the LLM writes a snake_case label + a one-line
                 NL meaning ("SOURCE ... TARGET"). Meaning, not surface form, is what we canonicalize.
  CANONICALIZE — embed the definitions (bge-m3), greedily cluster by cosine similarity to merge
                 synonyms ('hosted on' + 'runs on' + 'lives on' -> one type). With align_existing,
                 the current graph.yaml types are pre-seeded as fixed cluster anchors so discovered
                 synonyms FOLD INTO them (schema-guided alignment) instead of duplicating.
  EMIT         — write a ranked, deduped PROPOSED ontology to graph.discovered.yaml (same shape as
                 graph.yaml, each type carrying its meaning + support count). This is a STARTER the
                 user CURATES — the research explicitly refuted zero-intervention discovery; curation
                 is load-bearing. This script NEVER overwrites the live graph.yaml.

Design constraints (match classify_edges.py): Ollama-only + runs ON the brain host (self-contained); off the
hot path (batch job); config-driven via graph.yaml (box-ready, code-default fallbacks); no new Python
deps (pure-Python cosine clustering); idempotent (re-run overwrites the proposal file). Local model by
default; the LLM call is isolated so can route it to a cloud provider later.

Usage (as the brain service user):
    python3 discover_ontology.py                 # sample all live notes, emit graph.discovered.yaml
    python3 discover_ontology.py --limit 40       # only the first 40 notes (fast smoke test)
    python3 discover_ontology.py --dry-run        # run + print the proposal, write NO file
    python3 discover_ontology.py --mtypes reference,project
    python3 discover_ontology.py --out /tmp/ont.yaml
"""
import argparse
import datetime
import json
import math
import os
import re
import sys
import urllib.request

import psycopg2
import psycopg2.extras
import yaml

import llm_provider  # provider-agnostic structured-JSON call (local Ollama default)

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph.yaml")

# --- config load (mirror classify_edges.py: graph.yaml is the primary knob; code defaults hold) ---
def load_cfg(path=CFG_PATH):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

CFG = load_cfg()
ONT = (CFG.get("ontology") or {})
EXISTING_TYPES = ONT.get("types") or ["relates_to"]
DEFAULT_REL = ONT.get("default") or "relates_to"
OLL = (CFG.get("ollama") or {})
OLLAMA_GEN_URL = OLL.get("endpoint") or "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = OLL.get("model") or "qwen3:30b-a3b-instruct-2507-q4_K_M"
OLLAMA_TIMEOUT = int(OLL.get("timeout") or 60)
# embed endpoint reuses the brain's bge-m3 (same host as generation); env override, code default
EMBED_URL = os.environ.get("OLLAMA_EMBED_URL") or "http://127.0.0.1:11434/api/embed"
EMBED_MODEL = os.environ.get("EMBED_MODEL") or "bge-m3"

DISC = (CFG.get("discovery") or {})
BODY_CAP = int(DISC.get("body_char_cap") or 6000)
MAX_TRIPLES = int(DISC.get("max_triples_per_note") or 12)
# Calibrated on bge-m3, see reference note): embedding the BARE verb gloss (not a
# "SOURCE..TARGET" template — the scaffolding tokens inflate every pair to 0.8+) separates true
# synonyms (0.67-0.86) from unrelated verbs (0.48-0.60), gap ~0.07. 0.68 sits in that gap and errs
# toward UNDER-merge (a split candidate is trivial for the user to merge; a silent over-merge loses
# a type). The LLM label (Define step) is the PRIMARY canonicalizer; embeddings are the backstop.
CLUSTER_THRESHOLD = float(DISC.get("cluster_threshold") or 0.68)
MIN_SUPPORT = int(DISC.get("min_support") or 2)
ALIGN_EXISTING = bool(DISC.get("align_existing", True))
MAX_DEFINE = int(DISC.get("max_define") or 150)       # cap distinct phrases sent to Define (by support)
DEFINE_BATCH = int(DISC.get("define_batch") or 20)
EXTRACT_NPRED = int(DISC.get("extract_num_predict") or 2048)   # per-note extract token budget (tune for speed)
OUT_PATH = DISC.get("out") or os.path.join(os.path.dirname(CFG_PATH), "graph.discovered.yaml")

# NL meanings for the existing types (matches classify_edges._MEANINGS) — shown in the proposal.
_EXISTING_MEANINGS = {
    "relates_to": "SOURCE is generally associated with / see-also TARGET (the generic default)",
    "accessed_via": "SOURCE is reached through / proxied by / fronted by TARGET",
    "runs_on": "SOURCE is hosted on / runs on TARGET",
    "depends_on": "SOURCE requires TARGET to function",
    "uses": "SOURCE uses / integrates with / calls TARGET",
    "supersedes": "SOURCE replaces / is the newer version of TARGET",
    "conflicts_with": "SOURCE contradicts TARGET",
}
# BARE synonym glosses (no SOURCE/TARGET scaffolding) used as the alignment ANCHOR embeddings — a
# discovered label's gloss is compared against these to fold synonyms into the existing type.
_EXISTING_GLOSS = {
    "relates_to": "related to / associated with / see also",
    "accessed_via": "accessed via / reached through / proxied by / fronted by",
    "runs_on": "runs on / hosted on / lives on",
    "depends_on": "depends on / requires / needs to function",
    "uses": "uses / integrates with / calls",
    "supersedes": "supersedes / replaces / newer version of",
    "conflicts_with": "conflicts with / contradicts",
}


def _ollama_json(prompt, schema, num_predict=2048):
    """One structured, JSON-schema-constrained LLM call via the configured provider (local Ollama by default; OpenAI/Anthropic optional via graph.yaml `llm:`). Returns dict or None."""
    return llm_provider.llm_json(prompt, schema, num_predict=num_predict, timeout=OLLAMA_TIMEOUT)


def _gloss(label):
    """Bare verb gloss for a rel_type label — 'runs_on' -> 'runs on'. What we embed for clustering
    (calibration showed the bare gloss separates synonyms cleanly; templated defs do not)."""
    return _EXISTING_GLOSS.get(label) or (label or "").replace("_", " ")


def _embed(text):
    """One bge-m3 embedding via Ollama /api/embed. Returns list[float] or None."""
    try:
        data = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(EMBED_URL, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            d = json.loads(r.read())
        emb = d.get("embeddings") or []
        return emb[0] if emb and emb[0] else None
    except Exception as e:
        sys.stderr.write("  ! embed failed: %s\n" % e)
        return None


def _cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_WS = re.compile(r"\s+")

def _norm_phrase(p):
    """Lowercase, drop leading copulas, collapse whitespace, strip trailing punctuation — so
    'is hosted on', 'Hosted On', 'hosted on.' all fold to one surface form before counting."""
    p = (p or "").strip().lower()
    p = re.sub(r"^(is|are|was|were|be|been|being|to)\s+", "", p)
    p = _WS.sub(" ", p).strip(" .,:;-")
    return p


def _slug(label):
    """Force a candidate label into a safe snake_case rel_type token."""
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return s or "relates_to"


# ---------------------------------------------------------------- EXTRACT
def _extract_schema():
    return {"type": "object", "properties": {"triples": {"type": "array", "items": {
        "type": "object", "properties": {
            "subject": {"type": "string"}, "relation": {"type": "string"},
            "object": {"type": "string"}},
        "required": ["subject", "relation", "object"]}}}, "required": ["triples"]}


def extract_triples(name, body):
    """Open information extraction: pull explicit (subject, relation, object) triples from ONE note.
    Relation is a SHORT verb phrase, unconstrained (discovery learns the vocabulary)."""
    prompt = (
        "Extract factual relationships stated in the NOTE below as (subject, relation, object) "
        "triples. Rules:\n"
        "- Use ONLY relationships the text explicitly states — never infer from outside knowledge.\n"
        "- subject and object are the two things being related (a system, tool, concept, person, "
        "place, document...).\n"
        "- relation is a SHORT verb phrase (1-4 words) describing how subject relates to object, "
        "e.g. 'runs on', 'depends on', 'replaces', 'is fronted by', 'reports to', 'is stored in'. "
        "Keep the verb; drop articles.\n"
        "- Skip vague 'is related to' / 'see also' mentions — only real, specific relationships.\n"
        "- At most %d triples; fewer is fine. If the note states no clear relationships, return an "
        "empty list.\n\n"
        "NOTE TITLE: %s\n"
        "NOTE TEXT:\n%s\n" % (MAX_TRIPLES, name, (body or "")[:BODY_CAP])
    )
    parsed = _ollama_json(prompt, _extract_schema(), num_predict=EXTRACT_NPRED)
    if not parsed:
        return None
    out = []
    for t in (parsed.get("triples") or [])[:MAX_TRIPLES]:
        rel = _norm_phrase(t.get("relation"))
        subj = (t.get("subject") or "").strip()
        obj = (t.get("object") or "").strip()
        if rel and subj and obj and len(rel) <= 40:
            out.append((subj, rel, obj))
    return out


# ---------------------------------------------------------------- DEFINE
def _define_schema():
    return {"type": "object", "properties": {"defs": {"type": "array", "items": {
        "type": "object", "properties": {
            "phrase": {"type": "string"}, "label": {"type": "string"},
            "definition": {"type": "string"}},
        "required": ["phrase", "label", "definition"]}}}, "required": ["defs"]}


def define_phrases(phrases):
    """For a batch of relation phrases, get a snake_case label + one-line SOURCE->TARGET meaning.
    Returns {phrase -> (label, definition)}."""
    listing = "\n".join("- %s" % p for p in phrases)
    prompt = (
        "For each relation phrase below, produce:\n"
        "  label: a short snake_case identifier for the relationship type (e.g. runs_on, "
        "depends_on, stored_in, reports_to, supersedes). Reuse the SAME label for phrases meaning "
        "the same thing.\n"
        "  definition: one line of the exact form 'SOURCE <meaning> TARGET' describing the "
        "relationship from subject (SOURCE) to object (TARGET). Example: 'SOURCE is hosted on / "
        "runs on TARGET'.\n\n"
        "Relation phrases:\n%s\n" % listing
    )
    parsed = _ollama_json(prompt, _define_schema(), num_predict=2048)
    out = {}
    if not parsed:
        return out
    for d in (parsed.get("defs") or []):
        ph = _norm_phrase(d.get("phrase"))
        lab = _slug(d.get("label"))
        defn = (d.get("definition") or "").strip()
        if ph and defn:
            out[ph] = (lab, defn)
    return out


# ---------------------------------------------------------------- CANONICALIZE
class Cluster:
    __slots__ = ("label", "definition", "emb", "support", "phrases", "existing")
    def __init__(self, label, definition, emb, support, phrases, existing=False):
        self.label = label
        self.definition = definition
        self.emb = emb
        self.support = support
        self.phrases = list(phrases)
        self.existing = existing


def canonicalize(phrase_defs, support, examples):
    """Two-stage canonicalization. STAGE A (primary, LLM-driven): group phrases by the snake_case
    label the Define step assigned — the LLM is told to reuse one label per meaning, so this already
    merges most synonyms. STAGE B (backstop, embedding): greedily fold label-groups whose BARE GLOSS
    is close (cosine >= CLUSTER_THRESHOLD) into each other and into the existing ontology anchors, so
    two differently-named-but-synonymous labels still merge. Returns list[Cluster] by support desc."""
    # STAGE A — group by LLM label; keep the most-supported phrase's definition as representative
    groups = {}
    for ph, (lab, defn) in phrase_defs.items():
        g = groups.setdefault(lab, {"defn": defn, "support": 0, "phrases": [], "_max": -1})
        s = support.get(ph, 0)
        g["support"] += s
        g["phrases"].append(ph)
        if s > g["_max"]:
            g["defn"], g["_max"] = defn, s

    # STAGE B — seed existing-type anchors, then greedily merge label-groups by gloss embedding
    clusters = []
    if ALIGN_EXISTING:
        for t in EXISTING_TYPES:
            emb = _embed(_gloss(t))
            if emb:
                clusters.append(Cluster(t, _EXISTING_MEANINGS.get(t, ""), emb, 0, [], existing=True))

    for lab in sorted(groups, key=lambda l: groups[l]["support"], reverse=True):
        g = groups[lab]
        emb = _embed(_gloss(lab))
        if not emb:
            clusters.append(Cluster(lab, g["defn"], None, g["support"], g["phrases"]))
            continue
        best, best_sim = None, 0.0
        for c in clusters:
            sim = _cosine(emb, c.emb)
            if sim > best_sim:
                best, best_sim = c, sim
        # exact label match with an existing type always aligns (label is the strongest signal)
        exact = next((c for c in clusters if c.existing and c.label == lab), None)
        if exact is not None:
            exact.support += g["support"]
            exact.phrases += g["phrases"]
        elif best is not None and best_sim >= CLUSTER_THRESHOLD:
            best.support += g["support"]
            best.phrases += g["phrases"]
        else:
            clusters.append(Cluster(lab, g["defn"], emb, g["support"], g["phrases"]))
    return sorted(clusters, key=lambda c: (c.support, c.existing), reverse=True)


# ---------------------------------------------------------------- EMIT
def emit_yaml(clusters, n_notes, n_triples, dry_run, out_path):
    kept, dropped = [], []
    seen_labels = {}
    for c in clusters:
        # always keep existing types (shows coverage even at 0 support); prune weak NEW ones
        if not c.existing and c.support < MIN_SUPPORT:
            dropped.append(c)
            continue
        # de-dupe labels that collided post-clustering (fold support together)
        if c.label in seen_labels:
            seen_labels[c.label].support += c.support
            seen_labels[c.label].phrases += c.phrases
            continue
        seen_labels[c.label] = c
        kept.append(c)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append("# PROPOSED relation ontology — discovered by discover_ontology.py (Stage 0, EDC).")
    lines.append("# Generated %s from %d notes (%d triples extracted). CURATE before use:" % (
        stamp, n_notes, n_triples))
    lines.append("#   - rename/merge labels, delete noise, keep what matches your domain,")
    lines.append("#   - then copy the curated `types:` into graph.yaml's ontology block.")
    lines.append("# This file is a STARTER, never the live config — graph.yaml is untouched.")
    lines.append("# [existing] = already in graph.yaml; [new] = discovered this run. support = distinct notes.")
    lines.append("")
    lines.append("ontology:")
    lines.append("  default: %s" % DEFAULT_REL)
    lines.append("  types:")
    for c in kept:
        tag = "existing" if c.existing else "new"
        syn = ", ".join(sorted(set(c.phrases))[:6])
        lines.append("    - %-18s # [%s] support:%d  meaning: %s" % (c.label, tag, c.support, c.definition))
        if syn:
            lines.append("    #   %ssynonyms: %s" % (" " * 16, syn))
    lines.append("")
    lines.append("# --- machine-readable meanings (label -> definition) ---")
    lines.append("discovered_meanings:")
    for c in kept:
        lines.append("  %s: %s" % (c.label, json.dumps(c.definition)))
    text = "\n".join(lines) + "\n"

    print("\n=== ontology discovery %s ===" % ("(DRY RUN)" if dry_run else ""))
    print("notes sampled      : %d" % n_notes)
    print("triples extracted  : %d" % n_triples)
    print("types kept         : %d  (existing:%d  new:%d)" % (
        len(kept), sum(1 for c in kept if c.existing), sum(1 for c in kept if not c.existing)))
    print("weak types dropped : %d  (below min_support=%d)" % (len(dropped), MIN_SUPPORT))
    print("--- proposed ontology ---")
    print(text)
    if dropped:
        print("dropped (low support): %s" % ", ".join(
            "%s(%d)" % (c.label, c.support) for c in sorted(dropped, key=lambda c: c.support, reverse=True)[:20]))
    if not dry_run:
        with open(out_path, "w") as f:
            f.write(text)
        print(">>> wrote %s" % out_path)
    else:
        print(">>> DRY RUN — no file written")


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max notes to sample (0 = config/all)")
    ap.add_argument("--mtypes", default="", help="comma-list of mtypes to sample (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="run + print, write no file")
    ap.add_argument("--out", default="", help="output path (default: graph.discovered.yaml)")
    args = ap.parse_args()

    mtypes = [m.strip() for m in args.mtypes.split(",") if m.strip()] or \
             [m.strip() for m in (DISC.get("sample_mtypes") or [])]
    limit = args.limit or int(DISC.get("sample") or 0)
    out_path = args.out or OUT_PATH

    conn = connect()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        q = ("SELECT name, body FROM memory WHERE deleted_at IS NULL AND body IS NOT NULL "
             "AND length(body) > 80")
        params = []
        if mtypes:
            q += " AND mtype = ANY(%s)"
            params.append(mtypes)
        q += " ORDER BY updated_at DESC"
        if limit:
            q += " LIMIT %s"
            params.append(limit)
        cur.execute(q, params)
        notes = cur.fetchall()
    conn.close()

    print("EXTRACT: %d notes (mtypes=%s)" % (len(notes), mtypes or "all"))
    support = {}      # phrase -> distinct-note count
    examples = {}     # phrase -> sample (subj, obj)
    n_triples = 0
    failed = 0
    for i, note in enumerate(notes):
        triples = extract_triples(note["name"], note["body"])
        if triples is None:
            failed += 1
            continue
        seen_here = set()
        for subj, rel, obj in triples:
            n_triples += 1
            if rel not in seen_here:
                support[rel] = support.get(rel, 0) + 1     # count DISTINCT notes per phrase
                seen_here.add(rel)
            examples.setdefault(rel, (subj, obj))
        if (i + 1) % 25 == 0:
            sys.stderr.write("  ...extracted %d/%d notes (%d phrases so far)\n" % (
                i + 1, len(notes), len(support)))
    print("EXTRACT done: %d distinct relation phrases, %d triples, %d LLM failures" % (
        len(support), n_triples, failed))

    # DEFINE — cap to top-MAX_DEFINE phrases by support (singletons/noise beyond the cap dropped)
    top_phrases = sorted(support.keys(), key=lambda p: support[p], reverse=True)[:MAX_DEFINE]
    if len(support) > MAX_DEFINE:
        print("DEFINE: capping %d phrases -> top %d by support" % (len(support), MAX_DEFINE))
    phrase_defs = {}
    for b in range(0, len(top_phrases), DEFINE_BATCH):
        batch = top_phrases[b:b + DEFINE_BATCH]
        phrase_defs.update(define_phrases(batch))
        sys.stderr.write("  ...defined %d/%d phrases\n" % (min(b + DEFINE_BATCH, len(top_phrases)), len(top_phrases)))
    print("DEFINE done: %d phrases defined" % len(phrase_defs))

    # CANONICALIZE + EMIT
    print("CANONICALIZE: embedding definitions + clustering (threshold=%.2f, align_existing=%s)" % (
        CLUSTER_THRESHOLD, ALIGN_EXISTING))
    clusters = canonicalize(phrase_defs, support, examples)
    emit_yaml(clusters, len(notes), n_triples, args.dry_run, out_path)


if __name__ == "__main__":
    main()
