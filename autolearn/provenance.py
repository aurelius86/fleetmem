"""Deterministic provenance tagging + trust verdict (Big Step 6, Phase A).

THE LOAD-BEARING SECURITY RULE (locked by the adversarial review):
trust = where the CONTENT came from, NOT which session it was in. managers are
LLMs, so a poisoned web page read mid-"trusted" session must never auto-keep —
that is the forbidden author==validator pattern. So provenance is decided HERE,
deterministically, from the transcript's structure — never guessed by an LLM.

What this module does:
  * walk a Claude Code session transcript (JSONL, already parsed to dicts) and
    classify every content span into ONE origin channel (the contract.py vocab);
  * expose the deterministic trust verdict for any SET of channels a candidate
    fact traces to: trusted iff every contributing channel is first-party,
    else quarantined.

What it deliberately does NOT do: decide which spans a given extracted fact came
from. That attribution is the extractor's job (Phase B); it must emit the span
indices it used, then call `trust_for_channels()` on their channels. When in
doubt the extractor should over-include spans — more channels can only LOWER
trust (quarantine), never falsely raise it. Fail-closed by construction.

No third-party deps — must run on the mini, the legacy host, or the API box unchanged.
"""

# Channels must match contract.py ORIGIN_CHANNELS exactly.
HUMAN_INPUT = "human-input"
AGENT_REASONING = "agent-reasoning"
WEB_FETCH = "web-fetch"
TOOL_OUTPUT = "tool-output"
FILE_READ = "file-read"
UNKNOWN = "unknown"

# First-party = the only origins that may auto-keep. Everything else is a path
# by which externally-authored (and therefore potentially poisoned) text enters.
FIRST_PARTY = frozenset({HUMAN_INPUT, AGENT_REASONING})

# Tool name -> the channel its OUTPUT (tool_result) carries. The map is about
# where the RESULT TEXT originates, not what the tool is "for":
#   * web/search tools surface remote pages         -> web-fetch
#   * file/Glob/Grep surface on-disk content         -> file-read (a file can hold injected text)
#   * everything else (Bash, MCP, subagents, edits) -> tool-output
# A Bash `curl` or an MCP call can return arbitrary external content, so the safe
# default for anything unrecognised is tool-output (non-first-party -> quarantine).
_TOOL_CHANNEL = {
    "webfetch": WEB_FETCH,
    "websearch": WEB_FETCH,
    "read": FILE_READ,
    "notebookread": FILE_READ,
    "glob": FILE_READ,
    "grep": FILE_READ,
    "ls": FILE_READ,
}


def channel_for_tool(name):
    """Map a tool name (case-insensitive) to the channel its result carries.
    Unrecognised / missing -> tool-output (the fail-closed, non-first-party default)."""
    if not name:
        return TOOL_OUTPUT
    return _TOOL_CHANNEL.get(str(name).strip().lower(), TOOL_OUTPUT)


def trust_for_channels(channels):
    """Deterministic content-origin verdict for the set of channels a candidate
    fact traces to. 'trusted' iff EVERY channel is first-party; else 'quarantined'.
    Empty set -> quarantined (a fact with no known origin is never auto-trusted)."""
    chans = set(channels or ())
    if not chans:
        return "quarantined"
    return "trusted" if chans <= FIRST_PARTY else "quarantined"


def _is_meta(line):
    """Harness-injected user-role content (system reminders, slash-command
    expansions, hook output) is NOT the operator typing. Treat it as non-first-party so a
    recalled-memory reminder or injected note can't masquerade as human input."""
    if line.get("isMeta") or line.get("is_meta"):
        return True
    msg = line.get("message") or {}
    content = msg.get("content")
    text = content if isinstance(content, str) else ""
    if isinstance(content, list):
        text = "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return "<system-reminder>" in text or "<command-name>" in text


