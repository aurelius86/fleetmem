#!/usr/bin/env python3
"""classify_edges.py — the whole-note LLM pass that TYPES the knowledge-graph edges.

The write path (apply.link_explicit_refs) records author-typed [[name|rel_type]] links directly,
and leaves plain [[name]] links as `relates_to`. THIS script is the other half of the hybrid:
it reads each note that still has untyped (`relates_to`, classified_at IS NULL) edges and types
them with a LOCAL LLM — as BOTH the one-time backfill AND the ongoing safety net.

PRECISION-FIRST 3-GATE PIPELINE (research-verified). Single-pass prompting over-promotes badly
(Qwen invents a relation in ~97% of no-relation cases), so each edge passes three gates in ONE
structured call, and only survives all three to be promoted — else it stays relates_to:
  GATE 1 ABSTENTION  — model first says specific=true ONLY if the source text states a SPECIFIC
                       relation (else specific=false -> relates_to). Fixes over-promotion.
  GATE 2 CONSTRAINED — the relation label is enum-restricted to the ontology via Ollama's JSON
                       `format` schema (decode-time guarantee, not just prompt advice).
  GATE 3 GROUNDING   — model must return a verbatim supporting_quote; we REJECT the promotion
                       (fall back to relates_to) unless that quote is a literal substring of the
                       source note. Kills name-guessing + direction errors.

Design constraints (all still hold): Ollama-only + runs ON the brain host (self-contained); off the hot
path (batch job); config-driven via graph.yaml (box-ready); idempotent/resumable via classified_at;
promote-in-place with weight-merge on the (src,dst,type) unique key.

Usage (as the brain service user):
    python3 classify_edges.py            # classify the whole backlog
    python3 classify_edges.py --limit 20 # first 20 pending notes
    python3 classify_edges.py --dry-run  # classify + print (incl. rejects), write NOTHING
"""
import argparse
import json
import os
import re
import sys
import urllib.request

import psycopg2
import psycopg2.extras
import yaml

import llm_provider  # provider-agnostic structured-JSON call (local Ollama default)

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph.yaml")
_DEFAULT_ONTOLOGY = ["relates_to", "supersedes", "conflicts_with",
                     "accessed_via", "runs_on", "depends_on", "uses"]


def load_cfg(path=CFG_PATH):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


CFG = load_cfg()
ONT = (CFG.get("ontology") or {})
TYPES = ONT.get("types") or _DEFAULT_ONTOLOGY
DEFAULT_REL = ONT.get("default") or "relates_to"
SPECIFIC = [t for t in TYPES if t != DEFAULT_REL]
OLL = (CFG.get("ollama") or {})
OLLAMA_URL = OLL.get("endpoint") or "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = OLL.get("model") or "qwen3:30b-a3b-instruct-2507-q4_K_M"
OLLAMA_TIMEOUT = int(OLL.get("timeout") or 20)
CLS = (CFG.get("classifier") or {})
BODY_CAP = int(CLS.get("body_char_cap") or 6000)
MAX_LINKS = int(CLS.get("max_links_per_note") or 40)
# Types the LLM may set on its own. Measured, 6 dry-run iterations): qwen3:30b is
# reliable only on lexically-explicit relations; the infra types stay author-typed-only
# ([[name|rel_type]]) until a stronger model earns them. Missing key => all SPECIFIC (old behavior).
_AUTO = CLS.get("autopromote_types")
AUTOPROMOTE = [t for t in _AUTO if t in SPECIFIC] if _AUTO else list(SPECIFIC)
# review gate: types the LLM may PROPOSE (queued for a manager) but not auto-apply. Disjoint
# from AUTOPROMOTE (a type in both auto-promotes). Empty => infra types just stay relates_to.
_REVIEW = CLS.get("review_types") or []
REVIEW = [t for t in _REVIEW if t in SPECIFIC and t not in AUTOPROMOTE]
# make human review OPTIONAL for a shipped user. review_mode=true (OUR default) keeps the
# behaviour — REVIEW types queue for a manager. review_mode=false (shipped user with no queue)
# auto-applies a REVIEW candidate that reaches auto_apply_confidence across confidence_votes
# independent verifier lenses (corroboration = the precision floor, ODKE+ 91%->98.8%); anything
# below the threshold stays relates_to (borderline). Precision-first default: unanimous (1.0).
REVIEW_MODE = bool(CLS.get("review_mode", True))
_AAC = CLS.get("auto_apply_confidence")
AUTO_APPLY_CONF = float(_AAC) if _AAC is not None else 1.0
CONFIDENCE_VOTES = max(1, int(CLS.get("confidence_votes") or 2))

