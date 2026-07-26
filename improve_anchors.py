#!/usr/bin/env python3
""" Phase 2c — improve entity.anchor_memory (pick the BEST canonical note per entity).

Better anchors => mention edges point to the right note. Two signals, best-first:
  1. NAME match  — a live note whose slug (minus type prefix) equals the entity name or one of its
                   aliases. Highest confidence. Ties: prefer a 'reference_' note (definitional),
                   then the shortest slug (most general). This is deterministic + ~exact.
  2. EMBEDDING   — for entities with no name-match, the single most similar note by bge-m3 cosine
                   (entity.embedding <=> memory.embedding). Shown for review (lower confidence).

  python3 improve_anchors.py --dry-run    # print proposed before->after, write nothing
  python3 improve_anchors.py              # apply (UPDATE entity.anchor_memory)
  python3 improve_anchors.py --names-only # apply only the high-confidence NAME matches
"""
import argparse, os, re, collections
import psycopg2, psycopg2.extras

PREFIX = re.compile(r"^(reference|feedback|project|decision|session|user|task|inference|hook|agent|gotcha|idea)_")
norm = lambda s: re.sub(r"[\s\-_]", "", (s or "").lower())


def connect():
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--names-only", action="store_true")
    args = ap.parse_args()

    conn = connect(); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, name FROM memory WHERE deleted_at IS NULL AND name IS NOT NULL")
    notes = cur.fetchall()
    # normalized note-stem -> [names], for exact + contains matching
    stem2names = collections.defaultdict(list)
    for n in notes:
        stem2names[norm(PREFIX.sub("", n["name"]))].append(n["name"])
    live = {n["name"] for n in notes}

    cur.execute("SELECT name, anchor_memory FROM entity ORDER BY name")
    entities = cur.fetchall()
    cur.execute("SELECT entity_name, alias FROM entity_alias")
    aliases = collections.defaultdict(set)
    for r in cur.fetchall():
        aliases[r["entity_name"]].add(r["alias"])

    def pick_name(ent):
        keys = {norm(ent)} | {norm(a) for a in aliases.get(ent, ())}
        cands = []
        for k in keys:
            for nm in stem2names.get(k, []):        # exact stem match
                cands.append((0, nm))
        if not cands:                                # contains match (entity term inside a slug)
            for stem, names in stem2names.items():
                if any(k and k in stem for k in keys):
                    cands += [(1, nm) for nm in names]
        if not cands:
            return None
        # rank: exact(0)<contains(1); prefer reference_; then shortest slug
        cands.sort(key=lambda t: (t[0], 0 if t[1].startswith("reference_") else 1, len(t[1])))
        return cands[0][1]

    def pick_embed(ent):
        cur.execute("SELECT m.name FROM memory m, entity e WHERE e.name=%s AND m.deleted_at IS NULL "
                    "AND m.name IS NOT NULL AND e.embedding IS NOT NULL "
                    "ORDER BY m.embedding <=> e.embedding LIMIT 1", (ent,))
        r = cur.fetchone()
        return r["name"] if r else None

    changes = []   # (entity, old, new, method)
    for e in entities:
        ent, old = e["name"], e["anchor_memory"]
        new = pick_name(ent); method = "name"
        if not new and not args.names_only:
            new = pick_embed(ent); method = "embed"
        if new and new in live and new != old:
            changes.append((ent, old, new, method))

    bym = collections.Counter(c[3] for c in changes)
    print("entities: %d   proposed anchor changes: %d  (%s)" % (len(entities), len(changes), dict(bym)))
    print("\n--- NAME matches (high confidence) ---")
    for ent, old, new, m in changes:
        if m == "name":
            print("  %-24s %s -> %s" % (ent[:24], (old or "-")[:34], new[:40]))
    print("\n--- EMBEDDING fallback (review) ---")
    for ent, old, new, m in changes:
        if m == "embed":
            print("  %-24s %s -> %s" % (ent[:24], (old or "-")[:34], new[:40]))

    if args.dry_run:
        print("\nDRY RUN — no writes."); return
    n = 0
    for ent, old, new, m in changes:
        if args.names_only and m != "name":
            continue
        cur.execute("UPDATE entity SET anchor_memory=%s, updated_at=now() WHERE name=%s", (new, ent))
        n += 1
    conn.commit()
    print("\nUPDATED %d anchors." % n)


if __name__ == "__main__":
    main()
