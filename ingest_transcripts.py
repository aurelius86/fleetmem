#!/usr/bin/env python3
"""Ingest Claude Code .jsonl transcripts into the brain (session + session_turn) —.

Mirrors the PROVEN dashboard parser (graph-server.js getTranscript/blockText) and its
redactSecrets scrub, ported verbatim, so raw secrets NEVER land in the store. Idempotent by
source_session (re-run skips already-ingested chats). Embeds each turn via Ollama bge-m3.
Runs ON the brain host (local Postgres peer-auth + Ollama at the configured OLLAMA endpoint).

Usage (from /opt/brain-db/db):
  sudo -u brain python3 ingest_transcripts.py <dir> <agent_body> [--no-embed] [--limit N]
"""
import glob
import json
import os
import re
import sys

import psycopg2

from search import embed, vec_literal

# --- redactSecrets, ported verbatim from shared/projects/brain-dashboard/graph-server.js -----
_RULES = [
    (re.compile(r'op://[^\s"\'`)]+'), 'op://«redacted»'),
    (re.compile(r'https?://[^\s"\'`)]*(?:webhook|hook)[^\s"\'`)]*', re.I), '«redacted-webhook-url»'),
    (re.compile(r'\b(?:sk-|ghp_|gho_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9._-]{8,}'), '«redacted-token»'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+'), '«redacted-jwt»'),
    (re.compile(r'-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----'), '«redacted-private-key»'),
    (re.compile(r'((?:secret|token|api[_-]?key|password|passwd|bearer)\b[^\S\n]*[:=][^\S\n]*)\S+', re.I), r'\1«redacted»'),
    # space-form bearer tokens ("Authorization: Bearer <tok>") — the dashboard redactor misses these
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{10,}'), 'Bearer «redacted»'),
]


def redact(s):
    for rx, rep in _RULES:
        s = rx.sub(rep, s)
    return s


def block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return ""


def ingest_file(cur, path, agent, do_embed):
    src = os.path.splitext(os.path.basename(path))[0]
    cur.execute("SELECT 1 FROM session WHERE source_session=%s", (src,))
    if cur.fetchone():
        return "skip"                                   # idempotent — already ingested
    turns = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ln = line.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") not in ("user", "assistant"):
                continue                                # skip metadata/tool/system lines
            text = redact(block_text((o.get("message") or {}).get("content"))).strip()
            if not text:
                continue                                # drops thinking/tool_use/tool_result (no text block)
            turns.append((o["type"], o.get("timestamp"), text))
    if not turns:
        return "empty"
    cur.execute("INSERT INTO session(source_session,agent_body,started_at,ended_at,turn_count,"
                "sensitivity,origin_channel) VALUES (%s,%s,%s,%s,%s,'normal','chat-archive') RETURNING id",
                (src, agent, turns[0][1], turns[-1][1], len(turns)))
    sid = cur.fetchone()[0]
    for i, (role, ts, text) in enumerate(turns):
        emb = None
        if do_embed:
            try:
                emb = vec_literal(embed(text))
            except Exception:
                emb = None
        cur.execute("INSERT INTO session_turn(session_id,idx,role,ts,text,embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s::vector)", (sid, i, role, ts, text, emb))
    return "ok(%d)" % len(turns)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    d, agent = sys.argv[1], sys.argv[2]
    do_embed = "--no-embed" not in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    files = sorted(glob.glob(os.path.join(d, "*.jsonl")))
    if limit:
        files = files[:limit]
    conn = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    cur = conn.cursor()
    stats = {}
    for f in files:
        try:
            r = ingest_file(cur, f, agent, do_embed)
            conn.commit()
        except Exception as e:
            conn.rollback()
            r = "ERROR:%s" % e
        k = r.split("(")[0].split(":")[0]
        stats[k] = stats.get(k, 0) + 1
        print(r, os.path.basename(f))
    cur.close()
    conn.close()
    print("SUMMARY", stats, "of", len(files), "files")


if __name__ == "__main__":
    main()