# meaning cheatsheet the model sees; only the SPECIFIC types (relates_to is the abstention default)
_MEANINGS = {
    "accessed_via": "SOURCE is reached through / proxied by / fronted by TARGET",
    "runs_on": "SOURCE is hosted on / runs on TARGET",
    "depends_on": "SOURCE requires TARGET to function",
    "uses": "SOURCE uses / integrates with / calls TARGET",
    "supersedes": "SOURCE replaces / is the newer version of TARGET",
    "conflicts_with": "SOURCE contradicts TARGET",
}


def _norm_text(s):
    """Lowercase, strip markdown emphasis, collapse whitespace — so the grounding substring check
    tolerates **bold**/`code`/spacing differences between the model's quote and the raw body."""
    s = (s or "").lower()
    for ch in "*`_#>":
        s = s.replace(ch, "")
    return " ".join(s.split())


# Words that carry NO relational meaning — a "quote" made only of these + a [[link]] is a
# cross-reference ('Links: [[X]]', 'see also [[X]]', 'Detail + sources: [[X]]'), NOT a relation.
_CITE_WORDS = {"links", "link", "linked", "see", "related", "relatedly", "relates",
               "sources", "source", "detail", "details", "tie", "refs", "ref", "also",
               "more", "background", "cf", "pairs", "pair", "the", "a", "an", "this",
               "that", "it", "its", "note", "notes"}


def _readable(name):
    """Turn a note slug into human prose for the verifier prompt (reference_example_note ->
    'example note') — the raw slug never appears in the quoted sentence, so asking the verifier
    to match the slug guarantees a false reject."""
    n = re.sub(r"^(reference|feedback|project|decision|session|user|task|inference|hook)_", "",
               name or "")
    return n.replace("_", " ")


def _is_citation_quote(quote):
    """True if the model's supporting quote is essentially just a [[link]] citation with no
    real relational statement — the v3 exploit (quote a 'Links:' line to pass the substring gate).
    Strip wikilinks + punctuation + citation/stop words; if <3 meaningful tokens remain, it's a
    citation, not evidence of a relationship -> reject the promotion."""
    q = re.sub(r"\[\[[^\]]*\]\]", " ", quote or "")
    q = re.sub(r"[^a-z0-9 ]", " ", q.lower())
    toks = [t for t in q.split() if t not in _CITE_WORDS]
    return len(toks) < 3


def _schema():
    """Ollama structured-output schema: forces the relation into the ontology enum (GATE 2)."""
    return {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "specific": {"type": "boolean"},
                        "relation": {"type": "string", "enum": TYPES},
                        "quote": {"type": "string"},
                    },
                    "required": ["n", "specific", "relation", "quote"],
                },
            }
        },
        "required": ["labels"],
    }


_USEDBY_LINK = re.compile(r'\[\[([^\]|#]+)')

def _usedby_targets(body):
    """links under a 'Used by' section are REVERSE-direction (the target acts ON the
    source), so they must NEVER be forward-typed. Returns the set of link names on such lines."""
    ub = set()
    for ln in (body or '').split('\n'):
        if re.search(r'used[ _-]?by', ln, re.I) and '[[' in ln:
            ub |= {m.strip() for m in _USEDBY_LINK.findall(ln)}
    return ub


