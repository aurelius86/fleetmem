"""Candidate extraction (Big Step 6, Phase B).

Pipeline: scrubbed provenance spans -> an extractor LLM drafts candidate memories,
each CITING the span indices it used -> deterministic trust verdict from those
spans' channels (Phase A) -> a /propose payload. The LLM proposes; it never gets
to declare trust — trust is computed from structure, with an anti-spoof backstop.

Backends are pluggable and sensitivity-routed (the operator: support local AND
cloud for safe-mode):
  * sensitive/secret  -> LOCAL only (never leaves the LAN)
  * public/normal     -> prefer local, may use cloud if allow_cloud
A backend is just `generate(prompt, system=None) -> str`. MockBackend makes the
whole deterministic path unit-testable with no external call.

This module does NOT call the network or post to /propose by itself unless asked
(dry-run by default). Live wiring (deploy on the legacy host, real LLM, real POST) is a
separately-gated step.
"""
import os
import hashlib
import json
import re
import urllib.request
from collections import Counter

from contract import compute_content_hash   # the ONE canonical content-hash formula
from . import provenance as P

# Severity order for collapsing a candidate's cited channels into ONE origin_channel
# for the proposal record (the most-external wins — it's what drove the verdict).
_SEVERITY = [P.WEB_FETCH, P.TOOL_OUTPUT, P.FILE_READ, P.AGENT_REASONING, P.HUMAN_INPUT, P.UNKNOWN]

# Anti-spoof: if a candidate body's significant tokens overlap an EXTERNAL span's
# tokens at/above this fraction, force quarantine even if the LLM cited only
# first-party spans (catches the model laundering external text as first-party).
OVERLAP_QUARANTINE = 0.60

#: audit-grounded rubric. The previous 383-char prompt said only "durable,
# reusable ... worth remembering long-term" with NO reject criteria, so the extractor happily
# emitted session-state, task-board mirrors and analysis snapshots — the five junk shapes a
# 150-capture graded audit actually found (notes/autolearn-quality-audit-2026-07-23.md).
# A/B on real transcripts (warm 30b, real provenance->scrub->extract path): OLD 11 candidates
# / ~45% junk -> NEW 7 candidates / ~29% junk, with all 3 durable preferences retained.
# NOTE this is the SECONDARY lever; the primary was the judge-model fix. Deliberately
# NOT a "be more selective" instruction — that was A/B'd in and REGRESSED decision recall
# (see gotcha_qwen3_extraction_prompt_regresses_decision_recall). Naming the junk CATEGORIES
# works where a vague selectivity nudge does not.
_SYSTEM = (
    "You extract DURABLE, REUSABLE memory candidates from an assistant session — facts a future "
    "session would need and could NOT re-derive from the code, task board, or git.\n"
    "KEEP: stable infra/config facts; decisions + their rationale; user preferences; reusable "
    "gotchas/root-causes; security/access rules; hard-won technical facts (API shapes, protocols, "
    "endpoint/file semantics).\n"
    "REJECT (return nothing for these):\n"
    "- session/run STATE — 'verification passed', 'lint/build ok', 'byte-identical', 'no errors', "
    "'screenshot NxN', 'service restarted', 'X imported successfully';\n"
    "- task-board MIRRORS — 'Task Txxx assigned/open/in-progress';\n"
    "- ANALYSIS SNAPSHOTS that change with inputs — computed numbers like 'terminal value -$4.08M', "
    "'cumulative profit $554,309', counts/sizes/hashes;\n"
    "- CODE LOCATIONS that drift — 'writer is at lines 113-120', 'call X at end of render';\n"
    "- LABEL-ONLY entries with no concrete fact in the body;\n"
    "- facts already in the brain (listed below).\n"
    "Test each candidate: will this STILL be true and USEFUL to recall next month? If not, drop it. "
    "Prefer FEWER, higher-value candidates; an EMPTY list is correct for a routine work session.\n"
    "For EACH candidate you MUST list every source span index whose content the fact depends on "
    "(over-citing is safe; under-citing is not; never invent spans). Output strict JSON only."
)


# ---------------------------------------------------------------- backends ----
class MockBackend:
    """Returns a fixed string (or a callable's output). For tests / dry design."""
    name = "mock"

    def __init__(self, response):
        self._r = response

    def generate(self, prompt, system=None):
        return self._r(prompt) if callable(self._r) else self._r


