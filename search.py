#!/usr/bin/env python3
"""Minimal hybrid retrieval (dense bge-m3 + Postgres FTS, RRF-fused) — OUR code,
the seed of the Big Step 3 governance API's read path. Exact scan (no HNSW yet).

CLI:
  python3 search.py "a query"                  -> prints top-k names
  python3 search.py --golden golden.json       -> runs a golden set, reports hit@k
"""
import os
import json
import sys
import time
import urllib.request
import urllib.error

import psycopg2

OLLAMA = os.environ.get("OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embed")
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
K = int(os.environ.get("RECALL_K", "5"))            # default recall depth
POOL = int(os.environ.get("RECALL_POOL", "20"))     # per-arm candidate pool before RRF fusion
RRF_K = 60                                          # RRF smoothing constant (IR literature) — kept as code, not a knob
EMBED_TIMEOUT = int(os.environ.get("EMBED_TIMEOUT", "60"))   # hot-path embed call timeout ( wanted this tunable)
# bge-m3 via Ollama 400s ("input length exceeds the context length") on long text, and Ollama
# honours NEITHER truncate:true NOR the per-request num_ctx for embeddings — the model's load-time
# context (~2048 tok) governs. Empirically ≤6000 chars is safe. So we truncate CLIENT-SIDE (also
# box-portable: independent of the embed server's config). Truncation is safe for recall — the vector
# loses the tail, but full text is still indexed by the FTS/keyword arm (tsv).
EMBED_MAX_CHARS = int(os.environ.get("EMBED_MAX_CHARS", "6000"))
# a TRANSIENT embed failure (HTTP 503 "server busy" when the embedder's queue is momentarily full,
# or a connection reset/timeout) should NOT immediately collapse recall to keyword_only — a short bounded
# backoff-retry rides out the blip. Kept small so the hot path never stalls long; 0 disables retries.
EMBED_RETRIES = int(os.environ.get("EMBED_RETRIES", "2"))
EMBED_RETRY_DELAY = float(os.environ.get("EMBED_RETRY_DELAY", "0.5"))   # base seconds; grows linearly per attempt


def embed(q):
    return _embed_call((q or "")[:EMBED_MAX_CHARS])


def _embed_call(text, _tries=0):
    data = json.dumps({"model": MODEL, "input": text, "options": {"num_ctx": 8192}, "truncate": True}).encode()
    req = urllib.request.Request(OLLAMA, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
            return json.loads(r.read())["embeddings"][0]
    except urllib.error.HTTPError as e:
        # token-dense text can still exceed ctx under the char cap — halve and retry (bounded)
        # so no valid text ever ends up a permanent NULL embedding.
        if e.code == 400 and len(text) > 1000:
            return _embed_call(text[:len(text) // 2], _tries)
        # 503 = embedder queue full (transient) — brief backoff + retry before giving up.
        if e.code == 503 and _tries < EMBED_RETRIES:
            time.sleep(EMBED_RETRY_DELAY * (_tries + 1))
            return _embed_call(text, _tries + 1)
        raise
    except (urllib.error.URLError, TimeoutError) as e:
        # connection refused/reset or read timeout — also transient; retry a couple times.
        if _tries < EMBED_RETRIES:
            time.sleep(EMBED_RETRY_DELAY * (_tries + 1))
            return _embed_call(text, _tries + 1)
        raise


def vec_literal(v):
    return "[" + ",".join(format(x, ".7g") for x in v) + "]"


def search(cur, q, k=K):
    vec = vec_literal(embed(q))
    # name IS NOT NULL: skip author-only personal notes (name=None) — they crash the name-join in
    # --golden and aren't valid golden targets. (This CLI benchmark has no access filter, unlike the
    # API's access-gated recall; it's a base-retrieval yardstick over named/trusted memories.)
    cur.execute("SELECT name FROM memory WHERE deleted_at IS NULL AND name IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s", (vec, POOL))
    dense = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT name FROM memory WHERE deleted_at IS NULL AND name IS NOT NULL "
                "AND tsv @@ websearch_to_tsquery('english', %s) "
                "ORDER BY ts_rank(tsv, websearch_to_tsquery('english', %s)) DESC LIMIT %s",
                (q, q, POOL))
    kw = [r[0] for r in cur.fetchall()]
    scores = {}
    for rank, n in enumerate(dense):
        scores[n] = scores.get(n, 0) + 1.0 / (RRF_K + rank)
    for rank, n in enumerate(kw):
        scores[n] = scores.get(n, 0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda n: -scores[n])[:k]


def main():
    conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")
    conn.set_client_encoding("UTF8")   # robust on any host locale
    cur = conn.cursor()
    if sys.argv[1] == "--golden":
        golden = json.load(open(sys.argv[2]))
        hits = 0
        for g in golden:
            top = search(cur, g["q"])
            hit = any(g["expect"] in n for n in top)
            hits += hit
            print(("HIT " if hit else "MISS") + " | %-44s | top: %s" % (g["q"][:44], ", ".join(top[:3])))
        print("\nhit@%d = %d/%d (%.0f%%)" % (K, hits, len(golden), 100.0 * hits / len(golden)))
    else:
        for n in search(cur, sys.argv[1]):
            print(n)
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
