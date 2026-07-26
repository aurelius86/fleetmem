#!/usr/bin/env python3
"""eval_typing_nogold.py — estimate per-rel-type typing PRECISION on ANY corpus with NO
hand-labeled gold, so a shipped user (or we) can see "is this relation type reliable enough to
auto-apply?" and set the Stage-4 threshold (classify_edges auto_apply_confidence) PER TYPE.

Generalizes eval_edge_typing.py (which scores the classifier against a hand-built gold JSONL) to a
no-gold, corpus-driven form: it samples the typed edges ALREADY in the graph and grades each with an
independent LLM judge (SILVER labels). Read-only — it never writes to the graph. Ollama-only,
config-driven via graph.yaml (imports classify_edges for the shared config + helpers).

METHOD (per rel_type != relates_to):
  1. sample up to --sample real edges of that type (src note + target + created_by).
  2. judge_edge(): an INDEPENDENT grounding judge re-reads the WHOLE source note (NOT the
     classifier's stored quote — that would rubber-stamp the classifier's own verdict) and decides
     if the note explicitly states SOURCE-rel-TARGET, grounded + direction-correct. "supported"
     requires a yes AND a verbatim quote found in the note (grounding), else not-supported.
  3. precision estimate = supported / sampled, with a Wilson 95% interval + the created_by mix.
Output is a human table + a JSON block (rel_type -> estimate) usable to set per-type thresholds.

These are ESTIMATES (silver labels), not truth. `--calibrate` measures the judge itself against
known-answer probes + our hand-labeled gold so we know how far to trust the numbers.

Usage (as the brain service user):
    python3 eval_typing_nogold.py                 # sample + judge live edges, print table + JSON
    python3 eval_typing_nogold.py --sample 20 --min-precision 0.9
    python3 eval_typing_nogold.py --seed 1        # reproducible sampling (Postgres setseed)
    python3 eval_typing_nogold.py --calibrate     # judge-vs-known + judge-vs-gold, then run
"""
import argparse
import json
import math
import os
import sys
import urllib.request

import psycopg2
import psycopg2.extras

import classify_edges as C

JUDGE_NPRED = 256


def _judge_schema():
    return {"type": "object",
            "properties": {"supported": {"type": "boolean"}, "quote": {"type": "string"}},
            "required": ["supported", "quote"]}


def judge_edge(src_name, body, dst_name, rel):
    """Independent grounded judge -> (supported: bool, quote: str). Sees the whole source note and
    re-derives its own supporting quote; supported=True requires the model to affirm AND for the
    quote to be a verbatim substring of the note (grounding gate), killing hallucinated support."""
    meaning = C._MEANINGS.get(rel, rel)
    src_r, dst_r = C._readable(src_name), C._readable(dst_name)
    prompt = (
        "You are a STRICT grounding judge auditing a knowledge-graph edge. Read the SOURCE note "
        "below. Decide whether the note EXPLICITLY states this relationship:\n"
        "    %s  (where SOURCE = \"%s\", TARGET = \"%s\")\n\n"
        "SOURCE note \"%s\":\n%s\n\n"
        "supported=true ONLY if a sentence in the note states this relationship in the SOURCE->TARGET "
        "direction. supported=false if the note merely mentions / lists / cross-references the target, "
        "only implies it, states the REVERSE direction (the target acts on the source), or does not "
        "state it. quote = copy the EXACT sentence proving it (verbatim substring of the note); if "
        "supported=false, use \"\"."
        % (meaning, src_r, dst_r, src_r, (body or "")[:C.BODY_CAP])
    )
    try:
        data = json.dumps({"model": C.OLLAMA_MODEL, "prompt": prompt, "stream": False,
                           "format": _judge_schema(),
                           "options": {"temperature": 0, "num_predict": JUDGE_NPRED}}).encode()
        req = urllib.request.Request(C.OLLAMA_URL, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=C.OLLAMA_TIMEOUT) as r:
            resp = json.loads(r.read()).get("response", "")
        parsed = json.loads(resp)
    except Exception as e:
        sys.stderr.write("  ! judge failed for %s-%s->%s: %s\n" % (src_name, rel, dst_name, e))
        return (False, "")
    q = parsed.get("quote") or ""
    grounded = bool(q) and C._norm_text(q) in C._norm_text(body or "")
    return (bool(parsed.get("supported")) and grounded, q)


