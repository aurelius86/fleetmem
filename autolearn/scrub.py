"""Deterministic secret-scrub (Big Step 6, Phase B).

Hard-rule compliance: transcripts are scrubbed of secrets BEFORE any extractor
(local OR cloud) ever sees them. Deterministic regex/shape patterns only — no
model in the loop, so it can't be talked out of redacting. This is the single
canonical scrubber for the brain-v2 pipeline (the old propose-memories.py /
import_legacy.py copies drifted — see; new code uses THIS).

scrub() is idempotent and conservative: it over-redacts rather than risk a leak.
Returned text keeps shape (a placeholder per hit) so the extractor still reads
naturally. Never log or persist the pre-scrub text downstream.
"""
import re

# Each pattern -> placeholder. Order matters a little (more specific first).
_PATTERNS = [
    # 1Password references (path or item UUIDs) — paths, never values, but redact anyway
    (re.compile(r"op://[^\s\"'`)]+"), "«op-ref»"),
    # PEM private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "«pem-key»"),
    # JWTs (three base64url segments)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"), "«jwt»"),
    # provider token shapes
    (re.compile(r"\b(?:sk-|ghp_|gho_|ghu_|ghs_|github_pat_|xox[baprs]-|AKIA|ASIA)[A-Za-z0-9._-]{8,}"), "«token»"),
    # webhook / hook URLs (whole URL is a secret)
    (re.compile(r"https?://[^\s\"'`)]*(?:webhook|hook)[^\s\"'`)]*", re.I), "«webhook-url»"),
    # key = value / key: value secret assignments
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|bearer|auth)\b\s*[:=]\s*\S+"),
     r"\1=«redacted»"),
    # long hex (>=40) — sha/keys
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), "«hex»"),
    # long base64-ish blobs (>=50)
    (re.compile(r"\b[A-Za-z0-9+/]{50,}={0,2}\b"), "«b64»"),
]


def scrub(text):
    """Redact secret-shaped substrings. Idempotent; safe to call more than once."""
    if not text:
        return text or ""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_spans(spans):
    """Return a copy of provenance spans with each span's text scrubbed."""
    return [dict(s, text=scrub(s.get("text", ""))) for s in spans]
