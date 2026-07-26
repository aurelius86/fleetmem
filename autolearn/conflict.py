"""Conflict + dedup gate (Big Step 6, Phase C).

The auto-keep path must never silently overwrite or duplicate a fact. This gate runs
BEFORE auto-keep and returns one deterministic verdict per candidate:

  * 'skip_dup'  — an identical fact already lives in memory (same content_hash, or same
                  name + same body). Don't propose it again; nothing to do.
  * 'conflict'  — the candidate's name already names a LIVE memory whose content differs.
                  That is a supersede/overwrite, which a person must judge -> escalate.
                  Auto-keep never overwrites; supersession is recorded as a relation
                  (with history) only after a human approves it.
  * 'clear'     — no exact dup, no name collision -> safe for the rest of the gate.

Why name-collision (not LLM-judged semantic contradiction) is the deterministic trigger:
contradiction detection needs a model, and a model in the auto-keep loop reintroduces the
author==validator risk. A name collision with differing content is the one contradiction
we can detect with certainty, and it is exactly the dangerous case (clobbering a known
fact). Semantically-similar NEIGHBOURS are surfaced (for the escalation card / future
LLM-judged conflict) but never block auto-keep on their own. Fail-safe: unknown -> 'clear'
only after the two hard checks pass; anything ambiguous about identity escalates.
"""

VERDICTS = ("skip_dup", "conflict", "clear")


def classify(candidate, *, dup_row=None, name_row=None):
    """Pure: decide the verdict from the two looked-up rows.

    `dup_row`  = a live memory row (dict with content_hash/body) matching the candidate's
                 content_hash, or None.
    `name_row` = a live memory row matching the candidate's name, or None.
    Either may be supplied by the caller's DB lookups (see find_collisions)."""
    chash = candidate.get("content_hash")
    body = (candidate.get("proposed_body") or candidate.get("body") or "").strip()

    # 1) exact content already present -> nothing new to keep
    if dup_row is not None:
        return "skip_dup"
    if name_row is not None:
        existing_body = (name_row.get("body") or "").strip()
        existing_hash = name_row.get("content_hash")
        # same name + identical content (hash or text) -> already have it
        if (chash and existing_hash and chash == existing_hash) or (body and body == existing_body):
            return "skip_dup"
        # same name, different content -> a supersede; a human decides
        return "conflict"
    return "clear"


def find_collisions(cur, candidate):
    """DB lookups for classify(): return (dup_row, name_row) for a candidate.
    Only LIVE rows (deleted_at IS NULL) count. Uses a RealDict-style cursor (rows as dicts)."""
    chash = candidate.get("content_hash")
    name = candidate.get("name")
    dup_row = None
    if chash:
        cur.execute("SELECT id, name, body, content_hash FROM memory "
                    "WHERE content_hash=%s AND deleted_at IS NULL LIMIT 1", (chash,))
        dup_row = cur.fetchone()
    name_row = None
    if name:
        cur.execute("SELECT id, name, body, content_hash FROM memory "
                    "WHERE name=%s AND deleted_at IS NULL LIMIT 1", (name,))
        name_row = cur.fetchone()
    return dup_row, name_row


def verdict_for(cur, candidate):
    """Convenience: DB lookups + classify in one call."""
    dup_row, name_row = find_collisions(cur, candidate)
    return classify(candidate, dup_row=dup_row, name_row=name_row)