class LocalBackend:
    """qwen3 on the legacy host via the existing admin channel. Kept on-LAN (safe-mode)."""
    name = "local"

    def __init__(self, runner=None, model=os.environ.get("EXTRACT_MODEL", "qwen3:14b")):
        self.model = model
        self._runner = runner  # injectable; live wiring sets the ssh/pct-exec call

    def generate(self, prompt, system=None):
        if self._runner is None:
            raise RuntimeError("LocalBackend has no runner wired (live step not configured)")
        return self._runner(prompt, system, self.model)


class CloudBackend:
    """Claude (secured cloud). Allowed ONLY for non-sensitive data per the routing rule."""
    name = "cloud"

    def __init__(self, caller=None, model="claude-opus-4-8"):
        self.model = model
        self._caller = caller  # injectable; live wiring sets the Anthropic SDK call

    def generate(self, prompt, system=None):
        if self._caller is None:
            raise RuntimeError("CloudBackend has no caller wired (live step not configured)")
        return self._caller(prompt, system, self.model)


class OllamaBackend:
    """server-side extraction via the local model host Ollama /api/generate — the brain host reaches
    Ollama directly over the mesh (same call style as api.py's embed + llm_rerank), replacing the old
    ssh->pct exec 200->review_llm.py coupling. Self-contained (Ollama is the only external dependency).
    On-LAN + local model, so safe for sensitive data (never leaves the homelab)."""
    name = "ollama"

    def __init__(self, url=None, model=None, timeout=120):
        self.url = url or os.environ.get("OLLAMA_GEN_URL", "http://127.0.0.1:11434/api/generate")
        self.model = model or os.environ.get("EXTRACT_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")
        self.timeout = timeout

    def generate(self, prompt, system=None):
        # pass num_ctx explicitly (stock models truncate silently at 2048-8192) and force valid
        # JSON with format=json — kills the parse-failure class parse_candidates_checked exists to catch.
        # The tolerant regex parse stays as a fallback for non-Ollama backends.
        payload = {"model": self.model, "think": False, "prompt": prompt, "stream": False, "format": "json",
                   "options": {"temperature": 0, "num_ctx": EXTRACT_NUM_CTX}}
        if system:
            payload["system"] = system
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read()).get("response", "")
        if "</think>" in resp:                     # strip any qwen3 hybrid <think> preamble (parse is tolerant anyway)
            resp = resp.split("</think>", 1)[-1]
        return resp


class SensitiveTopicToCloud(RuntimeError):
    """Raised (fail-closed) when extraction is asked to send private-topic spans to a CLOUD backend."""


# Fallback private-topic regex — used ONLY if the canonical orchestrate list can't be imported (never in
# practice), so the cloud-routing guard is never silently disabled. Canonical: orchestrate._SENSITIVE_TERMS.
_TOPIC_FALLBACK_RE = re.compile(
    r"(?i)\b(?:medical|health|diagnos|patient|prescription|symptom|illness|financial|finance|bank|"
    r"salary|income|invoice|tax|credit\s+card|passport|national\s+id|ssn|biometric|private\s+key)")


def has_sensitive_topic(text):
    """True if TEXT contains a private TOPIC (medical/health/financial/...). Reuses the SINGLE canonical
    term list in orchestrate (lazy import dodges the orchestrate<->extract cycle); falls back to a local
    regex so the guard is never disabled. Belt to scrub's suspenders: scrub redacts secret SHAPES, this
    catches private TOPICS the model must not hand to a CLOUD extractor (sensitivity-routing hard rule)."""
    if not text:
        return False
    try:
        from .orchestrate import _SENSITIVE_RE as rx
    except Exception:
        rx = _TOPIC_FALLBACK_RE
    return bool(rx.search(text))


def _spans_text(spans):
    return " ".join(str((s or {}).get("text") or "") for s in (spans or []))


def pick_backend(sensitivity, local, cloud=None, allow_cloud=False, text=None):
    """Route by sensitivity. sensitive/secret -> local ONLY (never cloud). Otherwise prefer local; use
    cloud only when allow_cloud and a cloud backend is given. `local` is required (the always-available,
    on-LAN path). if `text` is given and carries a private TOPIC, force local regardless of the
    DECLARED sensitivity (which is coarse/unknown at extract time)."""
    if local is None:
        raise ValueError("a local backend is required (safe default)")
    if str(sensitivity).lower() in ("sensitive", "secret"):
        return local
    if text is not None and has_sensitive_topic(text):
        return local
    if allow_cloud and cloud is not None:
        return cloud
    return local


