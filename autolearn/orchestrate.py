"""Auto-learn orchestration (Big Step 6, Phase B/decision layer).

Ties the pieces into ONE decision per candidate:

    transcript lines -> provenance.tag_transcript -> scrub.scrub_spans
        -> extract.extract_session (LLM drafts + structural trust verdict)
        -> decide_one(): auto_keep | escalate | skip | drop

The gate is DELIBERATELY deterministic — no model gets a vote on the verdict, so the
author==validator failure mode can't occur. A candidate is auto-kept ONLY when ALL hold:
  * trust == 'trusted'        (every cited channel first-party + anti-spoof clean, from Phase A/B)
  * sensitivity in public/normal   (sensitive/secret always go to a human)
  * no lesson match           (not something we/the operator already rejected)
  * corroborated              (carries a real first-party anchor span)
  * conflict verdict == 'clear'    (not a dup, not a name-collision overwrite)
Otherwise: 'skip' (exact dup — nothing to do), 'drop' (matched a critical lesson — never
queue), or 'escalate' (everything else -> the /approve queue for a human).

This module is import-light and DB-agnostic: `sensitivity_fn`, `conflict_fn`, and
`lesson_rows` are injected. The dry-run default (no conflict_fn) treats conflict as 'clear'
so the full path is exercisable with zero DB / zero network in tests.
"""
import re

from . import provenance as P
from . import scrub as S
from . import extract as E
from . import lessons as L

# Deterministic sensitivity bump: presence of any of these (word-boundary, case-insensitive)
# marks a candidate sensitive so it routes to a human, never auto-kept. Over-flagging is safe
# (it only sends MORE to the operator). Secrets themselves are already scrubbed upstream; this is about
# the TOPIC being private (medical/health/financial), per the sensitivity-routing hard rule.
_SENSITIVE_TERMS = (
    "medical", "health", "diagnos", "patient", "prescription", "symptom", "illness",
    "financial", "finance", "bank", "salary", "income", "invoice", "tax", "credit card",
    "passport", "national id", "ssn", "biometric", "password", "secret", "private key",
)
_SENSITIVE_RE = re.compile(r"(?i)\b(?:%s)" % "|".join(t.replace(" ", r"\s+") for t in _SENSITIVE_TERMS))


def sensitivity_of(candidate, floor="normal"):
    """Deterministic topic-sensitivity for a candidate. Returns 'sensitive' if any private
    topic term appears, else `floor`. Never LOWERS a caller-supplied sensitivity."""
    text = " ".join(str(candidate.get(k) or "") for k in ("name", "description", "proposed_body", "body"))
    if _SENSITIVE_RE.search(text):
        return "sensitive"
    return candidate.get("sensitivity") or floor


def effective_trust(proposal):
    """Re-derive trust deterministically from the cited channels (NOT the proposal's trust
    field), then AND it with what the pipeline reported. Both must say 'trusted' to be
    trusted — so a buggy/compromised pipeline can only ever LOWER trust, never raise it.
    The pipeline's anti-spoof quarantine (which needs span text the validator lacks) is
    therefore honored as a floor."""
    server = P.trust_for_channels(proposal.get("cited_channels") or ())
    claimed = proposal.get("trust") or "quarantined"
    return "trusted" if (server == "trusted" and claimed == "trusted") else "quarantined"


class Decision:
    """One candidate's verdict. `action` in {auto_keep, escalate, skip, drop}; `reasons`
    explains escalate/drop/skip; `trust`/`sensitivity` are the values the gate computed."""
    __slots__ = ("action", "reasons", "trust", "sensitivity", "lesson")

    def __init__(self, action, reasons, trust, sensitivity, lesson=None):
        self.action = action
        self.reasons = reasons
        self.trust = trust
        self.sensitivity = sensitivity
        self.lesson = lesson

    def __repr__(self):
        return "Decision(%s, reasons=%s, trust=%s, sens=%s)" % (
            self.action, self.reasons, self.trust, self.sensitivity)


