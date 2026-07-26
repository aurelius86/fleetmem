#!/usr/bin/env python3
""" (golden-set half) — scheduled recall-quality REGRESSION check.

Runs the FULL /recall pipeline (api.recall_core) over the labelled golden sets, records hit@5 to
action_log, and ALERTS every manager if the score dropped vs the previous recorded run. A drop can
mean recall regressed (a code/config change) OR the store drifted / the golden set needs review —
either way a manager should look. No side effects: recall_core is pure retrieval and we rollback the
read cursor, so eval queries never pollute session_recall.

Runs as a manager (owner peer-auth => sees all trusted memory), like golden_pipeline.py. Scheduled
monthly via the brain-golden-eval systemd timer on the brain host. Manual run:
    sudo -u brain python3 golden_regression.py
"""
import json
import os

import psycopg2
import psycopg2.extras

import api

HERE = os.path.dirname(os.path.abspath(__file__))
SETS = ["golden.json", "golden_hard.json"]
AGENT = {"name": "auditor", "role": "manager", "access_scope": {"see_all": True}}  # see_all = full trusted visibility (old synthetic agent had empty readers -> saw no trusted notes)
# a FIXED floor, independent of the last run. The below-last-run check silently rebaselines to
# each slightly-lower number, so a slow drift (one golden target going stale per week from memory
# consolidation) never trips — exactly how sat at a stable 38/41 with regressed=False while the
# documented baseline was 41. The floor catches that. 40 given the 41-query set (near-ceiling for this
# query set). Env-overridable to test (GOLDEN_FLOOR).
FLOOR = int(os.environ.get("GOLDEN_FLOOR", "38"))


def score_set(cur, path):
    golden = json.load(open(path))
    hits, misses = 0, []
    for g in golden:
        q, _ = api.normalize_query(g["q"])
        _mode, out, _ids = api.recall_core(cur, AGENT, q, 5, None)
        names = [o["name"] for o in out]
        _exp = g["expect"] if isinstance(g["expect"], list) else [g["expect"]]
        if any(e in n for e in _exp for n in names):
            hits += 1
        else:
            misses.append(g["q"])
    return hits, len(golden), misses


def main():
    conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")
    cur = conn.cursor()
    total_hits = total = 0
    per_set, all_misses = {}, []
    for s in SETS:
        p = os.path.join(HERE, s)
        if not os.path.exists(p):
            continue
        h, t, misses = score_set(cur, p)
        per_set[s] = "%d/%d" % (h, t)
        total_hits += h
        total += t
        all_misses += misses
    conn.rollback()   # eval reads have no side effects
    pct = round(100.0 * total_hits / total, 1) if total else 0.0

    # previous recorded run (for regression comparison)
    rc = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rc.execute("SELECT detail FROM action_log WHERE action='golden_eval' ORDER BY created_at DESC LIMIT 1")
    prev = rc.fetchone()
    rc.close()
    prev_hits = (prev["detail"] or {}).get("hits") if prev else None
    regressed = prev_hits is not None and total_hits < prev_hits
    below_floor = total_hits < FLOOR          # fixed-floor trip, independent of the last run
    alert = regressed or below_floor

    detail = {"hits": total_hits, "total": total, "pct": pct, "per_set": per_set,
              "prev_hits": prev_hits, "floor": FLOOR, "below_floor": below_floor, "misses": all_misses[:20]}
    w = conn.cursor()
    w.execute("INSERT INTO action_log(actor,action,target_kind,detail) VALUES (%s,%s,%s,%s)",
              ("brain-golden", "golden_eval", "eval", psycopg2.extras.Json(detail)))
    if alert:
        w.execute("INSERT INTO action_log(actor,action,target_kind,detail) VALUES (%s,%s,%s,%s)",
                  ("brain-golden", "golden_regression", "eval", psycopg2.extras.Json(detail)))
        # name the trigger(s) so the alert says WHY (floor and/or below-last-run)
        why = []
        if below_floor:
            why.append("BELOW FLOOR (%d < %d)" % (total_hits, FLOOR))
        if regressed:
            why.append("below last run (%d < %s)" % (total_hits, prev_hits))
        reason = " + ".join(why)
        w.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
        for (m,) in w.fetchall():
            w.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) VALUES (%s,%s,%s,%s,'alert')",
                      ("brain-golden", m, "ALERT: recall golden-set regression",
                       "Recall hit@5 %s -> %d of %d (%.1f%%). Misses: %s. Either recall regressed "
                       "(code/config) or the store drifted / the golden set needs review — run "
                       "golden_pipeline.py on the brain host to inspect."
                       % (reason, total_hits, total, pct, ", ".join(all_misses[:8]) or "none")))
    conn.commit()
    w.close()
    cur.close()
    conn.close()
    print("golden_eval: %d/%d (%.1f%%) per_set=%s prev_hits=%s floor=%d regressed=%s below_floor=%s"
          % (total_hits, total, pct, per_set, prev_hits, FLOOR, regressed, below_floor))


if __name__ == "__main__":
    main()
