#!/usr/bin/env python3
"""derive_infra_edges.py — materialize STRUCTURAL knowledge-graph edges deterministically
from the curated infra model, instead of letting the LLM guess them.

WHY: qwen3:30b types the infra relations (runs_on/accessed_via/depends_on) at only ~25% precision — which is why those types were review-gated. But the infra model (migration 0021) already
encodes the truth: `infra_link` records depends_on/proxies/routes/runs_on/connects between hosts and
services, and each host/service carries an `anchor_memory` (the reference_* note it maps to). We map
every infra relation onto its two anchor notes and write a TYPED memory_relation edge between them at
100% precision — then the LLM classifier is restricted to genuinely semantic relations only (graph.yaml
review_types trimmed to `uses`; the three infra types are no longer proposed by the model).

MAPPING (infra rel -> memory edge type, direction):
  depends_on    -> depends_on    (same direction: src depends_on dst)
  runs_on       -> runs_on       (same direction)
  proxies       -> accessed_via  (REVERSED: the proxied thing is reached VIA the proxy, so dst->src)
  routes        -> accessed_via  (REVERSED: the routed network is reached VIA the router)
  connects      -> (skipped; too vague — left as relates_to / the LLM's job)
  infra_service.host -> runs_on   (service anchor -> host anchor; skipped when they share one note,
                                   which is the current norm — one reference_* note per CT+service)

IDEMPOTENT + safe to re-run. For each (src,dst,type):
  - upsert the typed edge (created_by='infra-derive', classified_at=now() so classify_edges — which
    only scans rel_type='relates_to' AND classified_at IS NULL — never touches it);
  - fold+remove any now-redundant relates_to edge for the SAME pair (the precise type replaces the
    generic one — this is the "type existing edges" half; the "invent new" half is the plain INSERT
    when no edge exists yet, per the operator's choice for a fuller graph).
Endpoints with no anchor_memory, or whose anchor isn't a LIVE (deleted_at IS NULL) memory, are skipped.
Self-loops (src anchor == dst anchor) are skipped (the memrel_no_self CHECK would reject them anyway).

Ollama-free (pure DB, deterministic). Runs as the brain service user. Wire on a weekly timer — infra changes
rarely, and a re-run only reconciles.

Usage (as the brain service user):
    python3 derive_infra_edges.py            # apply
    python3 derive_infra_edges.py --dry-run  # show what it WOULD write, change nothing
"""
import argparse
import os
import psycopg2
import psycopg2.extras

# infra_link.rel  ->  (memory edge rel_type, reverse_direction?)
REL_MAP = {
    "depends_on": ("depends_on", False),
    "runs_on":    ("runs_on",    False),
    "proxies":    ("accessed_via", True),
    "routes":     ("accessed_via", True),
    "connects":   (None, False),          # too vague to type structurally — skip
}


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


