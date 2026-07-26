#!/usr/bin/env python3
"""eval_edge_typing.py — labeled before/after eval for the edge-type classifier.

Runs the SAME pipeline as classify_edges.main() (classify_note -> decide -> verify_edge) over a
labeled JSONL set and reports predicted-vs-gold. Self-contained: the notes+targets are SYNTHETIC
(no DB touched), only Ollama is hit. It imports classify_edges, so it picks up whatever prompt +
graph.yaml are DEPLOYED — run it before AND after deploying the changes for a real before/after.

JSONL line: {"name": <src note>, "body": <text containing [[target]]>, "cases": [
    {"target": <target name>, "gold": <expected final rel_type>, "note": <why>}]}
gold = the CORRECT final type. relates_to = the classifier should abstain (confusion/citation cases).
"""
import json
import sys

import classify_edges as C


def predict(name, body, cases):
    targets = [c["target"] for c in cases]
    verdict = C.classify_note(name, body, targets)
    if verdict is None:
        return [(c["target"], "relates_to", "llm-fail") for c in cases]
    body_norm = C._norm_text(body)
    out = []
    for i, c in enumerate(cases):
        rel, reason = C.decide(verdict.get(i), body_norm)
        if reason in ("promoted", "propose"):        # mirror main(): verify promote AND propose
            q = (verdict.get(i) or {}).get("quote")
            if not C.verify_edge(q, name, c["target"], rel):
                rel, reason = C.DEFAULT_REL, "verify-failed"
        out.append((c["target"], rel, reason))
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "eval_edge_typing.jsonl"
    total = correct = false_specific = missed_specific = 0
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for c, (tgt, rel, reason) in zip(rec["cases"], predict(rec["name"], rec["body"], rec["cases"])):
                total += 1
                gold = c["gold"]
                ok = (rel == gold)
                if ok:
                    correct += 1
                elif gold == "relates_to":
                    false_specific += 1
                else:
                    missed_specific += 1
                rows.append(("OK " if ok else "XX", rec["name"], tgt, gold, rel, reason))
    print("=== edge-typing eval: %s ===" % path)
    print("  autopromote=%s  review=%s" % (C.AUTOPROMOTE, C.REVIEW))
    for mark, nm, tgt, gold, rel, reason in rows:
        print("  [%s] %-40s -> %-30s gold=%-13s pred=%-13s (%s)"
              % (mark, nm[:40], tgt[:30], gold, rel, reason))
    print("\naccuracy         : %d/%d" % (correct, total))
    print("false specific   : %d   (gold=relates_to but the LLM typed it — a PRECISION miss)" % false_specific)
    print("missed specific  : %d   (gold=a specific type but got relates_to — a RECALL miss)" % missed_specific)


if __name__ == "__main__":
    main()
