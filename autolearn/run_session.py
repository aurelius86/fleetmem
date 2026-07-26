#!/usr/bin/env python3
"""Live auto-learn pipeline driver (Big Step 6, F2). RUNS ON THE MINI.

One real session transcript -> the brain's proposal/auto-keep queue:

    transcript JSONL -> provenance.tag_transcript -> scrub.scrub_spans
        -> extract.extract_session (local Ollama via OllamaBackend)
        -> POST candidates to the brain governance API (mTLS + bearer token)

DRY-RUN BY DEFAULT: prints a LOCAL preview of the gate's decisions and posts nothing. The
server gate (api.py /autolearn/ingest) is the AUTHORITATIVE validator — this preview runs with
no DB context (conflict='clear', no lessons), so it can only over-state auto_keep; the server
will be equal-or-stricter.

  --post                 actually send
  --target extract       (default) POST scrubbed spans -> /autolearn/extract => the brain
                         runs the extractor + the deterministic dedup/conflict gate server-side.
  --target propose       each candidate -> /propose => always lands 'pending' (no memory write).
                         Legacy client-side extraction; superseded by server-side extract.
  --target ingest        the whole batch -> /autolearn/ingest => server gate auto-keeps trusted
                         first-party facts, escalates the rest. The live auto-learn path (G).

Secrets: the transcript is secret-scrubbed (deterministic) BEFORE it leaves the mini for qwen3.
Nothing is routed through any cloud model; qwen3 is on-LAN (the legacy host).
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # reach db/ so `autolearn` imports
from autolearn import provenance as P  # noqa: E402
from autolearn import scrub as S  # noqa: E402
from autolearn import extract as E  # noqa: E402
from autolearn import orchestrate as O  # noqa: E402

# --- brain API auth (same defaults as db/mcp/server.py) ---
# load single-source client.conf into the environment (only fills unset keys; real env wins).
_bc = os.path.expanduser(os.environ.get("BRAIN_CLIENT_CONF", "~/.fleetmem/client.conf"))
if os.path.exists(_bc):
    for _ln in open(_bc):
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), os.path.expanduser(_v.strip()))
URL = os.environ.get("BRAIN_URL", "https://127.0.0.1:8443")
CERT = os.path.expanduser(os.environ.get("BRAIN_CERT", "~/.fleetmem/pki/client.crt"))
KEY = os.path.expanduser(os.environ.get("BRAIN_KEY", "~/.fleetmem/pki/client.key"))
CA = os.path.expanduser(os.environ.get("BRAIN_CA", "~/.fleetmem/pki/ca.crt"))
TOKEN_FILE = os.path.expanduser(os.environ.get("BRAIN_TOKEN_FILE", "~/.fleetmem/agent.token"))


def _ctx():
    ctx = ssl.create_default_context(cafile=CA)
    ctx.load_cert_chain(CERT, KEY)
    return ctx


def _post(path, payload, timeout=90):
    data = json.dumps(payload).encode()
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    req = urllib.request.Request(URL + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %s: %s" % (e.code, e.read().decode()[:200])}
    except Exception as e:
        return {"error": "brain unreachable: %s" % e}


def _get(path, timeout=30):
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    req = urllib.request.Request(URL + path, method="GET",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": "brain unreachable: %s" % e}


def load_transcript(path):
    """Parse a Claude Code JSONL transcript into a list of line dicts (skips bad lines)."""
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict):
                lines.append(obj)
    return lines


def preview(proposals):
    print("\n  LOCAL PREVIEW (server gate is authoritative; this assumes no conflict/lessons):")
    for p in proposals:
        d = O.decide_one(p)
        print("    %-9s trust=%-11s %-28s %s" % (d.action, d.trust, (p.get("name") or "")[:28], d.reasons))
    print()


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--post", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--target", choices=("propose", "ingest", "extract"), default="extract",  # was 'propose' (ungated)
                    help="extract = server-side (POST spans, brain runs the extractor+gate, no ssh->the legacy host); "
                         "propose = local extract, always pending (safe); ingest = local extract, live auto-keep gate")
    ap.add_argument("--author-body", default="")   # was "manager" (a PHANTOM body — not a registered agent, so its drafts were never self-reviewed). Empty -> the brain's /autolearn/extract falls back to the authenticated cert identity; callers should still pass the real body explicitly.
    ap.add_argument("--no-known", action="store_true",
                    help="skip the recalled-memory dedup context — smaller per-batch prompt so a large "
                         "session's qwen3 extract doesn't time out; used by brain-spool-drain.sh retries.")
    args = ap.parse_args(argv)

    # fail clearly if client credentials aren't set up yet (fresh install / pre-enrollment),
    # instead of a raw FileNotFoundError deep in the first brain call.
    _missing = [p for p in (CERT, KEY, CA, TOKEN_FILE) if not os.path.exists(p)]
    if _missing:
        sys.stderr.write(
            "fleetmem: client credentials not found: %s\n"
            "Complete enrollment first (see INSTALL.md / ENROLL.md) so ~/.fleetmem/"
            "{client.conf, pki/*, <name>.token} exist, or set the BRAIN_* env vars.\n"
            % ", ".join(_missing))
        return 2

    sid = args.session_id or Path(args.transcript).stem
    lines = load_transcript(args.transcript)
    spans = S.scrub_spans(P.tag_transcript(lines))
    print("transcript=%s  lines=%d  spans=%d  session=%s" % (args.transcript, len(lines), len(spans), sid))
    if not spans:
        print("no usable spans — nothing to extract."); return 0

    # tell the extractor what this session already recalled, so it won't re-propose known facts.
    # --no-known skips this — the ~60-memory context bloats EVERY batch's prompt and can time out
    # qwen3 on a large session (brain-spool-drain.sh drops it on retry to get the session through).
    known = None
    if args.no_known:
        print("--no-known: skipping recalled-memory dedup context")
    else:
        kr = _get("/session/%s/recalled" % sid)
        known = kr.get("recalled") if isinstance(kr, dict) else None
        if known:
            print("dedup context: %d already-known memories passed to the extractor" % len(known))

    # server-side extraction — POST the scrubbed spans; the brain runs the extractor (local
    # Ollama, no ssh->the legacy host) AND the deterministic gate (trust re-derived server-side, closing).
    if args.target == "extract":
        if not args.post:
            print("dry-run (no POST). %d spans would POST to /autolearn/extract." % len(spans)); return 0
        # BATCH the spans so each /autolearn/extract request stays short. The brain runs qwen3
        # per-request; a whole-session POST held ONE of the few gunicorn workers for ~2min (see,
        # which had to raise every timeout to 900s). Small chunks finish in ~20-30s, keep the worker
        # pool (also serving /mcp over loopback) responsive, and stay well under the timeouts.
        # Same `known` dedup is passed to every batch. Minor: a fact spanning a batch boundary, or the
        # same new fact surfacing in two batches, could duplicate — dedup-on-write + the review queue
        # absorb it; a future refinement could fold each batch's extracted names back into `known`.
        BATCH = int(os.environ.get("AUTOLEARN_SPAN_BATCH", "20"))  # default was 40. Big first-pass
        # batches timed out qwen3 server-side (120s) when Ollama/the model host was queue-saturated -> the run failed,
        # spooled, and (twice) burned all 8 attempts -> parked .dropped. 20-span batches proved reliable in
        # the manual recovery (22/22 batches, 0 drops). Still overridable via env; the spool-drain
        # shrinks it further on retry (20->10->5). The checkpoint below is a span COUNT, so it stays valid.
        # checkpoint-by-span-index resume. Before this, ANY single batch error (a qwen3 timeout
        # hiccup) aborted the whole run and the spool retry re-posted ALL batches from scratch — a large
        # transcript (200+ spans / 40+ batches) could burn all 5 spool attempts without ever landing one
        # clean pass, then get parked .dropped. We now record how many spans have been successfully
        # extracted and resume from there. The checkpoint is a plain span COUNT (index), so it stays
        # valid even though the drain shrinks AUTOLEARN_SPAN_BATCH between retries (40->20->10->5). It is
        # removed on a full clean pass. All ckpt I/O is best-effort: any error just means "start at 0".
        _spooldir = os.environ.get("BRAIN_SPOOL", os.path.expanduser("~/.claude-tools/brain/spool"))
        _ckpt = os.path.join(_spooldir, "%s.extract.ckpt" % sid)
        start = 0
        try:
            if os.path.exists(_ckpt):
                start = max(0, min(len(spans), int((open(_ckpt).read().strip() or "0"))))
        except Exception:
            start = 0
        if start:
            print(" resume: %d/%d spans already extracted -> resuming at span %d" % (start, len(spans), start))
        nbatches = (len(spans) + BATCH - 1) // BATCH
        agg = {"auto_kept": 0, "dropped": 0, "escalated": 0, "skipped": 0}
        merged = {"auto_kept": [], "dropped": [], "escalated": [], "skipped": []}
        for i in range(start, len(spans), BATCH):
            chunk = spans[i:i + BATCH]
            bn = i // BATCH + 1
            # per-batch timeout is generous headroom (a 40-span chunk is ~20-30s); short requests are
            # the point. On a batch error we STOP but keep the checkpoint, so the spool retry
            # resumes at this span instead of redoing every prior batch.
            # in-batch transient retry. The observed drops were server-side qwen3/Ollama 502
            # "timed out" — often a COLD or momentarily-loaded model that succeeds once warm. Retry the
            # same batch a few times (short backoff) before failing the whole run, so one hiccup on
            # batch 1 of a big transcript no longer aborts everything.
            resp = None
            for btry in range(3):
                resp = _post("/autolearn/extract",
                             {"session_id": sid, "spans": chunk, "author_body": args.author_body, "known": known},
                             timeout=300)
                if not (isinstance(resp, dict) and resp.get("error")):
                    break
                print("  batch %d/%d try %d/3 failed: %s" % (bn, nbatches, btry + 1, resp.get("error")))
                if btry < 2:
                    time.sleep(5 * (btry + 1))
            if isinstance(resp, dict) and resp.get("error"):
                print("autolearn extract FAILED on batch %d/%d after 3 tries: %s (checkpoint at span %d — retry resumes here)"
                      % (bn, nbatches, resp["error"], i))
                # backpressure: distinguish a SATURATION failure (Ollama/qwen3 cold-load or queue-
                # saturated -> HTTP 502/503 "timed out") from a genuine one. The batch already retried 3x
                # in-line; if it STILL fails on a saturation signal, the generator is wedged, not the data.
                # Exit 4 tells brain-spool-drain.sh to retry next tick WITHOUT burning a MAX_ATTEMPTS slot,
                # so a long Ollama-busy window can't park an otherwise-fine transcript as .dropped. The
                # checkpoint is kept, so the next tick resumes forward. Any other error exits 3 (counts).
                _e = str(resp.get("error", "")).lower()
                if any(s in _e for s in ("timed out", "timeout", "502", "503", "ollama")):
                    return 4
                return 3
            counts = resp.get("counts", {}) if isinstance(resp, dict) else {}
            for k in agg:
                agg[k] += int(counts.get(k, 0) or 0)
            results = resp.get("results", {}) if isinstance(resp, dict) else {}
            for k in merged:
                v = results.get(k)
                if isinstance(v, list):
                    merged[k].extend(v)
            # advance the checkpoint after each CLEAN batch so a later failure resumes here.
            try:
                with open(_ckpt, "w") as fh:
                    fh.write(str(i + len(chunk)))
            except Exception:
                pass
            print("  batch %d/%d (%d spans) -> %s" % (bn, nbatches, len(chunk), json.dumps(counts)))
        # full clean pass — drop the checkpoint so a future re-extract of this session starts fresh.
        try:
            os.remove(_ckpt)
        except OSError:
            pass
        print("EXTRACT (%d batch(es), %d spans) ->" % (nbatches, len(spans)), json.dumps(agg, indent=2))
        return 0

    # ---- legacy CLIENT-side extraction (propose/ingest): qwen3 via the legacy host admin channel ----
    backend = E.OllamaBackend()
    proposals = E.extract_session(spans, backend, session_id=sid, author_body=args.author_body, known=known)
    print("qwen3 returned %d candidate(s)." % len(proposals))
    preview(proposals)

    if not args.post:
        print("dry-run (no POST). re-run with --post to send."); return 0
    if not proposals:
        print("nothing to post."); return 0

    if args.target == "ingest":
        resp = _post("/autolearn/ingest", {"session_id": sid, "candidates": proposals}, timeout=900)  #
        print("INGEST ->", json.dumps(resp.get("counts", resp), indent=2))
        # _post returns {"error": ...} (not raise) when the brain is unreachable/refuses, and we
        # were returning 0 anyway — so a brain outage at the POST silently lost the candidates. Signal
        # failure so the session-end worker spools this transcript for retry.
        if isinstance(resp, dict) and resp.get("error"):
            print("autolearn ingest FAILED: %s" % resp["error"])
            return 3
    else:
        sent = {"ok": 0, "err": 0}
        for p in proposals:
            r = _post("/propose", {"body": p["body"], "name": p.get("name"), "mtype": p.get("mtype"),
                                   "description": p.get("description"), "origin_channel": p.get("origin_channel"),
                                   "cited_channels": p.get("cited_channels"), "trust": p.get("trust")})
            ok = isinstance(r, dict) and r.get("ok")
            sent["ok" if ok else "err"] += 1
            print("    propose %-28s -> %s" % ((p.get("name") or "")[:28], r.get("status") or r.get("error")))
        print("PROPOSE -> sent ok=%(ok)d err=%(err)d (all land 'pending')" % sent)
        if sent["err"] and not sent["ok"]:      # nothing landed -> spool for retry
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