def classify_note(src_name, body, targets):
    """One structured LLM call for all of a note's untyped links. Returns {index -> verdict dict}
    where verdict = {specific: bool, relation: str, quote: str}. On any failure returns {} (caller
    then leaves everything relates_to but still marks examined so a transient miss doesn't wedge
    the backlog). GATE 1 (specific) + GATE 2 (enum) are enforced here; GATE 3 (substring) in main."""
    meanings = "\n".join("  %s = %s" % (t, _MEANINGS[t]) for t in SPECIFIC if t in _MEANINGS)
    listing = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(targets))
    prompt = (
        "You label the relationship FROM a SOURCE note TO each TARGET note it links to. Judge ONLY "
        "from the SOURCE TEXT below — never from the note names or outside knowledge.\n\n"
        "For each target, decide in this order:\n"
        "1. specific: true ONLY if the SOURCE TEXT explicitly states one of the specific "
        "relationships below, in the SOURCE -> TARGET direction. If the target is just mentioned, "
        "listed, cross-referenced ('see also'), or you are unsure -> specific=false.\n"
        "2. relation: if specific=false, use 'relates_to'. If specific=true, choose the ONE type.\n"
        "3. quote: if specific=true, copy the EXACT sentence/phrase from the SOURCE TEXT that proves "
        "it (verbatim substring). If specific=false, use \"\".\n\n"
        "DIRECTION matters: 'A uses B' != 'B uses A'. If the text says the TARGET acts on the SOURCE "
        "(reverse direction), that is NOT a match -> specific=false.\n"
        "CITATION rule: a cross-reference such as 'Links: [[X]]', 'see [[X]]', 'see also', "
        "'sources: [[X]]', 'Detail: [[X]]', 'related [[X]]', 'pairs with [[X]]', 'tie-in: [[X]]' is "
        "NOT a relationship -> specific=false. The quote must be a real STATEMENT with a verb saying "
        "how SOURCE is hosted by / reached through / uses / depends on / replaces TARGET — NEVER just "
        "a link-list or 'see also' line.\n"
        "NEGATIVE rules — the following do NOT make a specific relation, answer relates_to:\n"
        "  - SOURCE 'feeds / pushes to / commits to / rsyncs to / writes to / exports to / syncs to' "
        "TARGET is a DATA FLOW into a store/destination — not runs_on, not accessed_via, and not uses "
        "(uses means SOURCE calls/integrates with TARGET's interface, not that it dumps data there).\n"
        "  - SOURCE 'signed by / minted from / issued by / gets a cert from' TARGET (e.g. a CA"
        "(a CA) is not runs_on and not accessed_via.\n"
        "  - SOURCE 'monitored by / scraped by / polled by / logged by / watched by' TARGET is NOT "
        "accessed_via — that is the REVERSE direction (the monitor reaches the source).\n"
        "  - If the SOURCE note's whole purpose is to DOCUMENT or describe the TARGET (a note about "
        "another note), that is not a relationship -> relates_to.\n"
        "Most links are NOT specific — it is correct and expected for most answers to be relates_to.\n\n"
        "Specific relationship types (SOURCE -> TARGET):\n%s\n\n"
        "SOURCE note: %s\n"
        "SOURCE TEXT:\n%s\n\n"
        "TARGETS:\n%s\n\n"
        "Return one label object per target (by its number n)."
        % (meanings, src_name, (body or "")[:BODY_CAP], listing)
    )
    parsed = llm_provider.llm_json(prompt, _schema(), num_predict=2048, timeout=OLLAMA_TIMEOUT)
    if parsed is None:                                    # signal FAILURE (vs a legit-empty {}) so the
        return None                                       # caller can skip + retry instead of stamping examined
    out = {}
    for lab in (parsed.get("labels") or []):
        try:
            idx = int(lab.get("n")) - 1
        except (TypeError, ValueError):
            continue
        rel = (lab.get("relation") or DEFAULT_REL).strip().lower()
        if 0 <= idx < len(targets):
            out[idx] = {"specific": bool(lab.get("specific")),
                        "relation": rel if rel in TYPES else DEFAULT_REL,
                        "quote": lab.get("quote") or ""}
    # GUARD: never forward-type a 'Used by' (reverse-direction) link -> force relates_to.
    _ub = _usedby_targets(body)
    for _i, _t in enumerate(targets):
        if str(_t).strip() in _ub:
            out[_i] = {"specific": False, "relation": DEFAULT_REL, "quote": ""}
    return out