def _blocks(content):
    """Normalise a message .content into a list of block dicts. A plain string
    becomes one synthetic text block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def tag_transcript(lines):
    """Walk parsed transcript lines (list of dicts, in order) and return a list of
    Span dicts: {idx, role, channel, tool_name, text, ts}.

    Rules (deterministic, structural — never look at the prose to decide origin):
      * assistant text/thinking            -> agent-reasoning
      * assistant tool_use                 -> records tool_use_id -> tool name (no span emitted;
                                              it's the agent's ACTION, judged by the result it yields)
      * user text, genuine                 -> human-input
      * user text, harness/meta            -> tool-output (non-first-party; see _is_meta)
      * user tool_result                   -> channel of the tool that produced it
                                              (via the tool_use_id map; unknown id -> tool-output)
    Non user/assistant lines (system, summaries) are skipped.
    """
    tool_by_id = {}                 # tool_use_id -> tool name, learned from assistant tool_use blocks
    spans = []
    for idx, line in enumerate(lines):
        typ = line.get("type")
        if typ not in ("user", "assistant"):
            continue
        msg = line.get("message") or {}
        ts = line.get("timestamp")
        blocks = _blocks(msg.get("content"))

        if typ == "assistant":
            for b in blocks:
                bt = b.get("type")
                if bt == "tool_use":
                    tuid = b.get("id")
                    if tuid:
                        tool_by_id[tuid] = b.get("name")
                elif bt in ("text", "thinking"):
                    txt = (b.get("text") or b.get("thinking") or "").strip()
                    if txt:
                        spans.append({"idx": idx, "role": "assistant", "channel": AGENT_REASONING,
                                      "tool_name": None, "text": txt, "ts": ts})
            continue

        # user line
        meta = _is_meta(line)
        for b in blocks:
            bt = b.get("type")
            if bt == "tool_result":
                name = tool_by_id.get(b.get("tool_use_id"))
                spans.append({"idx": idx, "role": "tool", "channel": channel_for_tool(name),
                              "tool_name": name, "text": _result_text(b.get("content")), "ts": ts})
            elif bt == "text":
                txt = (b.get("text") or "").strip()
                if not txt:
                    continue
                spans.append({"idx": idx, "role": "user",
                              "channel": (TOOL_OUTPUT if meta else HUMAN_INPUT),
                              "tool_name": None, "text": txt, "ts": ts})
    return spans


def _result_text(content):
    """Flatten a tool_result.content (string, or list of text blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def trust_for_spans(spans):
    """Convenience: verdict for a list of Span dicts (uses their channels)."""
    return trust_for_channels(s.get("channel") for s in spans)


# --------------------------------------------------------- audit-against-source ----
# The "teeth" (DeepTutor consolidator/modes/audit.py): a synthesized memory is only as trustworthy
# as the evidence it cites. audit_support() scores, deterministically, how much of the memory BODY is
# actually grounded in the cited spans' text. A near-zero score means the body asserts things the
# cited evidence does not contain — over-synthesis or laundering — and is worth flagging at synthesis
# time. Deterministic + LLM-free by design (same stance as the trust verdict above): never ask a model
# to grade its own output against the source.
import re as _re

_WORD_RE = _re.compile(r"[a-z0-9]+")
# tiny stoplist — drop the highest-frequency function words so the score reflects CONTENT overlap,
# not shared grammar. Deliberately small; the >=3-char filter already removes most noise.
_STOP = frozenset((
    "the and or of to in on for is are was were be been being as at by with from this that these those "
    "it its it's a an if then else not no yes but so than too very can will would should could may might "
    "have has had do does did done use used using via per not any all each our your their his her"
).split())

# below this fraction of the body grounded in cited evidence, flag the memory as weakly supported
AUDIT_WEAK_THRESHOLD = 0.35


def _sig_tokens(text):
    """Significant (content) tokens of a text: lowercase alnum words >=3 chars, minus the stoplist."""
    return {t for t in _WORD_RE.findall((text or "").lower()) if len(t) >= 3 and t not in _STOP}


def audit_support(body, evidence_texts):
    """Fraction (0..1, rounded) of the memory body's significant tokens that also appear in the union
    of the cited evidence texts. 1.0 = fully grounded in what was cited; near 0 = the body claims
    things the cited spans don't support. Empty body or no evidence -> 0.0 (nothing to stand on)."""
    b = _sig_tokens(body)
    if not b:
        return 0.0
    evidence = set()
    for t in (evidence_texts or ()):
        evidence |= _sig_tokens(t)
    if not evidence:
        return 0.0
    return round(len(b & evidence) / len(b), 4)


def audit_verdict(score, threshold=AUDIT_WEAK_THRESHOLD):
    """'supported' if the support score meets the threshold, else 'weak' (worth a human look)."""
    return "supported" if (score is not None and score >= threshold) else "weak"
