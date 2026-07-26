"""Lessons store + cross-time corroboration (Big Step 6, Phase D) — poison defense.

A `lesson` (migration 0001) records a poison/rejection SIGNATURE so the pipeline does
not relearn something it (or the operator) already rejected. Two deterministic uses:

  * matches_lesson() — consulted on EVERY auto-keep candidate. If the candidate's text
    matches a known lesson pattern, it is blocked. A 'critical' lesson => drop outright
    (never even queue it); anything lower => escalate to a human. Patterns are matched as
    case-insensitive substrings (a model never decides the match -> no author==validator).
  * record_lesson() — write a new lesson when the pipeline auto-rejects something, or when
    the operator hits Drop in /approve (so the same junk can't come back next session).

Cross-time corroboration (corroborated()) is the OTHER half of the locked rule: trust is
content-origin, and corroboration is across TIME, never cross-body consensus (managers share
one brain, so "two bodies agree" is one witness, not two). For auto-keep we already require a
first-party origin; corroborated() is the belt-and-suspenders assertion that a candidate
actually carries a first-party anchor span, so a future relaxation of the gate can't quietly
auto-keep an anchorless claim. It does NOT lower trust on its own; it's a required-True check.
"""
import re

from . import provenance as P

_norm_ws = re.compile(r"\s+")


def _norm(text):
    return _norm_ws.sub(" ", (text or "").lower()).strip()


def matches_lesson(candidate, lesson_rows):
    """Return the first matching lesson row (dict with 'pattern'/'title'/'severity'), or None.
    Match = the lesson's (non-empty) pattern appears as a substring of the candidate's
    name+description+body (all lowercased, whitespace-normalised). Deterministic."""
    hay = _norm(" ".join(str(candidate.get(k) or "") for k in ("name", "description", "proposed_body", "body")))
    if not hay:
        return None
    for lz in lesson_rows or ():
        pat = _norm(lz.get("pattern"))
        if pat and pat in hay:
            return lz
    return None


def lesson_action(lesson_row):
    """How a matched lesson gates the candidate: 'drop' for critical (never queue),
    else 'escalate' (a human decides). Unknown/missing severity -> escalate (fail-safe)."""
    if lesson_row and str(lesson_row.get("severity")).lower() == "critical":
        return "drop"
    return "escalate"


def corroborated(cited_channels):
    """True iff the candidate traces to at least one FIRST-PARTY channel (the operator typed it /
    manager reasoning) — a real anchor across the session, not laundered external text.
    Cross-TIME corroboration, not cross-body consensus."""
    return bool(set(cited_channels or ()) & P.FIRST_PARTY)


_LESSON_SEVERITIES = ("low", "normal", "high", "critical")


def record_lesson(cur, *, title, pattern, severity="normal", source_proposal_id=None):
    """INSERT a lesson signature. Idempotent-ish: skips if an identical (title, pattern)
    lesson already lives. Returns the lesson id (new or existing). severity is whitelisted
    to the CHECK values (unknown -> 'normal') so a bad value can't CheckViolation-rollback the
    caller's whole decide transaction."""
    if severity not in _LESSON_SEVERITIES:
        severity = "normal"
    cur.execute("SELECT id FROM lesson WHERE title=%s AND pattern=%s AND deleted_at IS NULL LIMIT 1",
                (title, pattern))
    row = cur.fetchone()
    if row:
        return row[0] if not isinstance(row, dict) else row.get("id")
    cur.execute("INSERT INTO lesson(title, pattern, severity, source_proposal_id, trust, origin_channel) "
                "VALUES (%s,%s,%s,%s,'trusted','agent-reasoning') RETURNING id",
                (title, pattern, severity, source_proposal_id))
    out = cur.fetchone()
    return out[0] if not isinstance(out, dict) else out.get("id")


def load_active(cur):
    """Fetch live lesson signatures for the gate. RealDict-style cursor -> list of dicts."""
    cur.execute("SELECT id, title, pattern, severity FROM lesson WHERE deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) "
                "AND (invalid_at IS NULL OR invalid_at > now())")   # don't gate on expired/invalidated lessons
    return list(cur.fetchall())