# Prompt-size budget for ONE extraction call. Keeps the rendered span list well
# under the local model's context (qwen3 num_ctx=16384 tokens) AND under the base64-as-CLI-arg
# limit the runner uses. A long session (the 2.1MB / 254-span build session HTTP-400'd the
# model) is split into windows, each extracted on its own, candidates de-duped across windows.
MAX_PROMPT_CHARS = int(os.environ.get("EXTRACT_MAX_PROMPT_CHARS", "24000"))   # tied to EXTRACT_MODEL's context window
_SPAN_CAP = int(os.environ.get("EXTRACT_SPAN_CAP", "600"))  # per-span text cap (mirrors build_prompt)
# num_ctx the OllamaBackend passes EXPLICITLY. MAX_PROMPT_CHARS assumes the model can hold the
# whole prompt (~chars/3.5 tokens for dense JSON/code) + system + JSON output. A public box's STOCK
# model defaults to num_ctx 2048-8192 and would SILENTLY truncate the prompt -> bad/empty extractions
# with no error. Derive from the prompt cap with output headroom, floored to the fleet-proven 16384.
EXTRACT_NUM_CTX = int(os.environ.get("EXTRACT_NUM_CTX", str(max(16384, MAX_PROMPT_CHARS // 3 + 2048))))


def _span_render_len(s):
    """Approx chars a span adds to the prompt: capped text + the '[idx] (channel) ' + newline."""
    return min(len(s.get("text") or ""), _SPAN_CAP) + 24


def chunk_spans(spans, max_chars=MAX_PROMPT_CHARS):
    """Greedily pack spans into windows whose rendered prompt stays under max_chars. A single
    over-budget span still gets its own window (its text is 600-capped at render, so a window
    can't actually overflow the model)."""
    chunks, cur, cur_len = [], [], 0
    for s in spans:
        l = _span_render_len(s)
        if cur and cur_len + l > max_chars:
            chunks.append(cur); cur, cur_len = [], 0
        cur.append(s); cur_len += l
    if cur:
        chunks.append(cur)
    return chunks


# ----------------------------------------------------------------- prompt ----
def build_prompt(spans, known=None):
    """Render scrubbed spans as a numbered list the model cites by index.

    `known` = memories this session already recalled; rendered as a "do not re-propose"
    block so the model skips facts already in the brain (dedup context). Accepts dicts
    (name/description) or plain strings; None/empty = no dedup block. (Fix the param
    was passed by the caller since but never existed here → TypeError broke autolearn.)"""
    lines = ["Session spans (cite by [idx]):", ""]
    for i, s in enumerate(spans):
        tag = s.get("channel", P.UNKNOWN)
        txt = (s.get("text") or "").replace("\n", " ").strip()
        if len(txt) > 600:
            txt = txt[:600] + "…"
        lines.append("[%d] (%s) %s" % (i, tag, txt))
    if known:
        kn = []
        for k in known:
            if isinstance(k, dict):
                s = ("%s — %s" % (k.get("name") or "", k.get("description") or "")).strip(" —")
            else:
                s = str(k).strip()
            if s:
                kn.append(s)
        if kn:
            lines += ["", "Already in the brain (do NOT re-propose these facts):"]
            lines += ["- %s" % x for x in kn]
    lines += [
        "",
        'Return JSON: {"candidates":[{"name":"slug_case","mtype":"reference|feedback|project|user",'
        '"description":"one line","body":"the fact","source_spans":[<idx>,...]}]}',
        "If nothing is worth remembering, return {\"candidates\":[]}.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------- parse/trust ----
def parse_candidates_checked(model_text):
    """Like parse_candidates but also reports whether the output was UNPARSEABLE.
    Returns (candidates, parse_failed). parse_failed is True only for a REAL malformation —
    non-empty model output that yields no {...} blob or fails json.loads — which is otherwise
    indistinguishable from a legitimate empty result and would silently disable learning if the
    model/prompt broke. An empty response (model generated nothing) and a valid empty
    {"candidates":[]} are NOT failures."""
    if not model_text or not model_text.strip():
        return [], False                          # nothing generated -> not a parse failure
    m = re.search(r"\{.*\}", model_text, re.S)     # first {...} blob
    if not m:
        return [], True                           # produced prose but no JSON object -> malformed
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return [], True                           # broken JSON -> malformed
    cands = obj.get("candidates") if isinstance(obj, dict) else None
    return ([c for c in cands if isinstance(c, dict)] if isinstance(cands, list) else []), False


def parse_candidates(model_text):
    """Pull the candidates array out of a model response (tolerant of code fences /
    surrounding prose). Returns [] on anything unparseable. Thin wrapper over
    parse_candidates_checked for callers that don't need the parse-failure signal."""
    return parse_candidates_checked(model_text)[0]


def _tokens(text):
    return {t for t in re.findall(r"[a-z0-9]{4,}", (text or "").lower())}


def _most_external(channels):
    for c in _SEVERITY:
        if c in channels:
            return c
    return P.UNKNOWN


def _overlap_spoof(body, external_spans):
    """True if the candidate body reproduces an external span's content (>= threshold
    of the body's significant tokens appear in some external span)."""
    bt = _tokens(body)
    if not bt:
        return False
    for s in external_spans:
        st = _tokens(s.get("text", ""))
        if st and len(bt & st) / len(bt) >= OVERLAP_QUARANTINE:
            return True
    return False


def assess(candidate, spans):
    """Compute the deterministic verdict for one candidate against the tagged spans.
    Returns (origin_channel, trust, cited_channels). Mis-cited / out-of-range span
    ids are ignored (treated as no support -> empties quarantine via Phase A rule)."""
    idxs = [i for i in (candidate.get("source_spans") or []) if isinstance(i, int) and 0 <= i < len(spans)]
    cited = [spans[i] for i in idxs]
    channels = {s.get("channel") for s in cited}
    trust = P.trust_for_channels(channels)
    # anti-spoof backstop: laundered external content -> quarantine regardless of citation
    if trust == "trusted":
        external = [s for s in spans if s.get("channel") not in P.FIRST_PARTY]
        if _overlap_spoof(candidate.get("body", ""), external):
            trust = "quarantined"
    return _most_external(channels) if channels else P.UNKNOWN, trust, channels


# per-cited-span evidence cap. The provenance table keeps the span text the fact was distilled
# from; a span can be long (a big tool-output), so cap what we persist — enough to audit against, not
# a full transcript copy.
_PROV_TEXT_CAP = int(os.environ.get("PROVENANCE_TEXT_CAP", "2000"))


def cited_spans_for(candidate, spans, text_cap=_PROV_TEXT_CAP):
    """Resolve a candidate's `source_spans` citations to evidence dicts for provenance capture.

    Returns [{span_idx, channel, ts, text}] for each DISTINCT cited span, mirroring assess()'s
    citation rule (out-of-range / non-int indices dropped). `span_idx` prefers the span's own
    transcript idx (stable within the session) and falls back to the citation position. Text is
    capped. This is what apply_proposal writes into memory_provenance at synthesis time."""
    idxs = [i for i in (candidate.get("source_spans") or []) if isinstance(i, int) and 0 <= i < len(spans)]
    out, seen = [], set()
    for i in idxs:
        s = spans[i] or {}
        span_idx = s.get("idx") if isinstance(s.get("idx"), int) else i
        if span_idx in seen:
            continue
        seen.add(span_idx)
        out.append({"span_idx": span_idx, "channel": s.get("channel"),
                    "ts": s.get("ts"), "text": (s.get("text") or "")[:text_cap]})
    return out


def build_proposal(candidate, spans, session_id=None, author_body="manager"):
    """Assemble the /propose payload from a candidate + its provenance verdict.

    `cited_channels` is carried through so a downstream validator (orchestrate / the API)
    can RE-DERIVE trust deterministically server-side instead of believing the `trust`
    field — defense in depth against a buggy or compromised pipeline.

    `cited_spans` carries the ACTUAL evidence spans the fact was distilled from, so
    apply_proposal can record provenance at synthesis time. Like `trust`, it is advisory to the
    server (the API re-derives from the posted spans); it is never fed back into the extractor
    prompt (provenance must not leak into generation — DeepTutor references.py:209)."""
    origin_channel, trust, channels = assess(candidate, spans)
    body = candidate.get("body", "")
    return {
        "name": candidate.get("name", ""),
        "mtype": candidate.get("mtype", "reference"),
        "description": candidate.get("description", ""),
        "body": body,
        "origin_channel": origin_channel,
        "trust": trust,
        "cited_channels": sorted(c for c in channels if c),
        "cited_spans": cited_spans_for(candidate, spans),
        "author_body": author_body,
        "source_session": session_id,
        "content_hash": compute_content_hash(candidate.get("name", ""), body),   # canonical name+body
    }


# ---------------------------------------------------- sibling merge ----
# Fragmentation guard: the extractor sometimes splits ONE unit of work into
# several near-sibling candidates (e.g. brain_metrics_implementation_{pattern,
# location,safety_pattern,...}). The per-candidate judge/backstop can't catch it
# (each note is judged in isolation), so we fold same-session siblings AFTER
# extraction. Deterministic + high-precision by design; the failure mode is
# graceful because a merge CONCATENATES bodies — no information is ever dropped.
_MERGE_MIN_PREFIX = 3      # shared leading name-tokens to call two notes prefix-siblings
_MERGE_MIN_JACCARD = 0.6   # body-token Jaccard to call two notes content-siblings


def _name_tokens(name):
    """Order-preserving meaningful name tokens (split on _/-, drop <3-char/generic)."""
    return [t for t in re.split(r"[_\-]+", (name or "").lower()) if len(t) >= 3]


def _shared_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _jaccard(a, b):
    return (len(a & b) / len(a | b)) if (a and b) else 0.0


def _are_siblings(p, q):
    """Same-session AND (shared >=3-token name prefix OR near-identical body)."""
    if (p.get("source_session") or None) != (q.get("source_session") or None):
        return False
    if _shared_prefix_len(_name_tokens(p.get("name", "")), _name_tokens(q.get("name", ""))) >= _MERGE_MIN_PREFIX:
        return True
    return _jaccard(_tokens(p.get("body", "")), _tokens(q.get("body", ""))) >= _MERGE_MIN_JACCARD


# ---(c): cross-session dedup ------------------------------------------------------------------
# _are_siblings gates on same source_session (the within-session merge). At INGEST we also want
# to catch the SAME fact captured in a DIFFERENT session (the store's dominant fragmentation source).
# Because the ingest path SKIPs the new capture (not CONCAT like the within-session merge), it MUST NOT
# fire when the two notes differ in a VALUE — that would silently drop a distinct fact (MTU 1300 vs
# 1344). So: the session-agnostic sibling relation AND a value-guard.
_VALUE_NUM_RE = re.compile(r"\d{2,}")
_VALUE_URL_RE = re.compile(r"https?://\S+")


def _value_compatible(text_a, text_b):
    """No CONFLICTING numbers (>=2 digits) or URLs: one side's set must be a subset of the other
    (equal, or one merely adds detail). Two distinct values -> NOT compatible (they are distinct facts)."""
    na, nb = set(_VALUE_NUM_RE.findall(text_a or "")), set(_VALUE_NUM_RE.findall(text_b or ""))
    if na and nb and not (na <= nb or nb <= na):
        return False
    ua, ub = set(_VALUE_URL_RE.findall(text_a or "")), set(_VALUE_URL_RE.findall(text_b or ""))
    if ua and ub and ua != ub:
        return False
    return True


def cross_session_sibling(name_a, body_a, name_b, body_b):
    """True iff two notes (from ANY sessions) are the same fact worth deduping at ingest:
    (shared >=3-token name prefix OR body-token Jaccard >= _MERGE_MIN_JACCARD) AND value-compatible
    (no conflicting numbers/URLs across name+body). Pure; used by the api.py ingest dedup ((c))."""
    name_sib = _shared_prefix_len(_name_tokens(name_a), _name_tokens(name_b)) >= _MERGE_MIN_PREFIX
    body_sib = _jaccard(_tokens(body_a), _tokens(body_b)) >= _MERGE_MIN_JACCARD
    if not (name_sib or body_sib):
        return False
    return _value_compatible((name_a or "") + " " + (body_a or ""),
                             (name_b or "") + " " + (body_b or ""))


def _merge_cluster(members):
    """Fold a cluster of sibling proposals into one. Bodies concatenated (deduped),
    trust the most conservative, channels unioned, hash recomputed."""
    tok_lists = [_name_tokens(m.get("name", "")) for m in members]
    common = tok_lists[0]
    for tl in tok_lists[1:]:
        common = common[:_shared_prefix_len(common, tl)]
    name = "_".join(common) if len(common) >= _MERGE_MIN_PREFIX else members[0].get("name", "")
    seen, bodies = set(), []
    for m in members:
        b = (m.get("body") or "").strip()
        if b and b not in seen:
            seen.add(b)
            bodies.append(b)
    body = "\n".join(bodies)
    channels = sorted({c for m in members for c in (m.get("cited_channels") or [])})
    trust = "trusted" if all(m.get("trust") == "trusted" for m in members) else "quarantined"
    mtype = Counter(m.get("mtype", "reference") for m in members).most_common(1)[0][0]
    desc = next((m.get("description") for m in members if m.get("description")), "")
    # union the members' cited-span evidence (same-session merge, so span_idx is comparable),
    # dedup by span_idx — the merged note is grounded in the union of what its siblings cited.
    cited_spans, seen_spans = [], set()
    for m in members:
        for cs in (m.get("cited_spans") or []):
            k = cs.get("span_idx")
            if k in seen_spans:
                continue
            seen_spans.add(k)
            cited_spans.append(cs)
    return {
        "name": name,
        "mtype": mtype,
        "description": desc,
        "body": body,
        "origin_channel": _most_external(set(channels)) if channels else members[0].get("origin_channel"),
        "trust": trust,
        "cited_channels": channels,
        "cited_spans": cited_spans,
        "author_body": members[0].get("author_body"),
        "source_session": members[0].get("source_session"),
        "content_hash": compute_content_hash(name, body),
    }


def merge_siblings(proposals):
    """Fold same-session near-sibling candidates into one note.

    Union-find over the pairwise sibling relation, so a transitive chain (a~b, b~c)
    forms one cluster. Order-stable (a cluster takes its first member's position);
    singletons and empty input pass through untouched. Pure — no I/O, no LLM."""
    n = len(proposals)
    if n < 2:
        return list(proposals)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _are_siblings(proposals[i], proposals[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups, order = {}, []
    for i in range(n):
        r = find(i)
        if r not in groups:
            groups[r] = []
            order.append(r)
        groups[r].append(i)
    out = []
    for r in order:
        members = [proposals[i] for i in groups[r]]
        out.append(members[0] if len(members) == 1 else _merge_cluster(members))
    return out


def extract_session(spans, backend, session_id=None, author_body="manager", known=None, diag=None):
    """Full Phase-B core (no network of its own): build prompt -> backend.generate ->
    parse -> assess each candidate -> proposal payloads. Spans should already be
    secret-scrubbed (see scrub.scrub_spans). Returns a list of /propose-ready dicts.

    `known` (already-recalled memories) is passed to build_prompt as dedup context.
    a long session is WINDOWED (chunk_spans) and each window extracted on its own so
    the model never sees an over-context prompt. Each window renders spans from index 0, and
    build_proposal assesses against THAT window's spans, so cited indices stay valid. Candidates
    are de-duped across windows by content hash (a fact repeated in two windows lands once)."""
    # `diag` is an optional caller-supplied dict; if given, we report parse-failure telemetry
    # (windows_unparseable / windows_total) so the API layer can log a visible autolearn_parse_error
    # action_log row — a broken model/prompt otherwise looks identical to "nothing to learn". The
    # return type is UNCHANGED (still the proposals list) so no existing caller breaks.
    # defense-in-depth: the live path always passes a LOCAL backend; but if a caller ever wires a
    # CLOUD extractor, refuse spans carrying a private TOPIC (medical/health/financial) — scrub removed
    # secret SHAPES upstream, NOT private topics, so this is the last guard before text leaves the LAN.
    if getattr(backend, "name", "") == "cloud" and has_sensitive_topic(_spans_text(spans)):
        raise SensitiveTopicToCloud("refusing cloud extraction: spans contain a private topic "
                                    "(medical/health/financial); route to a local backend")
    chunks = chunk_spans(spans)
    if len(chunks) <= 1:   # common case (short session): identical to the pre- single pass
        raw = backend.generate(build_prompt(spans, known=known), system=_SYSTEM)
        cands, failed = parse_candidates_checked(raw)
        if diag is not None:
            diag["windows_total"] = 1
            diag["windows_unparseable"] = 1 if failed else 0
        return merge_siblings([build_proposal(c, spans, session_id, author_body) for c in cands])
    out, seen, parse_errors = [], set(), 0
    for chunk in chunks:
        raw = backend.generate(build_prompt(chunk, known=known), system=_SYSTEM)
        cands, failed = parse_candidates_checked(raw)
        if failed:
            parse_errors += 1
        for c in cands:
            prop = build_proposal(c, chunk, session_id, author_body)
            key = prop.get("content_hash") or prop.get("name")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(prop)
    if diag is not None:
        diag["windows_total"] = len(chunks)
        diag["windows_unparseable"] = parse_errors
    return merge_siblings(out)