def verify_edge(quote, src_name, dst_name, rel, lens=0):
    """GATE 4 — a strict second-pass fact-check (prediction-then-verify). Sees ONLY the one
    supporting sentence + the single claim SOURCE-rel-TARGET (not the note, not the other links),
    so there is nothing to guess from. Confirms the sentence EXPLICITLY states that relationship
    in that direction. Returns True only on an explicit yes; on failure/parse-miss returns False
    (reject -> the edge falls back to relates_to). This kills 'right quote, wrong type/direction'.

    `lens` selects an INDEPENDENT framing so _confidence() can corroborate a claim from
    different angles (diversity catches failure modes a repeated identical check would miss —
    temp=0 makes a re-run identical, so the framing must change, not the seed):
      0 = neutral fact-checker (the original GATE 4);
      1 = entailment — confirm only if the sentence's meaning NECESSARILY includes the relation in
          the SOURCE->TARGET direction (an independent angle; strict on vague/reverse but, unlike a
          refute-biased prompt, it still accepts what the sentence actually states);
      others cycle back over 0/1."""
    meaning = _MEANINGS.get(rel, rel)
    src_r, dst_r = _readable(src_name), _readable(dst_name)
    schema = {"type": "object", "properties": {"confirmed": {"type": "boolean"}},
              "required": ["confirmed"]}
    if lens % 2 == 1:
        prompt = (
            "Entailment check. Does the SENTENCE below NECESSARILY entail that \"%s\" %s \"%s\" "
            "(the relation in the SOURCE->TARGET direction, SOURCE=\"%s\", TARGET=\"%s\")?\n\n"
            "SENTENCE: \"%s\"\n\n"
            "confirmed=true if the sentence's meaning necessarily includes that relationship (judge by "
            "meaning; the target may be worded differently). confirmed=false if the sentence only "
            "mentions / lists / cross-references the target, is merely topically related, states the "
            "relationship in the REVERSE direction (the target acts on the source), or does not state "
            "it at all."
            % (src_r, meaning, dst_r, src_r, dst_r, quote or "")
        )
    else:
        prompt = (
            "You are a fact-checker. The SENTENCE below was written in a note ABOUT \"%s\" (the SOURCE). "
            "The claimed relationship is: %s  — where SOURCE = \"%s\" and TARGET = \"%s\".\n\n"
            "SENTENCE: \"%s\"\n\n"
            "Judge by MEANING, not exact words — the target may be worded differently in the sentence. "
            "confirmed=true ONLY if the sentence clearly states this relationship in THIS direction. "
            "confirmed=false if it merely mentions / lists / cross-references / 'aligns with' / 'see "
            "also' the target, only implies it, or states the REVERSE (the target acts on the source).\n"
            "Examples: sentence 'This CORRECTS the old mechanism' for a supersedes claim about that "
            "mechanism -> true; sentence 'Superseded by the new dashboard' for a claim that the source "
            "supersedes the new dashboard -> false (that is the reverse direction); 'see also X' -> false."
            % (src_r, meaning, src_r, dst_r, quote or "")
        )
    parsed = llm_provider.llm_json(prompt, schema, num_predict=128, timeout=OLLAMA_TIMEOUT)
    return bool(parsed.get("confirmed")) if parsed else False


def confidence(quote, src_name, dst_name, rel, k=CONFIDENCE_VOTES):
    """corroboration confidence for auto-apply — run k INDEPENDENT verifier lenses and return
    the fraction that confirm (0.0..1.0). A lens that errors counts as not-confirmed (conservative).
    Reused only in review_mode=off; verification, not a human, is the precision floor here."""
    votes = sum(1 for i in range(k) if verify_edge(quote, src_name, dst_name, rel, lens=i))
    return votes / float(k)


def decide(verdict, body_norm):
    """Apply the gates -> (rel_type, reason). The grounding + citation gates now run for ALL specific
    types (so REVIEW-gated infra types are only proposed when they actually pass). Reasons:
      promoted        -> AUTOPROMOTE type, apply now (still verified in main via GATE 4)
      propose         -> REVIEW type that passed grounding+citation; queue for a manager (verified too)
      not-autopromoted-> a specific type that's neither auto nor review-gated (author-typed-only)
      not-specific / default-or-invalid / ungrounded / citation-only -> relates_to."""
    if not verdict or not verdict.get("specific"):
        return DEFAULT_REL, "not-specific"
    rel = verdict.get("relation")
    if rel not in SPECIFIC:
        return DEFAULT_REL, "default-or-invalid"
    if rel not in AUTOPROMOTE and rel not in REVIEW:
        return DEFAULT_REL, "not-autopromoted"  # valid label, but this type is author-typed-only
    q = _norm_text(verdict.get("quote"))
    if not q or q not in body_norm:
        return DEFAULT_REL, "ungrounded"      # GATE 3a: quote not found in source -> reject
    if _is_citation_quote(verdict.get("quote")):
        return DEFAULT_REL, "citation-only"   # GATE 3b: quote is just a 'Links:/see also' line -> reject
    return (rel, "promoted") if rel in AUTOPROMOTE else (rel, "propose")