def decide_one(proposal, *, lesson_rows=None, conflict_verdict="clear"):
    """Pure decision for one proposal payload (as built by extract.build_proposal).
    `conflict_verdict` from conflict.py ('clear'|'skip_dup'|'conflict'); default 'clear'
    for dry-run without a DB. `lesson_rows` from lessons.load_active (or a fixture)."""
    reasons = []
    trust = effective_trust(proposal)
    sensitivity = sensitivity_of(proposal)

    # exact dup short-circuits everything — there is simply nothing to add
    if conflict_verdict == "skip_dup":
        return Decision("skip", ["duplicate"], trust, sensitivity)

    # a critical lesson means DROP (never queue); a lesser lesson escalates
    lz = L.matches_lesson(proposal, lesson_rows)
    if lz is not None:
        if L.lesson_action(lz) == "drop":
            return Decision("drop", ["lesson:%s" % lz.get("title")], trust, sensitivity, lesson=lz)
        reasons.append("lesson:%s" % lz.get("title"))

    if trust != "trusted":
        reasons.append("untrusted")
    if sensitivity in ("sensitive", "secret"):
        reasons.append("sensitive")
    if conflict_verdict == "conflict":
        reasons.append("conflict")
    if not L.corroborated(proposal.get("cited_channels")):
        reasons.append("uncorroborated")

    return Decision("auto_keep" if not reasons else "escalate", reasons, trust, sensitivity, lesson=lz)


# Reasons that REQUIRE a human/manager and so must NEVER auto-land as an author personal note:
# a private topic (sensitive/secret), or a same-name supersede (conflict). "untrusted" and
# "uncorroborated" are NOT blocking — a quarantined own-session capture lands personal and is
# validated against its source at recall (validate-on-recall, project_doc design-recall-validation).
_HUMAN_ONLY_REASONS = frozenset({"sensitive", "conflict"})
# (Approval 2.0 step 1): when land_sensitive is on, 'sensitive' NO LONGER blocks personal landing — a
# sensitive fact lands author-only (protected by the personal tier, not a human queue) and reaches other
# agents only through the manager share-gate.
# (Approval 2.0 step 3): 'conflict' (a same-name capture whose content differs = a supersede) is no
# longer unconditionally human-only. When the caller passes allow_supersede=True — set ONLY when the live
# same-name note is owned by the SAME author AND is not an already manager-validated shared/trusted fact —
# 'conflict' stops blocking, so the correction lands as the author's personal note and apply_proposal's
# retire_prior supersedes the stale row (+ a supersedes edge). A cross-author same-name, or a correction to
# a shared/trusted fact, keeps allow_supersede=False and still escalates to a human.
_HUMAN_ONLY_REASONS_SENSITIVE_OK = frozenset({"conflict"})


def lands_personal(decision, *, has_source, same_name, land_sensitive=False, allow_supersede=False):
    """Does this decided candidate land as the AUTHOR'S OWN personal note (recallable now, self-
    validated against its source at recall) vs go to the human/manager queue?

    True when it is session-backed, a NEW name, not a skip/drop, carries no human-only reason
    (sensitive/conflict), and matched no rejection lesson. `decision.trust` MAY be quarantined —
    that is the point: the author recalls it flagged untrusted and validates it before relying on
    it. This widens the old auto_keep-only landing ( v2) to every clean own-session capture
    (Approval 2.0 build 1/5) — cross-agent trust still needs the manager, so author!=validator
    holds for anything OTHER agents consume.

    `allow_supersede`: when True, a SAME-NAME candidate is allowed to land (so it supersedes
    the prior row) and 'conflict' stops being a blocking reason. The caller gates this on same-author
    + not-already-shared/trusted, so this never silently overwrites another agent's or a validated
    shared fact."""
    if decision.action in ("skip", "drop"):
        return False
    if not has_source or (same_name and not allow_supersede):
        return False
    if decision.lesson is not None:                     # matched a known rejection -> human eyes
        return False
    blocking = set(_HUMAN_ONLY_REASONS_SENSITIVE_OK if land_sensitive else _HUMAN_ONLY_REASONS)
    if allow_supersede:
        blocking.discard("conflict")                    # same-author correction may supersede
    return not (blocking & set(decision.reasons or ()))


def evaluate(transcript_lines, backend, *, session_id=None, author_body="manager",
             lesson_rows=None, conflict_fn=None, allow_cloud=False):
    """Full dry-run path: tag -> scrub -> extract -> decide. Returns a list of
    (proposal, Decision). Posts NOTHING and writes NOTHING. `conflict_fn(proposal)->verdict`
    is injected for a DB-backed conflict check; absent => 'clear' (pure dry-run)."""
    spans = S.scrub_spans(P.tag_transcript(transcript_lines))
    proposals = E.extract_session(spans, backend, session_id=session_id, author_body=author_body)
    out = []
    for prop in proposals:
        cv = conflict_fn(prop) if conflict_fn else "clear"
        out.append((prop, decide_one(prop, lesson_rows=lesson_rows, conflict_verdict=cv)))
    return out
