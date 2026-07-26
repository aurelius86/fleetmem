#!/usr/bin/env python3
""" golden harness — runs the FULL /recall pipeline in-process (via api.recall_core) over a
labelled query set and reports hit@k, broken down by query kind. Complements `search.py --golden`
(which measures ONLY the base dense+FTS+RRF, no gate/fuzzy/expansion/rerank), so the two side by
side show what the new stages actually add.

Runs with a FULL-VISIBILITY eval identity (access_scope see_all = ALL trusted + ALL personal, the
console scope: the old synthetic {"name":"manager"} agent resolved to an
EMPTY readers array, so `readers && '{}'` hid EVERY trusted note and hit@k collapsed to noise — a
benchmark-only regression, not a recall regression. see_all restores the intended "sees all trusted
memory" behaviour. `expect` may be a single name OR a list of acceptable names (hit = ANY present).

Run on the brain host:  sudo -u brain python3 golden_pipeline.py [golden.json]
"""
import json
import sys

import psycopg2

import api


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "golden.json"
    golden = json.load(open(path))
    agent = {"name": "auditor", "role": "manager", "access_scope": {"see_all": True}}
    conn = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    cur = conn.cursor()
    hits = 0
    by_kind = {}
    for g in golden:
        q, _ = api.normalize_query(g["q"])
        mode, out, _ = api.recall_core(cur, agent, q, 5, None)
        names = [o["name"] for o in out]
        expect = g["expect"] if isinstance(g["expect"], list) else [g["expect"]]
        hit = any(e in n for e in expect for n in names)
        hits += hit
        k = g.get("kind", "?")
        by_kind.setdefault(k, [0, 0])
        by_kind[k][0] += hit
        by_kind[k][1] += 1
        print(("HIT " if hit else "MISS") + " | %-8s | %-40s | %-24s | %s"
              % (k, g["q"][:40], mode, ", ".join(names[:3])))
    conn.rollback()
    cur.close()
    conn.close()
    print("\nfull-pipeline hit@5 = %d/%d (%.0f%%)" % (hits, len(golden), 100.0 * hits / len(golden)))
    for k in sorted(by_kind):
        h, t = by_kind[k]
        print("  %-8s %d/%d" % (k, h, t))


if __name__ == "__main__":
    main()