def promote(cur, edge, new_type):
    """Set an edge's rel_type (promote-in-place) and mark examined. On the (src,dst,type) unique
    key MERGE: fold this edge's weight into the existing one and drop this row. Returns outcome."""
    if new_type == DEFAULT_REL:
        cur.execute("UPDATE memory_relation SET classified_at = now() WHERE id = %s", (edge["id"],))
        return "kept"
    try:
        cur.execute("SAVEPOINT pr")
        cur.execute("UPDATE memory_relation SET rel_type=%s, classified_at=now(), updated_at=now() "
                    "WHERE id=%s", (new_type, edge["id"]))
        cur.execute("RELEASE SAVEPOINT pr")
        return "promoted"
    except psycopg2.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT pr")
        cur.execute("UPDATE memory_relation SET weight = weight + %s, updated_at = now() "
                    "WHERE src_id=%s AND dst_id=%s AND rel_type=%s",
                    (edge["weight"], edge["src_id"], edge["dst_id"], new_type))
        cur.execute("DELETE FROM memory_relation WHERE id = %s", (edge["id"],))
        cur.execute("RELEASE SAVEPOINT pr")
        return "merged"


def propose_edge_type(cur, edge, rel, quote):
    """queue a REVIEW-gated infra type for a manager instead of applying it. The edge KEEPS
    rel_type='relates_to' (graph/recall unchanged until approval) and records the proposal; we set
    classified_at so the backlog scan won't re-examine it. A manager approves/rejects via the API."""
    cur.execute("UPDATE memory_relation SET proposed_type=%s, proposed_quote=%s, proposed_at=now(), "
                "classified_at=now(), updated_at=now() WHERE id=%s",
                (rel, (quote or "")[:500], edge["id"]))
    return "proposed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max pending notes to process (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="classify + print, write nothing")
    ap.add_argument("--review-mode", choices=["on", "off"], default=None,
                    help="override graph.yaml review_mode: on=queue REVIEW types for a manager; "
                         "off=auto-apply high-confidence (>= auto_apply_confidence), skip the queue")
    args = ap.parse_args()
    review_mode = REVIEW_MODE if args.review_mode is None else (args.review_mode == "on")

    conn = connect()
    conn.autocommit = False
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT DISTINCT src_id FROM memory_relation "
                    "WHERE rel_type = %s AND classified_at IS NULL", (DEFAULT_REL,))
        src_ids = [r["src_id"] for r in cur.fetchall()]
    if args.limit:
        src_ids = src_ids[:args.limit]

    tally = {"notes": 0, "promoted": 0, "merged": 0, "kept": 0, "rejected": 0, "verify_failed": 0,
             "not_auto": 0, "proposed": 0, "auto_applied": 0, "borderline": 0}
    by_type = {}
    samples = []
    for src_id in src_ids:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name, body FROM memory WHERE id = %s AND deleted_at IS NULL", (src_id,))
            note = cur.fetchone()
            if not note:
                continue
            cur.execute(
                "SELECT r.id, r.src_id, r.dst_id, r.weight, m.name AS dst_name "
                "FROM memory_relation r JOIN memory m ON m.id = r.dst_id AND m.deleted_at IS NULL "
                "WHERE r.src_id = %s AND r.rel_type = %s AND r.classified_at IS NULL "
                "ORDER BY r.created_at LIMIT %s", (src_id, DEFAULT_REL, MAX_LINKS))
            edges = cur.fetchall()
            if not edges:
                continue
            targets = [e["dst_name"] for e in edges]
            verdict = classify_note(note["name"], note["body"], targets)
            if verdict is None:                           # transient LLM/parse failure — do NOT stamp
                tally["llm_failed"] = tally.get("llm_failed", 0) + 1   # examined; leave for the next run to retry
                continue
            body_norm = _norm_text(note["body"])
            tally["notes"] += 1
            for i, e in enumerate(edges):
                rel, reason = decide(verdict.get(i), body_norm)
                # GATE 4 — verify both auto-promote AND review candidates; a fail drops to relates_to
                if reason in ("promoted", "propose") and not verify_edge(
                        (verdict.get(i) or {}).get("quote"), note["name"], e["dst_name"], rel):
                    rel, reason = DEFAULT_REL, "verify-failed"
                if reason in ("ungrounded", "citation-only"):
                    tally["rejected"] += 1
                elif reason == "verify-failed":
                    tally["verify_failed"] += 1
                elif reason == "not-autopromoted":
                    tally["not_auto"] += 1
                q80 = (verdict.get(i, {}).get("quote") or "")[:80]
                if reason == "propose" and review_mode:
                    # verified candidate -> QUEUE for a manager (rel_type stays relates_to)
                    if not args.dry_run:
                        propose_edge_type(cur, e, rel, (verdict.get(i) or {}).get("quote"))
                    tally["proposed"] += 1
                    by_type["propose:" + rel] = by_type.get("propose:" + rel, 0) + 1
                    if len(samples) < 30:
                        samples.append('PROPOSE %s --%s--> %s   ["%s"]'
                                       % (note["name"], rel, e["dst_name"], q80))
                elif reason == "propose":
                    # review_mode=off: no human queue. Corroborate across CONFIDENCE_VOTES lenses;
                    # auto-apply if the confirm fraction reaches the threshold, else stay relates_to.
                    conf = confidence((verdict.get(i) or {}).get("quote"),
                                      note["name"], e["dst_name"], rel)
                    if conf >= AUTO_APPLY_CONF:
                        outcome = "would-promote" if args.dry_run else promote(cur, e, rel)
                        by_type[rel] = by_type.get(rel, 0) + 1
                        tally["merged" if outcome == "merged" else "promoted"] += 1
                        tally["auto_applied"] += 1
                        if len(samples) < 30:
                            samples.append('AUTO(%.2f) %s --%s--> %s   ["%s"]'
                                           % (conf, note["name"], rel, e["dst_name"], q80))
                    else:
                        if not args.dry_run:
                            promote(cur, e, DEFAULT_REL)   # borderline -> relates_to, mark examined
                        tally["borderline"] += 1
                        tally["kept"] += 1
                        if len(samples) < 30:
                            samples.append('BORDERLINE(%.2f) %s -x-> %s (%s) kept relates_to'
                                           % (conf, note["name"], e["dst_name"], rel))
                elif rel != DEFAULT_REL:
                    outcome = "would-promote" if args.dry_run else promote(cur, e, rel)
                    by_type[rel] = by_type.get(rel, 0) + 1
                    tally["merged" if outcome == "merged" else "promoted"] += 1
                    if len(samples) < 30:
                        samples.append('%s --%s--> %s   ["%s"]'
                                       % (note["name"], rel, e["dst_name"], q80))
                else:
                    if not args.dry_run:
                        promote(cur, e, DEFAULT_REL)   # mark examined (classified_at) so it isn't re-scanned
                    tally["kept"] += 1
            if not args.dry_run:
                conn.commit()
    conn.close()

    print("\n=== classify_edges %s ===" % ("(DRY RUN)" if args.dry_run else ""))
    print("review_mode        : %s   (votes=%d, auto_apply_confidence=%.2f)"
          % ("on (queue for manager)" if review_mode else "off (auto-apply high-confidence)",
             CONFIDENCE_VOTES, AUTO_APPLY_CONF))
    print("notes processed    : %d" % tally["notes"])
    print("promoted (typed)   : %d   merged: %d" % (tally["promoted"], tally["merged"]))
    if review_mode:
        print("PROPOSED (review)  : %d   (REVIEW types queued for manager approve/reject)" % tally["proposed"])
    else:
        print("AUTO-APPLIED       : %d   borderline->relates_to: %d   (REVIEW types, no human queue)"
              % (tally["auto_applied"], tally["borderline"]))
    print("kept relates_to    : %d   (grounding/citation rejects: %d, verify rejects: %d, "
          "not-autopromoted: %d)"
          % (tally["kept"], tally["rejected"], tally["verify_failed"], tally["not_auto"]))
    print("by type            : %s"
          % (", ".join("%s=%d" % (k, v) for k, v in sorted(by_type.items())) or "-"))
    if samples:
        print("sample typed edges (with grounding quote):")
        for s in samples:
            print("  " + s)


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


if __name__ == "__main__":
    main()