def upsert_typed_edge(cur, src_id, dst_id, rel_type, dry):
    """Ensure the typed edge exists and drop any redundant relates_to for the same pair.
    Returns an outcome string. Pre-checks existence (single-threaded batch job) so the report can
    distinguish inserted / promoted-from-relates_to / reinforced."""
    if src_id == dst_id:
        return "self-skip"
    cur.execute("SELECT weight FROM memory_relation WHERE src_id=%s AND dst_id=%s AND rel_type=%s",
                (src_id, dst_id, rel_type))
    typed = cur.fetchone()
    cur.execute("SELECT weight FROM memory_relation WHERE src_id=%s AND dst_id=%s AND rel_type='relates_to'",
                (src_id, dst_id))
    rr = cur.fetchone()
    fold = (rr["weight"] if rr else 0)
    if dry:
        if typed:
            return "reinforce" + ("+demote" if rr else "")
        return "promote" if rr else "insert"
    if typed:
        cur.execute("UPDATE memory_relation SET weight = weight + %s, created_by='infra-derive', "
                    "classified_at = now(), updated_at = now() "
                    "WHERE src_id=%s AND dst_id=%s AND rel_type=%s", (fold, src_id, dst_id, rel_type))
        outcome = "reinforced"
    else:
        cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight,classified_at) "
                    "VALUES (%s,%s,%s,'infra-derive','normal',%s,now())", (src_id, dst_id, rel_type, 1 + fold))
        outcome = "promoted" if rr else "inserted"
    if rr:
        cur.execute("DELETE FROM memory_relation WHERE src_id=%s AND dst_id=%s AND rel_type='relates_to'",
                    (src_id, dst_id))
    return outcome


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print planned edges, write nothing")
    args = ap.parse_args()

    conn = connect()
    conn.autocommit = False
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # live memory: exact note name (lowercased) -> id
        cur.execute("SELECT id, lower(name) AS nn FROM memory WHERE deleted_at IS NULL AND name IS NOT NULL")
        id_by_name = {r["nn"]: r["id"] for r in cur.fetchall()}
        # infra name (host or service) -> anchor_memory note name
        anchor = {}
        cur.execute("SELECT name, anchor_memory FROM infra_host WHERE anchor_memory IS NOT NULL")
        for r in cur.fetchall():
            anchor[r["name"]] = r["anchor_memory"]
        cur.execute("SELECT name, anchor_memory FROM infra_service WHERE anchor_memory IS NOT NULL")
        for r in cur.fetchall():
            anchor[r["name"]] = r["anchor_memory"]

        def resolve(infra_name):
            a = anchor.get(infra_name)
            return id_by_name.get(a.lower()) if a else None

        # build the deterministic edge list: (src_id, dst_id, rel_type, why, unresolved?)
        planned = []
        skipped = []
        cur.execute("SELECT src, dst, rel FROM infra_link ORDER BY src, dst, rel")
        for r in cur.fetchall():
            s, d, rel = r["src"], r["dst"], r["rel"]
            m = REL_MAP.get(rel)
            if not m or m[0] is None:
                skipped.append("skip(%s): %s %s %s — vague/unmapped rel" % (rel, s, rel, d))
                continue
            ttype, rev = m
            a_src, a_dst = resolve(s), resolve(d)
            if rev:
                a_src, a_dst = a_dst, a_src
            if not a_src or not a_dst:
                skipped.append("skip: %s %s %s — endpoint has no live anchor memory" % (s, rel, d))
                continue
            planned.append((a_src, a_dst, ttype, "%s %s %s" % (s, rel, d)))
        # infra_service.host -> runs_on (service anchor runs on host anchor)
        cur.execute("SELECT name, host FROM infra_service WHERE host IS NOT NULL ORDER BY name")
        for r in cur.fetchall():
            svc, host = r["name"], r["host"]
            a_svc, a_host = resolve(svc), resolve(host)
            if not a_svc or not a_host or a_svc == a_host:
                continue          # unresolved or the common self-loop (one note per CT+service)
            planned.append((a_svc, a_host, "runs_on", "%s runs_on %s" % (svc, host)))

        tally = {}
        for src_id, dst_id, ttype, why in planned:
            outcome = upsert_typed_edge(cur, src_id, dst_id, ttype, args.dry_run)
            tally[outcome] = tally.get(outcome, 0) + 1
            print("  %-11s %-13s  <- %s" % (outcome, ttype, why))
        if not args.dry_run:
            conn.commit()
    conn.close()

    print("\n=== derive_infra_edges %s ===" % ("(DRY RUN)" if args.dry_run else ""))
    print("planned edges : %d" % len(planned))
    print("outcomes      : %s" % (", ".join("%s=%d" % (k, v) for k, v in sorted(tally.items())) or "-"))
    if skipped:
        print("skipped       : %d" % len(skipped))
        for s in skipped:
            print("  " + s)


if __name__ == "__main__":
    main()