def wilson(k, n, z=1.96):
    """Wilson score 95% interval for k successes in n trials -> (low, high). Robust at small n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


def calibrate():
    """Measure the JUDGE itself. (a) hand-crafted known-answer probes (clear yes / clear no /
    reverse) prove it neither rubber-stamps nor over-rejects; (b) recall on our hand-labeled
    specific-gold cases (eval_edge_typing.jsonl). Prints accuracy so we know how far to trust the
    silver estimates."""
    print("=== JUDGE CALIBRATION ===")
    probes = [
        ("Grafana uses Prometheus as its metrics data source.", "Grafana", "Prometheus", "uses", True),
        ("The dashboard runs on the app server node.", "dashboard", "app server", "runs_on", True),
        ("This note supersedes the old design and replaces it entirely.", "new design", "old design", "supersedes", True),
        ("See also Prometheus. Links: [[Prometheus]].", "Grafana", "Prometheus", "uses", False),
        ("Prometheus is scraped by Grafana for metrics.", "Prometheus", "Grafana", "uses", False),
        ("The API's cert is signed by the internal CA on issuance.", "API", "internal CA", "runs_on", False),
    ]
    ok = 0
    for body, s, d, rel, exp in probes:
        sup, _ = judge_edge(s, body, d, rel)
        mark = "OK " if sup == exp else "XX "
        ok += (sup == exp)
        print("  [%s] want=%-5s got=%-5s  %s -%s-> %s" % (mark, exp, sup, s, rel, d))
    print("  known-answer probes: %d/%d correct" % (ok, len(probes)))

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_edge_typing.jsonl")
    pos = pos_ok = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                for c in rec["cases"]:
                    if c["gold"] == "relates_to":
                        continue                       # no specific relation -> not a judge positive
                    pos += 1
                    sup, _ = judge_edge(rec["name"], rec["body"], c["target"], c["gold"])
                    pos_ok += bool(sup)
        print("  gold-positive recall (judge confirms a KNOWN-true typed relation): %d/%d" % (pos_ok, pos))
    except Exception as e:
        print("  (gold recall skipped: %s)" % e)
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30, help="max edges to judge per rel_type")
    ap.add_argument("--min-precision", type=float, default=0.9,
                    help="precision estimate at/above which a type is flagged auto-apply-safe")
    ap.add_argument("--seed", type=float, default=None, help="Postgres setseed for reproducible sampling")
    ap.add_argument("--calibrate", action="store_true", help="run judge calibration first")
    args = ap.parse_args()

    if args.calibrate:
        calibrate()

    conn = connect()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if args.seed is not None:
            cur.execute("SELECT setseed(%s)", (max(-1.0, min(1.0, args.seed)),))
        cur.execute("SELECT DISTINCT rel_type FROM memory_relation WHERE rel_type <> %s "
                    "ORDER BY rel_type", (C.DEFAULT_REL,))
        rel_types = [r["rel_type"] for r in cur.fetchall()]

        results = {}
        for rt in rel_types:
            cur.execute(
                "SELECT r.created_by, s.name AS src_name, s.body AS src_body, d.name AS dst_name "
                "FROM memory_relation r "
                "JOIN memory s ON s.id = r.src_id AND s.deleted_at IS NULL "
                "JOIN memory d ON d.id = r.dst_id AND d.deleted_at IS NULL "
                "WHERE r.rel_type = %s ORDER BY random() LIMIT %s", (rt, args.sample))
            edges = cur.fetchall()
            n = len(edges)
            supported = 0
            cby = {}
            fails = []
            for e in edges:
                cby[e["created_by"] or "?"] = cby.get(e["created_by"] or "?", 0) + 1
                sup, _q = judge_edge(e["src_name"], e["src_body"], e["dst_name"], rt)
                if sup:
                    supported += 1
                elif len(fails) < 4:
                    fails.append("%s -x-> %s" % (e["src_name"][:34], e["dst_name"][:24]))
            prec = supported / n if n else 0.0
            lo, hi = wilson(supported, n)
            results[rt] = {"sampled": n, "supported": supported, "precision": round(prec, 3),
                           "ci95": [round(lo, 3), round(hi, 3)], "created_by": cby,
                           "auto_apply_safe": bool(n >= 5 and lo >= args.min_precision),
                           "sample_fails": fails}
            sys.stderr.write("  ...judged %s: %d edges\n" % (rt, n))
    conn.close()

    print("=== per-rel-type typing precision (SILVER estimate, no gold) ===")
    print("%-16s %6s %10s %11s  %-22s %s" % ("rel_type", "n", "precision", "95% CI", "created_by", "auto-apply?"))
    for rt in sorted(results, key=lambda k: results[k]["precision"], reverse=True):
        r = results[rt]
        cby = ",".join("%s:%d" % (k, v) for k, v in sorted(r["created_by"].items()))
        flag = "YES" if r["auto_apply_safe"] else ("n<5" if r["sampled"] < 5 else "no")
        print("%-16s %6d %10.3f  [%.2f-%.2f]  %-22s %s"
              % (rt, r["sampled"], r["precision"], r["ci95"][0], r["ci95"][1], cby[:22], flag))
    print("\nauto-apply-safe = sampled>=5 AND lower-CI >= %.2f (precision-first: judge the FLOOR, "
          "not the point estimate)" % args.min_precision)
    print("CAVEAT: this judge checks PROSE grounding — 'does the source note STATE this relation?'. "
          "It is the right evaluator for prose-typed relations (e.g. uses/supersedes). It UNDERSTATES "
          "types encoded as 'Depends on:' LISTS or derived structurally from the infra model "
          "(accessed_via/runs_on/depends_on via infra-derive) — those are true-by-structure, not "
          "restated as a sentence. Read the created_by mix: infra-derive edges are ~100% by "
          "construction and should not be gated on this prose score.")
    print("\nJSON (per-type, usable to set classify_edges auto_apply_confidence per type):")
    print(json.dumps(results, indent=2))
    print("\nNOTE: silver labels from an LLM judge, not ground truth — run --calibrate to see how far "
          "to trust these.")


if __name__ == "__main__":
    main()
