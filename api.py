#!/usr/bin/env python3
"""Brain governance API — Wave 1 (OUR code, on 203).

The ONLY path to the brain. Auth = mTLS + bearer token (both):
  - nginx terminates mTLS and passes the verified client CN as X-SSL-Client-CN.
  - this app checks the Bearer token (sha256 -> agent row), and that the token's
    agent.cert_cn matches the nginx-verified CN. Both must agree.
Identity -> role -> a DETERMINISTIC access WHERE-clause (never the LLM).
Reads: role-filtered hybrid retrieval. Writes: never touch memory directly -> proposal queue.

Endpoints (Wave 1): GET /healthz, GET /whoami, POST /recall, POST /propose.
Run behind nginx via gunicorn; binds 127.0.0.1 only.
"""
import os
import base64
import hashlib
import json
import re
import time
import urllib.request

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, g

from search import embed, vec_literal, RRF_K, MODEL   # POOL now via cfg("RECALL_POOL")

# single source of truth for the running code's release version (mirrors CHANGELOG.md);
# exposed on /healthz so an operator can confirm what a live box is running.
FLEETMEM_VERSION = "0.1.8"
from contract import SENSITIVITY, TRUST, ORIGIN_CHANNELS, compute_content_hash
from autolearn import apply as AL_apply
from autolearn import conflict as AL_conflict
from autolearn import lessons as AL_lessons
from autolearn import orchestrate as AL_orch
from autolearn import extract as AL_extract   # server-side extraction (OllamaBackend)
from autolearn import scrub as AL_scrub       # belt-and-suspenders server-side span scrub
from autolearn import provenance as AL_prov   # audit-against-source scoring for /provenance
from ingest_transcripts import redact   # reuse the backfill redactor (ONE server-side copy)

app = Flask(__name__)
# cap the request body so an oversized POST can't buffer unbounded into worker memory before a
# handler runs (aligns with ATTACH_MAX_MB; Flask returns 413 past the cap). Env-tunable.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_BODY_BYTES", str(25 * 1024 * 1024)))
TRUST_HEADER_CN = "X-SSL-Client-CN"   # set by the local nginx after mTLS verify; trusted because only nginx reaches us on localhost


from werkzeug.exceptions import HTTPException


@app.errorhandler(Exception)
def _json_error_handler(e):
    """Always answer with JSON, never Flask's default HTML error page — an MCP/HTTP client then
    gets a machine-readable reason (e.g. the UntranslatableCharacter/SQL_ASCII cause) instead of an
    opaque 500 with a chunk of HTML. HTTPExceptions (abort(4xx), 404, 413, …) keep their status and
    description; anything else is an unhandled 500 and is logged with a full traceback."""
    if isinstance(e, HTTPException):
        return jsonify(error=e.description, status=e.code), e.code
    app.logger.exception("unhandled exception")
    return jsonify(error=str(e)), 500


DB_NAME = os.environ.get("PGDATABASE", "brain")
# when set (e.g. 'brain_app'), the API connects as this NON-OWNER role so RLS on `memory`
# bites, and db() stamps the per-request access context (app.agent/role/groups) that the policies
# read. UNSET/empty => connect as the DB owner with RLS bypassed = pre- behaviour, so a
# cutover ROLLBACK is just clearing this env var + restarting (no code revert).
DB_APP_USER = os.environ.get("BRAIN_DB_USER") or None


def _groups_literal(a):
    """The agent's reader-groups as a Postgres text[] literal, e.g. '{workers,common}'."""
    gs = ((a.get("access_scope") or {}).get("groups")) or []
    return "{" + ",".join(str(x) for x in gs) + "}"


def db():
    conn = (psycopg2.connect(dbname=DB_NAME, user=DB_APP_USER) if DB_APP_USER
            else psycopg2.connect(dbname=DB_NAME))
    conn.set_client_encoding("UTF8")   # robust on any host locale (fresh minimal installs are C/ASCII)
    # Stamp the RLS context from the authenticated agent (set by _load_ctx before_request).
    # Session-scoped set_config on a per-request connection = request-scoped; unset => policies
    # fail-closed. Skipped during authenticate()'s own pre-context lookup (g.agent still None).
    try:
        a = getattr(g, "agent", None)
    except RuntimeError:
        a = None      # no Flask request context (standalone script, e.g. golden_regression) — connect unstamped
    if DB_APP_USER and a is not None:
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.agent',%s,false), set_config('app.role',%s,false), "
                    "set_config('app.groups',%s,false), set_config('app.see_all',%s,false)",
                    (a.get("name") or "", a.get("role") or "", _groups_literal(a),
                     'true' if (a.get("access_scope") or {}).get("see_all") else 'false'))
        cur.close(); conn.commit()
    return conn


@app.before_request
def _load_ctx():
    """Resolve the caller once per request so db() can stamp the RLS context. Endpoints still call
    authenticate() themselves for their own error handling; this only populates g.agent (never aborts,
    so unauthenticated/enroll routes are unaffected)."""
    g.agent = None
    g.auth_err = ("missing bearer token", 401)   # default until the core runs below
    g._auth_done = False
    try:
        a, err = _authenticate_core()            # the ONE DB-hitting auth per request
        g.agent = a if not err else None
        g.auth_err = err
    except Exception:
        g.agent = None
        g.auth_err = ("auth unavailable", 503)
    g._auth_done = True


# ---- runtime config knobs (config table > brain.env > code default) ----
# The manager dashboard reads/writes these via GET/PATCH /config. cfg() resolves a knob at
# REQUEST time from a short in-process cache of the `config` table, so a manager's PATCH takes effect
# with NO service restart (eventually-consistent across gunicorn workers within _CFG_TTL). Only the 9
# request-resolved knobs below are live here; connection/model/module-level knobs (PGDATABASE,
# OLLAMA_*, EMBED_TIMEOUT, EXTRACT_*) stay brain.env-managed (restart-coupled) — see the inventory.
CONFIG_KNOBS = {
    "RECALL_K": (int, 5), "RECALL_POOL": (int, 20), "RELATED_CAP": (int, 3), "RELATED_EXPLICIT_MIN": (int, 1),
    "RECALL_SPAN_COMPRESS": (int, 0), "RECALL_SPAN_COUNT": (int, 3), "RECALL_SPAN_MIN_CHARS": (int, 400),   # span-level body compression on recall. 0=OFF (deploy-inert). When on, each recalled body is trimmed to its RECALL_SPAN_COUNT most query-relevant spans (first span always kept for context); bodies shorter than RECALL_SPAN_MIN_CHARS are left whole. Full body always fetchable via brain_get.
    "AUTOLEARN_VET_LINKS": (int, 1), "VET_LINK_TIMEOUT": (int, 60),   # menu-pick link vetting at ingest (0=off=blind link); per-call LLM timeout (s) — 60s absorbs a cold 21GB model load, else the first call falls back to blind
    "RERANK_TIMEOUT": (int, 3), "DEEP_BODY_CAP": (int, 2200), "DEEP_TIMEOUT": (int, 60),
    "ENROLL_APPROVALS": (int, 2), "PROVISIONAL_TTL_DAYS": (int, 14), "ATTACH_MAX_MB": (int, 20),
    "CONSOLIDATE_COSINE": (float, 0.90),   # near-dup consolidation similarity threshold (0..1)
    "AUTOLEARN_DEDUP_COSINE": (float, 0.90),   # autolearn semantic-dedup gate; skip a candidate this
    # close (cosine) to an existing trusted memory. 0..1; set >=1.0 to disable the semantic gate.
    "AUTOLEARN_NAME_DEDUP": (int, 1),   #(c): also dedup an author's OWN cross-session fragment via
    # the name/body sibling + value-guard signal (catches same-topic notes below the cosine gate). 0=off.
    "AUTOLEARN_NAME_DEDUP_FLOOR": (float, 0.6),   # min neighbour cosine before the name/body sibling check runs
    "AUTOLEARN_LINK_COSINE": (float, 0.75),
    "ENTITY_EXPAND_WEIGHT": (float, 0.9),   # recall weight for a shared-entity match (peer to relation edges)
    "ENTITY_EXPAND_HUBCAP": (int, 25),      # skip entities mentioned by > this many memories (hubs = noise)   # similarity FLOOR for cross-graph relates_to links on a
    # personal landing. Only neighbours at/above this cosine get an edge (was: ALL top-8, so weak topical
    # coincidences became junk edges recall then surfaced). 0..1; set 0 to link every neighbour (old behaviour).
    "AUTOLEARN_LINK_CAP": (int, 4),   # max cross-graph links one landed note may create per run.
    "AUTOLEARN_LLM_JUDGE": (int, 1),   # few-shot LLM worthiness judge in the gate (1=on, 0=off).
    # Eval-proven 25% splinter-catch at 0 false-drops (db/autolearn/tests/t193_worthiness_eval). Fail-open.
    "AUTOLEARN_LAND_QUARANTINED": (int, 1),   # (validate-on-recall): 1 = a quarantined non-sensitive,
    # non-conflicting OWN-session capture lands as the author's personal note (recallable, self-validated at
    # recall) instead of escalating to the human queue. 0 = legacy (only trusted auto_keep lands; rest queue).
    "AUTOLEARN_LAND_SENSITIVE": (int, 0),   # (Approval 2.0 step 1): 1 = a SENSITIVE own-session capture
    # ALSO lands as the author's personal note (the author-only tier protects it; it reaches other agents only
    # via the manager share-gate) instead of escalating to the human queue. 0 = legacy (sensitive -> human
    # queue). Requires AUTOLEARN_LAND_QUARANTINED=1. Live-tunable via /config (activation is a reversible knob flip).
    "RECALL_VALIDATE_MAX": (int, 0),   # (Approval 2.0 step 2) FAST-KILL: an untrusted personal note
    # recalled in >= this many DISTINCT sessions and still unconfirmed is soft-deleted at recall time.
    # 0 = OFF (deploy-inert). the default operator value = 2. Manager-trusted-at-birth notes (trust=trusted) are exempt.
    "RECALL_VALIDATE_TTL_DAYS": (int, 0),   # (Approval 2.0 step 2) TIME-CAP: an untrusted personal note
    # still unconfirmed this many DAYS after creation is soft-deleted by the daily validate_sweep. 0 = OFF
    # (deploy-inert). the default operator value = 14 (2 weeks). Autolearn's weak-spot notes don't linger forever.
    "VALIDATE_SWEEP_DELETE": (int, 1),   # 1 = the daily backstop sweep (validate_sweep.py) soft-deletes an
    # untrusted personal note whose source transcript was PROVABLY pruned (session-row tombstone, zero live turns).
    # Never touches never-ingested / null-source notes. 0 = census-only (report, delete nothing). Read by the CLI sweep.
    "SESSION_BRIEF_OVERLAY": (str, ""),   # global user house-rules overlay, injected RAW into
    # every agent's session brief (first-party instructions). Free text; managed via /session-overlay.
    "SESSION_BRIEF_MAX_CHARS": (int, 12000),   # hard cap on the assembled /session-brief text.
    "NEXT_TASK_A": (str, ""),   # T385b: handle a prior session flagged for agent A to run next (shown in the brief; auto-ignored once the task closes). Set via PATCH /config.
    "NEXT_TASK_B": (str, ""),   # T385b: same, for agent B.
    "EXTRACT_TIMEOUT": (int, 300),   # per-call ollama timeout (s) for /autolearn/extract; warm
    # qwen3:30b is 40s-2min/call, the old hardcoded 120 tipped fat batches to 502. Live-tunable via /config.
}
_CFG_TTL = 30.0
_cfg_cache = {"at": -1.0, "vals": {}}


def _cfg_load():
    """Refresh the config-table cache. Fail-open: on any error keep resolving via env/default so a
    config read never breaks a request."""
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT key, value FROM config")
        _cfg_cache["vals"] = {k: v for (k, v) in cur.fetchall()}
        cur.close(); conn.close()
    except Exception:
        app.logger.warning("cfg: config-table load failed; using env/default", exc_info=True)
    _cfg_cache["at"] = time.monotonic()


def cfg(key):
    """Resolve a knob: config table > brain.env > code default (cast per the CONFIG_KNOBS registry)."""
    caster, default = CONFIG_KNOBS[key]
    if time.monotonic() - _cfg_cache["at"] > _CFG_TTL:
        _cfg_load()
    raw = _cfg_cache["vals"].get(key)
    if raw is None:
        raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return caster(raw)
    except Exception:
        return default


def tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


# authenticate ONCE per request. _load_ctx (before_request) runs the DB-hitting core and caches
# (agent, err) on g; endpoints call authenticate() which returns that cached result instead of a 2nd
# token-hash + agent SELECT per call. Only the default require_cert=True path is cached (what _load_ctx
# runs); an explicit require_cert=False caller bypasses the cache and re-runs the core.
def authenticate(require_cert=True):
    if require_cert and getattr(g, "_auth_done", False):
        return (g.agent, None) if g.agent is not None else (None, g.auth_err)
    return _authenticate_core(require_cert)


# lightweight in-process per-agent rate limit for the expensive LLM/DB endpoints. Fixed 60s
# window, per gunicorn worker (a floor — a shared store would be exact but adds a dependency). Each
# tag's limit is env-tunable (RL_<TAG>); 0/negative disables it.
_RL_BUCKETS = {}
def _rate_ok(agent, tag, limit, window=60):
    now = time.monotonic()
    key = (agent or "?", tag)
    b = _RL_BUCKETS.get(key)
    if b is None or now - b[0] >= window:
        _RL_BUCKETS[key] = [now, 1]
        return True
    if b[1] >= limit:
        return False
    b[1] += 1
    return True


def _rate_limit(agent, tag, default_limit):
    """Return a (response, 429) tuple to short-circuit if over the limit, else None."""
    try:
        limit = int(os.environ.get("RL_%s" % tag.upper(), default_limit))
    except Exception:
        limit = default_limit
    if limit <= 0 or _rate_ok(agent, tag, limit):
        return None
    return jsonify(error="rate limit exceeded for %s; slow down" % tag), 429


def _internal_err(msg, code, exc=None):
    """return a GENERIC error to the client; the exception detail (DB/Ollama/file internals)
    goes to the SERVER LOG only. Use in `except Exception as e` handlers that previously returned
    `% e`/str(e). Deliberately NOT used for `except ValueError` input-validation handlers — those echo
    the caller's own bad input back and are intended user feedback, not an internal leak."""
    if exc is not None:
        app.logger.warning("%s: %s", msg, exc)
    return jsonify(error=msg), code


def _authenticate_core(require_cert=True):
    """-> (agent_row_dict, None) or (None, (msg, http_status)). The uncached worker (hits the DB)."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else None
    if not token:
        return None, ("missing bearer token", 401)
    cn = request.headers.get(TRUST_HEADER_CN)
    # the brain MCP HTTP service on THIS box forwards each caller's bearer to api.py over
    # loopback (127.0.0.1:5000), bypassing nginx — so no X-SSL-Client-CN header is present. nginx
    # ALWAYS sets that header (a CN, or "" on the enroll-exempt routes), so an ABSENT header + a
    # loopback peer uniquely identifies our trusted local proxy → authenticate by bearer alone (the
    # mTLS factor is satisfied by the hop never leaving the box). External clients still need mTLS.
    trusted_loopback = cn is None and request.remote_addr in ("127.0.0.1", "::1")
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM agent WHERE token_hash=%s AND revoked_at IS NULL", (tok_hash(token),))
    a = cur.fetchone()
    cur.close()
    conn.close()
    if not a:
        return None, ("invalid or revoked token", 401)
    if require_cert and not trusted_loopback:
        if not a.get("cert_cn"):
            return None, ("agent has no bound cert; mTLS required for this route", 403)
        if a["cert_cn"] != cn:
            return None, ("client cert CN does not match the token's agent", 403)
    return dict(a), None


def access_where(agent, soft_delete=True, temporal=True):
    """Role -> (sql_fragment, params). An agent with access_scope.see_all reads all live rows;
    everyone else is capped at their per-role sensitivity ceiling AND must share a reader-group or be
    named in readers (default-closed) — so a memory can be scoped to a subset of agents/managers.
    soft_delete/temporal toggle the deleted_at/invalid_at guards for tables that lack
    them (structure kind, e.g. task, has neither)."""
    conds = []
    if soft_delete:
        conds.append("deleted_at IS NULL")
    if temporal:
        conds.append("invalid_at IS NULL")
    base = " AND ".join(conds) if conds else "TRUE"
    scope = agent.get("access_scope") or {}
    if scope.get("see_all"):                            # see-all is an explicit capability now, not the manager role
        return base, []
    gate, gparams = _trusted_scope_gate(agent)
    return base + " AND " + gate, gparams


def _trusted_scope_gate(agent):
    """The sensitivity-ceiling + reader-group gate applied to TRUSTED shared rows: an agent is capped
    at its per-role sensitivity ceiling AND must share a reader-group / be named in readers
    (default-closed). Returns (sql_fragment, params). Single source shared by access_where and
    mem_read_where so the app layer can't diverge — and so mem_read_where can apply it to ONLY the
    trusted tier (a caller's OWN personal/ready_to_share rows bypass it, matching RLS mem_sel)./M8: the ceiling reads the SAME access_config expression RLS's mem_sel policy uses (migration
    0020); COALESCE -> 'normal' keeps an unknown role fail-closed. sens_rank + access_config are
    granted to brain_app."""
    groups = (agent.get("access_scope") or {}).get("groups", []) or []
    return ("sens_rank(sensitivity) <= sens_rank(COALESCE("
            "(SELECT max_sensitivity FROM access_config WHERE role=%s),'normal')) "
            "AND readers && %s", [agent["role"], groups])


def mem_read_where(agent):
    """access_where + the MEMORY share-tier visibility rules (supersedes the provisional
    rule). Normal recall returns:
      - TRUSTED rows (the shared brain — access_where's readers/sensitivity gate still applies, and
        only managers see all); PLUS
      - the caller's OWN PERSONAL rows (author-only, permanent; no other agent — not even a manager —
        picks them up in recall; managers inspect another agent's personal explicitly via
        /personal/inspect).
    READY_TO_SHARE rows are in the manager review queue (/provisional/pending) awaiting a managers
    approve (-> trusted) or deny (-> delete). Per the final governance rule, the REQUESTER can still RECALL its own
    ready_to_share note until it's deleted — it is no longer invisible to its author while pending
    (still invisible to everyone else; RLS mem_sel already permits the author-own read).
    Expired rows are still filtered (legacy TTL'd notes). Only the memory read paths use this."""
    scope = agent.get("access_scope") or {}
    if scope.get("personal_only"):                      # cloud/untrusted agent (e.g. a hosted LLM)
        base = ("deleted_at IS NULL AND invalid_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())")
        return base + " AND share_status IN ('personal','ready_to_share') AND author_body = %s", [agent["name"]]
    if scope.get("see_all"):                            # full-read console identity (dashboard) —
        where, params = access_where(agent)             # ALL trusted + ALL personal (any author); ready_to_share
        where += (" AND (expires_at IS NULL OR expires_at > now()) "   # + expired stay hidden. mTLS-gated console only.
                  "AND share_status IN ('trusted','personal')")
        return where, params
    # the trusted-scope gate (sensitivity ceiling + reader-group) applies to the TRUSTED tier
    # ONLY. A caller's OWN personal/ready_to_share rows are visible regardless of readers/sensitivity
    # (you own them) — OR'd OUTSIDE the gate. This matches RLS mem_sel (migration 0020) exactly; the
    # pre- code AND-ed access_where's gate over the whole clause, so a personal note (readers=[])
    # was wrongly filtered for any agent without see_all (a fresh genesis manager, or any worker).
    base = ("deleted_at IS NULL AND invalid_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())")
    gate, gparams = _trusted_scope_gate(agent)
    if agent["role"] == "manager":
        # the managers are one entity — a manager recalls ANY manager's `ready_to_share` DRAFTS
        # (so either twin reviews/uses what the other captured), while `personal` stays strictly
        # author-own (private scratch tier).
        where = (base + " AND ((share_status='trusted' AND " + gate + ") "
                 "OR (share_status='personal' AND author_body = %s) "
                 "OR (share_status='ready_to_share' AND author_body IN "
                 "(SELECT name FROM agent WHERE role='manager')))")
        return where, gparams + [agent["name"]]
    where = (base + " AND ((share_status='trusted' AND " + gate + ") "
             "OR (share_status IN ('personal','ready_to_share') AND author_body = %s))")
    return where, gparams + [agent["name"]]


def log(cur, actor, action, tkind=None, tid=None, detail=None):
    cur.execute("INSERT INTO action_log(actor,action,target_kind,target_id,detail) VALUES (%s,%s,%s,%s,%s)",
                (actor, action, tkind, tid, psycopg2.extras.Json(detail) if detail else None))


@app.get("/healthz")
def healthz():
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()")
        enc = cur.fetchone()[0]
        cur.close(); conn.close()
        if enc != "UTF8":
            app.logger.warning("database encoding is %s, not UTF8 — non-ASCII writes will fail; re-encode it (see UPGRADING.md)", enc)
        return jsonify(ok=True, version=FLEETMEM_VERSION, encoding=enc)
    except Exception as e:
        app.logger.warning("healthz db check failed: %s", e)
        return jsonify(ok=False, error="database unavailable", version=FLEETMEM_VERSION), 503


@app.get("/healthz/detail")
def healthz_detail():
    """Deeper health than /healthz: DB up, encoding, and migration state (drift/pending). Unauth
    like /healthz (reachable only through the nginx mTLS gate). 200 when healthy, 503 otherwise —
    the machine-readable companion to `doctor.py`."""
    out = {"version": FLEETMEM_VERSION, "ok": True}
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()")
        out["encoding"] = cur.fetchone()[0]
        out["db"] = "up"
        try:
            import migrate
            done = migrate.applied(cur)
            allm = migrate.discover()
            out["migrations_applied"] = len(done)
            out["migrations_pending"] = [v for v, _n, _m, _p in allm if v not in done]
            out["migrations_drift"] = [v for v, _n, _m, p in allm if v in done and done[v] != migrate.checksum(p)]
        except Exception as e:
            out["migrations_error"] = str(e)
        cur.close(); conn.close()
    except Exception as e:
        app.logger.warning("healthz/detail db check failed: %s", e)
        return jsonify(ok=False, db="down", error=str(e), version=FLEETMEM_VERSION), 503
    if out.get("encoding") != "UTF8" or out.get("migrations_pending") or out.get("migrations_drift") or out.get("migrations_error"):
        out["ok"] = False
    return jsonify(out), (200 if out["ok"] else 503)


@app.get("/whoami")
def whoami():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    return jsonify(name=a["name"], role=a["role"], cert_cn=a.get("cert_cn"),
                   groups=(a.get("access_scope") or {}).get("groups", []))


# — names whose memory body is injected RAW as instructions into every session by /bootstrap.
# The highest-value poisoning target in the brain, so writes to these names are MANAGER-ONLY at the
# API (preventive) and any content change is alarmed by the DB trigger in migration 0016 (detective).
# Keep this in sync with the `protected` array in brain_bootstrap_moc_guard() (migration 0016).
BOOTSTRAP_RULES_MOC = "always_on_rules_moc"
CORE_KNOWLEDGE_MOC = "core_knowledge_moc"   # single-source setup/state digest injected into /session-brief (RLS-filtered like the rules MOC)
BOOTSTRAP_PINNED = frozenset({BOOTSTRAP_RULES_MOC})


def active_session(cur, body):
    """the server-authoritative current chat session for a body, recorded by /bootstrap from
    the SessionStart hook (the reliable id source). Used to stamp source_session on live per-session
    writes instead of the frozen X-Brain-Session header (which the remote-MCP connection captures at
    connect and then serves stale for the connection's whole life). Returns None if unknown (caller
    then falls back to whatever was passed). Table may not exist pre-migration-0017 -> treat as None
    (SAVEPOINT so the miss can't poison the caller's open transaction)."""
    try:
        cur.execute("SAVEPOINT _active_session")
        cur.execute("SELECT session_id FROM body_active_session WHERE body=%s", [body])
        r = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT _active_session")
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT _active_session")
        except Exception:
            pass
        return None
    if not r:
        return None
    return (r["session_id"] if isinstance(r, dict) else r[0]) or None


# --- tail: server-side Ed25519 tamper-evidence for memory rows -----------------------------
# The brain signs a canonical payload on write; GET /memory/verify re-checks it. This catches a
# direct-Postgres edit that bypassed the API (the trust/provenance gate can't see that). Detective +
# fail-soft: no key on disk -> signing disabled; a NULL signature = unsigned/legacy, NOT tampered.
_SIGN_KEY_PATH = os.environ.get("BRAIN_SIGN_KEY", "/opt/brain-db/keys/brain-sign.ed25519")
_sign_cache = {}


def _sign_key():
    """Lazy-load the Ed25519 private key + its short key id. Returns (key_or_None, key_id_or_None)."""
    if _sign_cache:
        return _sign_cache.get("k"), _sign_cache.get("kid")
    k = kid = None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as _ser
        raw = open(_SIGN_KEY_PATH, "rb").read()
        k = Ed25519PrivateKey.from_private_bytes(raw)
        pub = k.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
        kid = hashlib.sha256(pub).hexdigest()[:8]
    except Exception:
        k = kid = None
    _sign_cache["k"], _sign_cache["kid"] = k, kid
    return k, kid


def _sig_payload(name, body, author_body, source_session):
    """Canonical bytes bound to identity + content (US-unit separated; body hashed for length)."""
    bh = hashlib.sha256((body or "").encode()).hexdigest()
    return ("\x1f".join([name or "", author_body or "", source_session or "", bh])).encode()


def sign_memory(name, body, author_body, source_session):
    """Return (hex_signature, key_id) for a memory, or (None, None) if signing is disabled."""
    k, kid = _sign_key()
    if not k:
        return None, None
    try:
        return k.sign(_sig_payload(name, body, author_body, source_session)).hex(), kid
    except Exception:
        return None, None


def verify_memory_row(row):
    """True = signature valid, False = TAMPERED, None = unsigned/legacy or signing disabled."""
    sig = row.get("signature")
    if not sig:
        return None
    k, _ = _sign_key()
    if not k:
        return None
    try:
        k.public_key().verify(bytes.fromhex(sig),
                              _sig_payload(row.get("name"), row.get("body"),
                                           row.get("author_body"), row.get("source_session")))
        return True
    except Exception:
        return False


@app.post("/bootstrap")
def bootstrap():
    """Session-start handshake: return this body's persona `welcome` + the always-on
    behavioural house rules to inject DETERMINISTICALLY at the top of every session (so behaviour
    never depends on the agent choosing to recall them). Rules = the body of the trusted
    `always_on_rules_moc` memory, role-filtered. welcome = the agent row's welcome. These are
    trusted first-party INSTRUCTIONS (not recalled data), so they are returned raw, not defanged.
    Fail-soft by design: a missing rules memory just returns an empty rules string."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    sid = (request.get_json(silent=True) or {}).get("session_id")
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if sid:   # record THIS body's current chat so live writes stamp the right source_session
        cur.execute("INSERT INTO body_active_session(body, session_id, updated_at) VALUES (%s,%s,now()) "
                    "ON CONFLICT (body) DO UPDATE SET session_id=EXCLUDED.session_id, updated_at=now()",
                    [a["name"], sid])
    cur.execute("SELECT welcome FROM agent WHERE name=%s", [a["name"]])
    ar = cur.fetchone()
    welcome = (ar or {}).get("welcome") or ""
    where, params = mem_read_where(a)
    cur.execute("SELECT body FROM memory WHERE name=%s AND " + where + " ORDER BY updated_at DESC LIMIT 1", [BOOTSTRAP_RULES_MOC] + params)
    rr = cur.fetchone()
    rules = (rr or {}).get("body") or ""
    overlay = cfg("SESSION_BRIEF_OVERLAY") or ""   # global user house-rules overlay (raw, first-party)
    log(cur, a["name"], "bootstrap", "agent", a["name"], {"session": sid, "rules": bool(rules)})
    # B1-18: a non-manager bootstrapping with EMPTY rules means the always-on house-rules MOC is
    # invisible to it (a-class regression — bad readers/groups). Don't be silent: alert managers,
    # throttled to once per agent per 24h so it can't spam the inbox on every session start.
    if not rules and a["role"] != "manager":
        cur.execute("SELECT 1 FROM action_log WHERE action='bootstrap_empty_rules' AND actor=%s "
                    "AND created_at > now() - interval '24 hours' LIMIT 1", [a["name"]])
        if not cur.fetchone():
            log(cur, a["name"], "bootstrap_empty_rules", "agent", a["name"], {"session": sid})
            cur.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
            for row in cur.fetchall():
                cur.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) VALUES (%s,%s,%s,%s,'alert')",
                            ("brain-guard", row["name"], "ALERT: %s bootstrapped with EMPTY rules" % a["name"],
                             "Agent %s (role=%s) received empty always-on rules from /bootstrap — the '%s' "
                             "MOC is not visible to it (check its readers/groups vs the MOC). Session %s."
                             % (a["name"], a["role"], BOOTSTRAP_RULES_MOC, sid)))
    conn.commit(); cur.close(); conn.close()
    return jsonify(welcome=welcome, rules=rules, overlay=overlay, global_overlay=overlay, session_id=sid)


@app.route("/session-overlay", methods=["GET", "POST"])
def session_overlay():
    """ session-brief overlays. Two scopes:
      - scope='global'  -> the user's house-rules overlay injected (raw) into EVERY agent's session
        brief, stored as the SESSION_BRIEF_OVERLAY config knob. Manager/approver only (read + write).
      - scope='<agent>' -> that agent's per-agent overlay (its persona/job), stored in agent.welcome.
        GET: a manager reads any, a non-manager only its own. POST: manager/approver only.
    GET ?scope=... returns {scope,text}; POST {scope,text} sets it. First-run onboarding calls this
    (via brain_session_overlay_set) after asking the user for their house rules."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    body = request.get_json(silent=True) or {}
    scope = (request.args.get("scope") or body.get("scope") or "global").strip()
    is_mgr = a["role"] in ("manager", "approver")
    conn = db(); cur = conn.cursor()
    try:
        if request.method == "GET":
            if scope == "global":
                if not is_mgr:
                    return jsonify(error="manager/approver role required"), 403
                cur.execute("SELECT value FROM config WHERE key='SESSION_BRIEF_OVERLAY'")
                r = cur.fetchone()
                return jsonify(scope="global", text=(r[0] if r else ""))
            if not is_mgr and scope != a["name"]:
                return jsonify(error="you may only read your own overlay"), 403
            cur.execute("SELECT welcome FROM agent WHERE name=%s", [scope])
            r = cur.fetchone()
            if r is None:
                return jsonify(error="unknown agent: %s" % scope), 404
            return jsonify(scope=scope, text=(r[0] or ""))
        # POST — write
        text = body.get("text")
        if not isinstance(text, str):
            return jsonify(error='body must include "text" (a string)'), 400
        if not is_mgr:
            return jsonify(error="manager/approver role required"), 403
        if scope == "global":
            cur.execute("INSERT INTO config(key,value,updated_by) VALUES ('SESSION_BRIEF_OVERLAY',%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
                        "updated_by=EXCLUDED.updated_by", [text, a["name"]])
            log(cur, a["name"], "session_overlay_set", "config", "SESSION_BRIEF_OVERLAY",
                {"scope": "global", "len": len(text)})
        else:
            cur.execute("UPDATE agent SET welcome=%s WHERE name=%s", [text, scope])
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify(error="unknown agent: %s" % scope), 404
            log(cur, a["name"], "session_overlay_set", "agent", scope, {"scope": scope, "len": len(text)})
        conn.commit()
        if scope == "global":
            _cfg_cache["at"] = -1.0            # bust cfg() cache so /bootstrap serves the new overlay at once (other workers within _CFG_TTL), same as /config
        return jsonify(ok=True, scope=scope)
    finally:
        cur.close(); conn.close()


@app.post("/session-brief")
def session_brief():
    """ — the ONE per-agent session-start brief, assembled server-side for the AUTHENTICATED
    agent only. IDENTITY-BOUND: there is no agent parameter, so no caller can fetch another agent's
    brief. Combines the static core (persona welcome + always-on rules, RLS-filtered) + the global
    user overlay + this agent's live state (unread inbox, open tasks, and — for managers — the review
    queue), then caps the whole to SESSION_BRIEF_MAX_CHARS. Returns {brief, session_id}. The shipped
    SessionStart hook prefers this route and just prints `.brief`. Fail-soft like /bootstrap."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    sid = (request.get_json(silent=True) or {}).get("session_id")
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    inbox_rows = []; task_rows = []; review = ""; core = ""
    try:
        if sid:   # record THIS body's current chat (same as /bootstrap) so live writes stamp source_session
            cur.execute("INSERT INTO body_active_session(body, session_id, updated_at) VALUES (%s,%s,now()) "
                        "ON CONFLICT (body) DO UPDATE SET session_id=EXCLUDED.session_id, updated_at=now()",
                        [a["name"], sid])
        # static core: per-agent welcome + RLS-filtered always-on rules + global overlay
        cur.execute("SELECT welcome FROM agent WHERE name=%s", [a["name"]])
        ar = cur.fetchone(); welcome = (ar or {}).get("welcome") or ""
        where, params = mem_read_where(a)
        cur.execute("SELECT body FROM memory WHERE name=%s AND " + where + " ORDER BY updated_at DESC LIMIT 1", [BOOTSTRAP_RULES_MOC] + params)
        rr = cur.fetchone(); rules = (rr or {}).get("body") or ""
        cur.execute("SELECT body FROM memory WHERE name=%s AND " + where + " ORDER BY updated_at DESC LIMIT 1", [CORE_KNOWLEDGE_MOC] + params)
        kr = cur.fetchone(); core = (kr or {}).get("body") or ""
        overlay = cfg("SESSION_BRIEF_OVERLAY") or ""
        # live state: this agent's unread inbox + open tasks (RLS-filtered) + review queue (managers)
        cur.execute("SELECT from_agent, subject, body FROM message WHERE to_agent=%s AND read_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 10", [a["name"]])
        inbox_rows = cur.fetchall()
        swhere, sparams = struct_read_where(a)
        cur.execute("SELECT handle, status, title, (SELECT slug FROM project WHERE id=task.project_id) AS project "
                    "FROM task WHERE " + swhere + " AND status IN ('open','in-progress') ORDER BY status, handle",
                    sparams)
        task_rows = cur.fetchall()
        if a["role"] in ("manager", "approver"):
            cur.execute("SELECT count(*) AS n FROM proposal WHERE status='pending' AND deleted_at IS NULL")
            nprop = (cur.fetchone() or {}).get("n") or 0
            cur.execute("SELECT count(*) AS n FROM memory WHERE share_status='ready_to_share' AND deleted_at IS NULL")
            nprov = (cur.fetchone() or {}).get("n") or 0
            if (nprop + nprov) > 0:
                review = "🧠 Awaiting your review: %d proposal(s) · %d ready-to-share." % (nprop, nprov)
        log(cur, a["name"], "session_brief", "agent", a["name"], {"session": sid})
        conn.commit()
    finally:
        cur.close(); conn.close()
    parts = ["## your brain — session brief" + ((" (%s)" % sid[:8]) if sid else "")]
    if welcome:
        parts.append(welcome)
    if rules:
        parts.append(rules)
    if core:
        parts.append(core)
    if overlay:
        parts.append(overlay)
    if inbox_rows:
        lines = ["🧠 Unread inbox (%d) — turn actionable items into tasks, then mark read:" % len(inbox_rows)]
        for m in inbox_rows:
            body1 = re.sub(r"\s+", " ", (m.get("body") or "")).strip()[:160]
            lines.append("  - [%s] %s: %s" % (m.get("from_agent") or "?", m.get("subject") or "(no subject)", body1))
        parts.append("\n".join(lines))
    if task_rows:
        # compact — count + explicit run-next + in-progress "resume" pointers (full board on request)
        open_n = sum(1 for t in task_rows if (t.get("status") or "") == "open")
        prog = [t for t in task_rows if (t.get("status") or "") == "in-progress"]
        head = "🗂️ Tasks: %d open · %d in-progress — ask for the full board when you want it." % (open_n, len(prog))
        lines = [head]
        # T385b: explicit "run next" pointer a prior session set via PATCH /config NEXT_TASK_<BODY>;
        # shown only while that task is still open/in-progress (auto-ignored once it closes).
        nxt_key = "NEXT_TASK_" + (a["name"] or "").upper()
        nxt = (cfg(nxt_key).strip() if nxt_key in CONFIG_KNOBS else "")
        nxt_task = next((t for t in task_rows if t.get("handle") == nxt), None) if nxt else None
        if nxt_task:
            proj = (" — %s" % nxt_task["project"]) if nxt_task.get("project") else ""
            lines.append("▶ Run next (you flagged this last session): [%s] %s%s" % (nxt_task.get("handle"), nxt_task.get("title") or "", proj))
        if prog:
            lines.append("▶ Resume (last session's active work):")
            for t in prog:
                proj = (" — %s" % t["project"]) if t.get("project") else ""
                lines.append("  > [%s] %s%s" % (t.get("handle"), t.get("title") or "", proj))
        parts.append("\n".join(lines))
    if review:
        parts.append(review)
    brief = "\n\n".join(parts)
    cap = cfg("SESSION_BRIEF_MAX_CHARS")
    if cap and len(brief) > cap:
        brief = brief[:cap].rstrip() + ("\n\n…[brief truncated to %d chars]" % cap)
    return jsonify(brief=brief, session_id=sid)


_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def normalize_query(raw):
    """ input front-gate: validity guard + normalize, deterministic (no LLM in the hot path).
    Strip control chars, collapse whitespace runs, bound length. REPAIR-AND-PROCEED — only return
    an error for truly unusable input (empty); over-long is truncated, not rejected.
    Returns (clean_query, error_or_None). (Fuzzy typo/abbrev repair is a later pass — needs a
    trigram index/dictionary — see the recall-two-approach design.)"""
    q = _CTRL_RE.sub(" ", raw or "")
    q = " ".join(q.split())
    if not q:
        return "", "empty query"
    if len(q) > 512:
        q = q[:512]
    return q, None


def _clip_int(raw, default, lo, hi):
    """ input guard: parse an int query/body param and clamp to [lo,hi]; fall back to `default`
    on missing/non-int input. Deterministic (no LLM), so a malformed k/limit/offset repairs-and-
    proceeds instead of 500-ing a gunicorn worker."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


# RELATED_CAP / RERANK_TIMEOUT / DEEP_TIMEOUT / DEEP_BODY_CAP are now resolved at REQUEST time via
# cfg() so the manager dashboard can retune them without a restart — see CONFIG_KNOBS above.

OLLAMA_GEN = os.environ.get("OLLAMA_GEN_URL", "http://127.0.0.1:11434/api/generate")   # local Ollama; override via OLLAMA_GEN_URL
RERANK_MODEL = os.environ.get("RERANK_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")    # local LLM only — brain stays self-contained

# the worthiness judge gets its OWN model + endpoint. It asks a 6-TOKEN KEEP/DROP question on
# a 45s budget, but the 30B needs >200s for that on this CPU-only box (Ollama GPU discovery broken -
# / missing /sys/module/amdgpu/version), so the judge was failing OPEN on essentially every
# call and the junk gate was effectively dead (audit: ~39% junk landing). A small fast model answers
# in seconds; measured on qwen3:4b-instruct-2507 with the exact live _JUDGE_SYS: 40% junk-catch at
# ZERO false-drops, beating the 25% design spec. Defaults fall back to the rerank model/endpoint so
# behaviour is UNCHANGED when unset (rerank + deep-search keep using RERANK_MODEL / OLLAMA_GEN).
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", RERANK_MODEL)
JUDGE_OLLAMA_GEN = os.environ.get("JUDGE_OLLAMA_GEN_URL", OLLAMA_GEN)


def llm_rerank(q, items, timeout=None):
    """Brain-native rerank via the LOCAL Ollama LLM. RESERVED for the optional explicit deep-dive
    path — NOT used in per-turn recall: the golden set showed ranking added cost + a
    regression at this brain size, so the operator set recall to 'search + find related, NO ranking'.
    items = [(name, description), ...]. Returns names reordered best-first.
    On ANY failure/timeout/parse-miss it returns the input order UNCHANGED — rerank must never
    break or stall recall. ~0.5s warm; the timeout guards the cold model-load case."""
    if timeout is None:
        timeout = cfg("RERANK_TIMEOUT")                    # live-tunable
    names = [n for n, _ in items]
    if len(names) <= 1:
        return names
    lines = "\n".join("%d. %s — %s" % (i + 1, n, (d or "")[:160]) for i, (n, d) in enumerate(items))
    prompt = ("Rank the numbered items by relevance to the query. Reply with ONLY the item numbers, "
              "most relevant first, comma-separated (e.g. 3,1,2). No words.\n\n"
              "Query: %s\n\nItems:\n%s" % (q, lines))
    try:
        data = json.dumps({"model": RERANK_MODEL, "prompt": prompt, "stream": False,
                           "options": {"num_predict": 64, "temperature": 0}}).encode()
        req = urllib.request.Request(OLLAMA_GEN, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read()).get("response", "")
        order = []
        for tok in re.findall(r'\d+', resp):
            idx = int(tok) - 1
            if 0 <= idx < len(names) and idx not in order:
                order.append(idx)
        if not order:
            return names
        for i in range(len(names)):        # append anything the LLM omitted, in original order
            if i not in order:
                order.append(i)
        return [names[i] for i in order]
    except Exception:
        return names


DEEP_MODEL = os.environ.get("DEEP_MODEL", RERANK_MODEL)     # same local LLM; brain stays self-contained


def deep_synthesize(q, sources, timeout=None):
    """Server-side LLM synthesis over the recalled pool ( deep-dive). sources = the recall_core
    output rows [{name, description, body, ...}]. Asks the LOCAL Ollama LLM to answer the query using
    ONLY those sources and cite the memory names it used. Returns the answer string, or None on any
    failure/timeout (the caller then falls back to the raw sourced pool — deep-search never 500s on a
    slow/absent model). Off the per-turn hot path, so the longer timeout is acceptable."""
    if not sources:
        return None
    if timeout is None:
        timeout = cfg("DEEP_TIMEOUT")                      # live-tunable
    body_cap = cfg("DEEP_BODY_CAP")                        # live-tunable
    blocks = "\n\n".join(
        "[%d] %s — %s\n%s" % (i + 1, s.get("name"), (s.get("description") or ""),
                              (s.get("body") or "")[:body_cap])
        for i, s in enumerate(sources))
    prompt = (
        "You are the brain's deep-search synthesiser. Answer the QUESTION using ONLY the numbered "
        "SOURCES below (each is one memory from the user's own brain). Write a concise, direct answer "
        "and CITE the sources you draw on by their name in [brackets]. If the sources do not cover the "
        "question, say so plainly and name what seems missing. Never invent facts that aren't in the "
        "sources.\n\nQUESTION: %s\n\nSOURCES:\n%s" % (q, blocks))
    try:
        data = json.dumps({"model": DEEP_MODEL, "prompt": prompt, "stream": False,
                           "options": {"num_predict": 1024, "temperature": 0}}).encode()
        req = urllib.request.Request(OLLAMA_GEN, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read()).get("response", "")
        if "</think>" in resp:                          # defensive: instruct-2507 is non-thinking, but strip if present
            resp = resp.split("</think>")[-1]
        return resp.strip() or None
    except Exception:
        return None


def _ip_entity_pat():
    """optional site/fleet IP-subnet entity pattern, config-driven so NO subnet literal lives in
    the code (public-clean, per the ship boundary). ENTITY_IP_PREFIXES = comma-separated dotted IP
    prefixes (e.g. '10.0.0.'); each becomes a `<prefix>NNN` entity so recall + self-indexing treat
    host IPs as entities. Unset (the public default) => no IP entity pattern. brain.env-managed
    (restart-coupled), like the other deployment-fixed values."""
    prefixes = [p.strip() for p in os.environ.get("ENTITY_IP_PREFIXES", "").split(",") if p.strip()]
    if not prefixes:
        return None
    alt = "|".join(re.escape(p) for p in prefixes)
    return (re.compile(r"\b((?:%s)\d{1,3})\b" % alt), lambda m: m.group(1))


_ENTITY_Q_PATS = [
    (re.compile(r"\bT(\d{1,4})\b"), lambda m: "t"+m.group(1)),
    (re.compile(r"\bLXC[ ]?(\d{2,3})\b", re.I), lambda m: "lxc"+m.group(1)),
    (re.compile(r"\bPC([12])\b"), lambda m: "pc"+m.group(1)),
    (re.compile(r"\b([a-z_]+\.py)\b"), lambda m: m.group(1).lower()),
]
_ip_pat = _ip_entity_pat()
if _ip_pat:
    _ENTITY_Q_PATS.append(_ip_pat)


def _query_entities(q):
    """cheap deterministic entities in a query string (task-ids/hosts/IPs/py-files), no DB."""
    out = set()
    for rx, fn in _ENTITY_Q_PATS:
        for m in rx.finditer(q or ""):
            out.add(fn(m))
    return out


def _index_memory_entities(cur, mem_id, text):
    """ self-maintain: index a freshly-written memory's deterministic entities into the
    memory_entity junction so recall's entity-expansion sees new notes immediately (agents AND the
    local-LLM autolearn writes). Pattern entities only (cheap, no vocab load); the batch re-populate
    fills curated-registry entities. Fail-soft: never breaks the write."""
    try:
        cur.execute("SAVEPOINT _ment")
        ents = _query_entities(text or "")
        for en in ents:
            cur.execute("INSERT INTO memory_entity(memory_id, entity_name, kind, source, mentions) "
                        "VALUES (%s,%s,'ref','write',1) ON CONFLICT (memory_id, entity_name) DO NOTHING",
                        (mem_id, en))
        cur.execute("RELEASE SAVEPOINT _ment")
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT _ment")
        except Exception:
            pass


# menu-pick link vetting — turn a new note's blind similarity neighbours into an LLM-VETTED
# subset (freehand [[title]] fails 0/5; picking from a menu works — bench. Used by
# the autolearn ingest path; NEVER raises (returns None on any failure so the caller falls back to
# the blind autolearn-v2-link). Picks are written as created_by='autolearn-ref'.
_VET_TYPES = ("relates_to", "depends_on", "supersedes", "conflicts_with")
_VET_SYS = (
    "You connect a NEW memory to EXISTING memories. Given the new memory and a numbered MENU of "
    "candidates retrieved by similarity, choose ONLY the ones it is genuinely related to — pick by "
    "EXACT name from the menu, never invent a name, and be STRICT (similarity is not relatedness; "
    "reject coincidental topic overlap). For each pick choose a rel_type from: "
    + ", ".join(_VET_TYPES) + ". Output strict JSON only: "
    '{"links":[{"target":"<exact menu name>","rel_type":"..."}]}. If none, {"links":[]}.'
)


def _vet_neighbor_links(cur, c, neighbours, timeout):
    """Return [(neighbour_id, proposed_type_or_None), ...] for the LLM-picked subset, or None if the
    LLM is unavailable/errors (caller then falls back to the blind link). Never raises."""
    try:
        ids = [nb["id"] for nb in neighbours]
        cur.execute("SELECT id, description FROM memory WHERE id = ANY(%s::uuid[])", [ids])
        desc = {str(r[0]): (r[1] or "") for r in cur.fetchall()}
        lines = ["NEW memory:", "name: %s" % (c.get("name") or ""),
                 "description: %s" % (c.get("description") or ""),
                 "body: %s" % ((c.get("body") or "")[:600]), "", "MENU (pick by exact name):"]
        by_name = {}
        for i, nb in enumerate(neighbours):
            by_name[nb["name"]] = nb["id"]
            lines.append("[%d] %s — %s" % (i, nb["name"], desc.get(str(nb["id"]), "")[:120]))
        payload = {"model": os.environ.get("GEN_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M"),
                   "system": _VET_SYS, "prompt": "\n".join(lines), "think": False, "stream": False,
                   "format": "json", "keep_alive": -1, "options": {"temperature": 0, "num_ctx": 8192}}   # keep_alive:-1 keeps qwen3 resident so later ingests don't cold-load
        req = urllib.request.Request(OLLAMA_GEN, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read()).get("response", "")
        if "</think>" in resp:
            resp = resp.split("</think>", 1)[-1]
        out = []
        for l in (json.loads(resp).get("links") or []):
            nid = by_name.get(l.get("target"))
            if nid is None:
                continue
            rt = l.get("rel_type", "relates_to")
            out.append((nid, rt if (rt in _VET_TYPES and rt != "relates_to") else None))
        return out
    except Exception:
        return None


# span-level recall compression — cheap LEXICAL span scoring (no embed calls, safe on the
# per-turn path). Splits a body into sentence/line spans, keeps the RECALL_SPAN_COUNT most
# query-relevant (by non-stopword term overlap), ALWAYS keeps the first span for context, and
# preserves original order. Off by default (RECALL_SPAN_COMPRESS=0); full body stays fetchable via
# brain_get. Serves cache-law (thinner injected context) without changing WHICH notes are recalled.
_SPAN_SPLIT = re.compile(r'(?<=[.!?])\s+|\n+')
_SPAN_WORD = re.compile(r'[a-z0-9]+')
_SPAN_STOP = frozenset(
    "the a an of to in on for and or is are was were be been do does did how what where when "
    "which who why that this these those it its as at by with from into we you i our your my me "
    "should must can could would will not no never can't dont don't".split())


def _compress_spans(q, body, n, min_chars):
    """Return (body_or_compressed, was_compressed). Keeps the n most query-relevant spans of `body`
    (plus span 0), in original order. Bodies <= min_chars, or with <= n spans, are returned unchanged.
    Falls back to unchanged if compression wouldn't actually shrink it."""
    if not body or len(body) <= min_chars:
        return body, False
    spans = [s.strip() for s in _SPAN_SPLIT.split(body) if s.strip()]
    if len(spans) <= n:
        return body, False
    qterms = set(_SPAN_WORD.findall(q.lower())) - _SPAN_STOP
    keep = {0}                                  # always keep the first span (topic/context)
    if qterms:
        scored = [(sum(1 for w in _SPAN_WORD.findall(s.lower()) if w in qterms), i)
                  for i, s in enumerate(spans)]
        for _overlap, i in sorted(scored, key=lambda x: (-x[0], x[1])):
            if len(keep) >= n:
                break
            keep.add(i)
    else:                                       # no usable query terms -> lead spans
        keep = set(range(min(n, len(spans))))
    compressed = " ".join(spans[i] for i in sorted(keep))
    if len(compressed) >= len(body):
        return body, False
    return compressed, True


def recall_core(cur, a, q, k, session_id, tags=None, do_rank=False):   # do_rank (NOT `rank`): the RRF loops below reuse `rank` as an enumerate counter
    """The retrieval pipeline (input already normalized), factored out of the route so it is
    testable in-process (the golden harness calls this directly — no HTTP, no auth) and
    reusable (e.g. a future deep-dive path). PURE retrieval: no side effects — the caller does
    log_session_recall + the action log + commit. Returns (recall_mode, out, recalled_ids).
    `tags` (list) optionally restricts to memories carrying ANY of those tags; appended to
    the shared `where` so every retrieval arm (dense/keyword/fuzzy/related) honours it."""
    where, params = mem_read_where(a)
    if tags:
        where += " AND tags && %s::text[]"
        params = params + [list(tags)]
    pool = cfg("RECALL_POOL")                              # live-tunable candidate pool per arm

    # dense (degraded mode: embedder down -> keyword_only, stated loudly)
    recall_mode = "hybrid"
    dense = []
    try:
        cur.execute("SAVEPOINT _dense")                    # a dense DB error (e.g. dim mismatch) must
        vec = vec_literal(embed(q))                        # not abort the txn and take the keyword arm down with it
        cur.execute("SELECT name FROM memory WHERE " + where + " AND name IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s",
                    params + [vec, pool])
        dense = [r[0] for r in cur.fetchall()]
        cur.execute("RELEASE SAVEPOINT _dense")
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT _dense")
        except Exception:
            pass
        recall_mode = "keyword_only"

    cur.execute("SELECT name FROM memory WHERE " + where +
                " AND name IS NOT NULL AND tsv @@ websearch_to_tsquery('english', %s) "
                "ORDER BY ts_rank(tsv, websearch_to_tsquery('english', %s)) DESC LIMIT %s",
                params + [q, q, pool])
    kw = [r[0] for r in cur.fetchall()]

    # typo/OOV fallback — the FTS arm is exact-token, so a misspelling ('acme-routr') or an
    # out-of-vocabulary word matches nothing. Only THEN, try a pg_trgm word-similarity pass so the
    # keyword arm still contributes (fuses with dense below). word_similarity finds the best-matching
    # substring extent, so 'acme-routr' scores high against a body containing 'acme-router'.
    if not kw:
        cur.execute("SELECT name FROM memory WHERE " + where +
                    " AND name IS NOT NULL AND (word_similarity(%s, description) > 0.4 "
                    "OR word_similarity(%s, coalesce(name,'')) > 0.4) "
                    "ORDER BY GREATEST(word_similarity(%s, description), "
                    "word_similarity(%s, coalesce(name,''))) DESC LIMIT %s",
                    params + [q, q, q, q, pool])
        kw = [r[0] for r in cur.fetchall()]
        if kw:
            recall_mode += "+fuzzy"

    scores = {}
    for rank, n in enumerate(dense):
        scores[n] = scores.get(n, 0) + 1.0 / (RRF_K + rank)
    for rank, n in enumerate(kw):
        scores[n] = scores.get(n, 0) + 1.0 / (RRF_K + rank)
    top = sorted(scores, key=lambda n: -scores[n])[:k]
    if do_rank and len(top) > 1:                       # OPT-IN on-demand rerank of the content hits.
        # OFF the per-turn default on purpose — ranking regresses recall at scale (measured for this corpus); this is for hard/ambiguous queries where
        # the caller asks for it. Uses the local LLM (llm_rerank), which fail-safes to original order on any
        # error/timeout. Off the hot path, so the slower model is acceptable; runs on the gen instance (:11434),
        # isolated from the embedder (:11435).
        cur.execute("SELECT name, description FROM memory WHERE name = ANY(%s)", [top])
        _rdesc = {r[0]: r[1] for r in cur.fetchall()}
        top = llm_rerank(q, [(n, _rdesc.get(n, "")) for n in top])
        recall_mode += "+ranked"

    # (revised per the operator: "search + find related, NO ranking"). Expansion is now
    # purely ADDITIVE: the content hits keep their search (RRF) order and are NEVER reordered or
    # displaced — the golden set showed displacement dropped a correct hit. We simply APPEND up
    # to RELATED_CAP related memories, and there is NO LLM rerank. "Related" = 1-hop neighbours of
    # the content hits (usage co-recall COUNT + explicit-ref edges). SEVERAL-BRAINS PRIVACY: each
    # neighbour is re-checked through mem_read_where (the both-endpoints rule) so a personal note
    # never leaks across brains. (Teaser 'there is more, ask' affordance = a later pass.)
    related = []
    rel_info = {}   # name -> {type,direction,other} = HOW a related note connects to a content hit
    if top:
        cur.execute("SELECT id, name FROM memory WHERE name = ANY(%s) AND deleted_at IS NULL", [top])
        seed_rows = cur.fetchall()
        seed_ids  = [r[0] for r in seed_rows]
        seed_name = {r[0]: r[1] for r in seed_rows}
        if seed_ids:
            w = {}
            co_sql = ("SELECT sr2.memory_id, COUNT(DISTINCT sr1.session_id) "
                      "FROM session_recall sr1 JOIN session_recall sr2 ON sr1.session_id=sr2.session_id "
                      "WHERE sr1.memory_id = ANY(%s::uuid[]) AND sr2.memory_id <> ALL(%s::uuid[]) ")
            co_params = [seed_ids, seed_ids]
            if session_id:                          # exclude THIS session -> cross-session co-use only (no intra-session feedback)
                co_sql += "AND sr1.session_id <> %s "
                co_params.append(session_id)
            co_sql += "GROUP BY sr2.memory_id"
            cur.execute(co_sql, co_params)
            for nid, c in cur.fetchall():
                w[nid] = w.get(nid, 0.0) + float(c)
            # traverse ALL typed edges (not just relates_to); remember the strongest-signal
            # edge type + direction per neighbour so recall tells the agent HOW notes connect.
            edge_rank = {"supersedes":4,"conflicts_with":4,"depends_on":3,"runs_on":3,
                         "accessed_via":3,"uses":2,"relates_to":1}
            cand_rel = {}   # nid -> (rank, {type,direction,other})
            explicit_nids = set()   # neighbours reached by an AUTHOR-written [[ref]] edge (created_by='explicit-ref')
            cur.execute("SELECT src_id, dst_id, rel_type, created_by FROM memory_relation "
                        "WHERE src_id = ANY(%s::uuid[]) OR dst_id = ANY(%s::uuid[])", [seed_ids, seed_ids])
            sset = set(seed_ids)
            for s, d, rt, cb in cur.fetchall():
                if s in sset and d in sset:
                    continue                       # both endpoints are already content hits
                seed_id = s if s in sset else d
                nid     = d if s in sset else s
                if nid in sset:
                    continue
                direction = "from" if nid == s else "to"   # from the NEIGHBOUR's perspective
                rk = edge_rank.get(rt, 1)
                # correctness edges (supersedes/conflicts_with rk=4) must SURFACE, not be drowned by
                # co-recall counts; weight rises with edge importance. relates_to keeps its old 0.5.
                w[nid] = w.get(nid, 0.0) + (0.5 if rt == "relates_to" else 0.5 + 0.4 * rk)
                if cb == "explicit-ref":               # author-written link -> eligible for a reserved slot
                    explicit_nids.add(nid)
                if nid not in cand_rel or rk > cand_rel[nid][0]:
                    cand_rel[nid] = (rk, {"type": rt, "direction": direction,
                                          "other": seed_name.get(seed_id)})
            # dedicated ENTITY-EXPANSION — a peer signal to the relation graph. Pull memories
            # that share a RARE entity (task-id/host/service) with the QUERY or a content hit,
            # weighted by rarity (hub entities skipped). Reads memory_entity; the final visibility
            # filter below re-applies `where` (RLS), so no personal note leaks across brains.
            try:
                cur.execute("SAVEPOINT _entx")
                cur.execute("SELECT DISTINCT entity_name FROM memory_entity WHERE memory_id = ANY(%s::uuid[])", [seed_ids])
                want = _query_entities(q) | set(r[0] for r in cur.fetchall())
                if want:
                    hubcap = int(cfg("ENTITY_EXPAND_HUBCAP")); base_w = float(cfg("ENTITY_EXPAND_WEIGHT"))
                    cur.execute("SELECT entity_name, count(*) FROM memory_entity WHERE entity_name = ANY(%s) GROUP BY entity_name", [list(want)])
                    freq = {r[0]: r[1] for r in cur.fetchall()}
                    rare = [e for e in want if 2 <= freq.get(e, 0) <= hubcap]
                    if rare:
                        import math
                        cur.execute("SELECT me.memory_id, me.entity_name FROM memory_entity me "
                                    "WHERE me.entity_name = ANY(%s) AND me.memory_id <> ALL(%s::uuid[])", [rare, seed_ids])
                        for mid, en in cur.fetchall():
                            w[mid] = w.get(mid, 0.0) + base_w * (2.0 / math.log2(freq.get(en, 2) + 2))
                            cand_rel.setdefault(mid, (1, {"type": "shares_entity", "direction": "entity", "other": en}))
                cur.execute("RELEASE SAVEPOINT _entx")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT _entx")
            if w:
                cand = list(w.keys())
                cur.execute("SELECT id, name FROM memory WHERE id = ANY(%s::uuid[]) AND name IS NOT NULL AND " + where,
                            [cand] + params)
                id_name = {r[0]: r[1] for r in cur.fetchall()}
                ranked  = sorted(([id_name[i], w[i], i] for i in id_name), key=lambda x: -x[1])
                seen = set(top)
                related_cap = cfg("RELATED_CAP")           # live-tunable
                explicit_min = cfg("RELATED_EXPLICIT_MIN") # reserve >=N related slots for AUTHOR [[ref]] edges
                def _take(name, i):
                    related.append(name); seen.add(name)
                    if i in cand_rel:
                        rel_info[name] = cand_rel[i][1]
                # pass 1: reserve up to explicit_min slots for author-written (created_by='explicit-ref')
                # neighbours, highest-weight first — so a hand-authored link (e.g. a low-weight relates_to)
                # can't be crowded out of recall by high-co-use hub notes. explicit_min=0 => old behaviour.
                reserved = 0
                for name, _wt, i in ranked:
                    if reserved >= explicit_min or len(related) >= related_cap:
                        break
                    if name not in seen and i in explicit_nids:
                        _take(name, i); reserved += 1
                # pass 2: fill remaining slots by weight (co-use + edges + entity), search order preserved
                for name, _wt, i in ranked:
                    if len(related) >= related_cap:
                        break
                    if name not in seen:
                        _take(name, i)
        if related:
            recall_mode += "+related"

    # content hits in search order, THEN the related ones appended (never interleaved/reordered)
    final = top + related
    related_set = set(related)
    out = []
    recalled_ids = []
    if final:
        cur.execute("SELECT id, name, description, body, tags, share_status, source_session, trust, author_body FROM memory WHERE name = ANY(%s) AND deleted_at IS NULL", [final])
        rows = {r[1]: {"id": r[0], "name": r[1], "description": r[2], "body": r[3], "tags": r[4], "share": r[5], "src": r[6], "trust": r[7], "author": r[8]} for r in cur.fetchall()}
        # "trusted" = rely-able. A note is rely-able if it's SHARED-trusted OR the author has
        # self-validated it (trust='trusted' on their own personal note). Only a still-unvalidated note
        # (personal + quarantined) carries its id + source_session so the agent can validate it against
        # that transcript and self-trust (brain_validate_memory) or delete it before relying on it.
        out = [{"name": rows[n]["name"], "description": rows[n]["description"], "body": rows[n]["body"],
                "trusted": rows[n]["share"] == "trusted" or rows[n]["trust"] == "trusted",
                **({"id": str(rows[n]["id"]), "source_session": rows[n]["src"], "author": rows[n]["author"]}
                   if (rows[n]["share"] != "trusted" and rows[n]["trust"] == "quarantined") else {}),
                **({"tags": rows[n]["tags"]} if rows[n].get("tags") else {}),   #
                **({"via": "related"} if n in related_set else {}),
                **({"relation": rel_info[n]} if n in rel_info else {})}
               for n in final if n in rows]
        recalled_ids = [rows[n]["id"] for n in final if n in rows]
        if cfg("RECALL_SPAN_COMPRESS"):                     # trim each body to its query-relevant spans
            _sn, _smc = cfg("RECALL_SPAN_COUNT"), cfg("RECALL_SPAN_MIN_CHARS")
            for item in out:
                _cb, _did = _compress_spans(q, item.get("body") or "", _sn, _smc)
                if _did:
                    item["body"] = _cb
                    item["body_compressed"] = True         # signals brain_get returns the full body
        # surface attachments on each recalled memory (metadata only — the agent fetches a
        # specific blob via brain_attachment_get). No re-gating needed: recalled_ids are already
        # visibility-filtered (mem_read_where above), so their attachments are reachable too.
        if recalled_ids:
            cur.execute("SELECT anchor_memory_id, id, kind, filename, content_type, byte_size, caption "
                        "FROM memory_attachment WHERE anchor_memory_id = ANY(%s::uuid[]) "
                        "AND deleted_at IS NULL ORDER BY created_at", [recalled_ids])
            amap = {}
            for r in cur.fetchall():
                amap.setdefault(str(r[0]), []).append(
                    {"id": str(r[1]), "kind": r[2], "filename": r[3], "content_type": r[4],
                     "byte_size": r[5], "caption": r[6]})
            if amap:
                for item in out:
                    atts = amap.get(str(rows[item["name"]]["id"]))
                    if atts:
                        item["attachments"] = atts
    return recall_mode, out, recalled_ids


def _recall_fastkill(cur, a, recalled_ids):
    """ (Approval 2.0 step 2) FAST-KILL: an untrusted personal note of the CALLER'S OWN that has now
    been recalled in >= RECALL_VALIDATE_MAX distinct sessions and is still unconfirmed (trust='quarantined')
    is soft-deleted — the agent got it this last time; it won't return unvalidated again. Manager-trusted-at-
    birth notes (trust='trusted') are exempt. 0 = OFF (deploy-inert). Audited + reversible. Call AFTER
    log_session_recall so the distinct-session count includes THIS session."""
    thr = int(cfg("RECALL_VALIDATE_MAX"))
    if thr <= 0 or not recalled_ids:
        return
    cur.execute(
        "SELECT m.id, m.name FROM memory m "
        "WHERE m.id = ANY(%s::uuid[]) AND m.author_body = %s AND m.share_status='personal' "
        "AND m.trust='quarantined' AND m.deleted_at IS NULL "
        "AND (SELECT count(*) FROM session_recall sr WHERE sr.memory_id = m.id) >= %s",
        [[str(x) for x in recalled_ids], a["name"], thr])
    for mid, nm in cur.fetchall():
        cur.execute("UPDATE memory SET deleted_at=now(), invalid_at=now(), updated_at=now() WHERE id=%s", [mid])
        log(cur, a["name"], "memory_recall_unvalidated_delete", "memory", str(mid),
            {"name": nm, "reason": "recalled in >=%d sessions still untrusted ( fast-kill)" % thr})


@app.post("/recall")
def recall():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    rl = _rate_limit(a["name"], "recall", 60)      # embed on every call
    if rl:
        return rl
    body = request.get_json(silent=True) or {}
    q, gate_err = normalize_query(body.get("q"))          # check/repair input before searching
    if gate_err:
        return jsonify(error=gate_err), 400
    k = _clip_int(body.get("k"), cfg("RECALL_K"), 1, 50)  # clamp / live-tunable default
    conn = db(); cur = conn.cursor()
    # prefer the server-authoritative active session over the client-passed id, which is
    # stale for remote-MCP (frozen X-Brain-Session per connection); fall back to the passed id.
    session_id = active_session(cur, a["name"]) or body.get("session_id")  #/
    recall_mode, out, recalled_ids = recall_core(cur, a, q, k, session_id, body.get("tags"),
                                                 do_rank=bool(body.get("rank")))  # tags / opt-in rerank
    # log what THIS session recalled so a later new memory can link to it (usage-based graph)
    AL_apply.log_session_recall(cur, session_id, recalled_ids)
    _recall_fastkill(cur, a, recalled_ids)   # retire an own untrusted note recalled >= threshold sessions unvalidated
    log(cur, a["name"], "recall", "memory", None, {"q": q, "mode": recall_mode, "n": len(out)})
    # keyword_only is ONLY reached when the dense arm threw (embedder unavailable after retries) — it is a
    # silent SEMANTIC-ARM OUTAGE, not a query property. Surface it loudly (distinct audit action the dashboard/
    # a monitor can alert on) and tell the caller, so a fleet-wide recall degradation can never hide again.
    degraded = recall_mode.startswith("keyword_only")
    if degraded:
        log(cur, a["name"], "recall_degraded", "memory", None,
            {"q": q, "reason": "embedder_unavailable", "mode": recall_mode})
    conn.commit(); cur.close(); conn.close()
    return jsonify(recall_mode=recall_mode, results=out,
                   **({"degraded": "embedder_unavailable"} if degraded else {}))


@app.post("/docs/recall")
def docs_recall():
    """ — in-house library-docs retrieval (our self-hosted Context7). Dense pgvector search over the
    `refdoc` corpus (PUBLIC library docs; bge-m3, same vector space as memory) — a SEPARATE table, so it
    never touches canonical `memory`/recall. Any authenticated agent may read (docs are non-sensitive).
    Body: {q, library?, k?}. Returns {results:[{library,version,source_url,title,chunk_idx,body}]}."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    body = request.get_json(silent=True) or {}
    q, gate_err = normalize_query(body.get("q"))
    if gate_err:
        return jsonify(error=gate_err), 400
    k = _clip_int(body.get("k"), 5, 1, 25)
    lib = (body.get("library") or "").strip()
    conn = db(); cur = conn.cursor()
    try:
        vec = vec_literal(embed(q))                        # same bge-m3 pipeline as memory recall
    except Exception:
        cur.close(); conn.close()
        return jsonify(error="docs embedder unavailable"), 503
    where = "TRUE"; params = []
    if lib:
        where += " AND library = %s"; params.append(lib)
    cur.execute("SELECT library, version, source_url, title, chunk_idx, body FROM refdoc "
                "WHERE " + where + " ORDER BY embedding <=> %s::vector LIMIT %s", params + [vec, k])
    results = [{"library": r[0], "version": r[1], "source_url": r[2], "title": r[3],
                "chunk_idx": r[4], "body": r[5]} for r in cur.fetchall()]
    log(cur, a["name"], "docs_recall", "refdoc", None, {"q": q, "library": lib or None, "n": len(results)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(results=results)


@app.post("/skill/recall")
def skill_recall():
    """ — 'which skill fits this situation' lookup over the fleetmem skill corpus (on-demand methodology
    skills, e.g. Superpowers). Semantic search on name+description; returns LIGHT rows (no bodies). Body:
    {q, k?}. Returns {results:[{name,source,description}]}. Fetch a full skill with /skill/get."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    body = request.get_json(silent=True) or {}
    q, gate_err = normalize_query(body.get("q"))
    if gate_err:
        return jsonify(error=gate_err), 400
    k = _clip_int(body.get("k"), 5, 1, 25)
    conn = db(); cur = conn.cursor()
    try:
        vec = vec_literal(embed(q))
    except Exception:
        cur.close(); conn.close()
        return jsonify(error="skill embedder unavailable"), 503
    cur.execute("SELECT name, source, description FROM skill ORDER BY embedding <=> %s::vector LIMIT %s",
                [vec, k])
    results = [{"name": r[0], "source": r[1], "description": r[2]} for r in cur.fetchall()]
    log(cur, a["name"], "skill_recall", "skill", None, {"q": q, "n": len(results)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(results=results)


@app.post("/skill/get")
def skill_get():
    """ — load ONE skill's FULL body by name (the load-on-demand half of the fleetmem skill corpus).
    Body: {name}. Returns {name,source,description,body} or 404."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="'name' required"), 400
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT name, source, description, body FROM skill WHERE name = %s", [name])
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify(error="no such skill: %s" % name), 404
    log(cur, a["name"], "skill_get", "skill", None, {"name": name})
    conn.commit(); cur.close(); conn.close()
    return jsonify(name=row[0], source=row[1], description=row[2], body=row[3])


@app.get("/skill/list")
def skill_list():
    """ — list ALL skills in the corpus as LIGHT rows (no bodies) for the dashboard Skills browser.
    Ordered by source then name. Fetch a full body with /skill/get. Any authenticated agent may read."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT name, source, description FROM skill ORDER BY source, name")
    results = [{"name": r[0], "source": r[1], "description": r[2]} for r in cur.fetchall()]
    log(cur, a["name"], "skill_list", "skill", None, {"n": len(results)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(results=results)


@app.post("/deep-search")
def deep_search():
    """ — the EXPLICIT deep-dive path (NOT the per-turn recall hook). For hard 'dig up everything
    on X' questions: run a WIDER retrieval (reuses recall_core, so all access control + related
    expansion apply) then have the local LLM SYNTHESISE a concise, sourced answer over the pool, so
    the caller gets the distilled result instead of N raw notes. Falls back to the raw sourced pool if
    the synthesiser is slow/absent (answer=null). An LLM sits here deliberately — never in /recall."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    rl = _rate_limit(a["name"], "deep_search", 20)   # LLM synthesis per call
    if rl:
        return rl
    body = request.get_json(silent=True) or {}
    q, gate_err = normalize_query(body.get("q"))
    if gate_err:
        return jsonify(error=gate_err), 400
    k = _clip_int(body.get("k"), 12, 3, 30)               # wider pool than per-turn recall (default 5)
    conn = db(); cur = conn.cursor()
    session_id = active_session(cur, a["name"]) or body.get("session_id")   # server-authoritative
    recall_mode, pool, recalled_ids = recall_core(cur, a, q, k, session_id, body.get("tags"))
    AL_apply.log_session_recall(cur, session_id, recalled_ids)
    _recall_fastkill(cur, a, recalled_ids)   # same lifecycle safeguard on the deep path
    answer = deep_synthesize(q, pool)
    log(cur, a["name"], "deep_search", "memory", None,
        {"q": q, "n": len(pool), "synthesised": bool(answer)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(mode="deep:" + recall_mode, query=q, synthesised=bool(answer), answer=answer,
                   sources=[{"name": s["name"], "description": s.get("description"),
                             "trusted": s.get("trusted"),
                             **({"via": "related"} if s.get("via") == "related" else {}),
                             **({"relation": s["relation"]} if s.get("relation") else {})}
                            for s in pool])


@app.get("/session/<sid>/recalled")
def session_recalled(sid):
    """The distinct memories a session already has in the brain — names + one-line descriptions,
    role-filtered, capped. The auto-learn extractor passes these back into its prompt so it doesn't
    re-propose facts the session already covered. memories the session RECALLED (session_recall
    relation). PLUS memories the session CREATED (memory.source_session = sid) — so a fact added
    mid-session isn't re-proposed at session end."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = mem_read_where(a)
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT name, description FROM ("
                " SELECT m.name AS name, m.description AS description FROM session_recall sr "
                "   JOIN memory m ON m.id = sr.memory_id "
                "   WHERE sr.session_id = %s AND m.name IS NOT NULL AND " + where +
                " UNION "
                " SELECT m.name AS name, m.description AS description FROM memory m "
                "   WHERE m.source_session = %s AND m.name IS NOT NULL AND " + where +
                ") q ORDER BY name LIMIT 60", [sid] + params + [sid] + params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(count=len(rows), recalled=rows)


_MTYPES = ("user", "feedback", "project", "reference", "memory")
def _norm_mtype(m):
    """coerce a proposal/memory mtype to the memory_mtype_check allowlist; unknown -> 'reference'.
    Junk types (gotcha/decision/task/...) used to pass into proposal then 500 the Keep at apply time."""
    return m if m in _MTYPES else "reference"


@app.post("/propose")
def propose():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] in ("readonly", "viewer", "approver"):
        return jsonify(error="readonly/viewer/approver agents cannot propose"), 403
    if (a.get("access_scope") or {}).get("personal_only"):   # personal-only agents never touch the shared brain
        return jsonify(error="personal-only agents cannot propose to the shared brain"), 403
    body = request.get_json(silent=True) or {}
    text = (body.get("body") or "").strip()
    if not text:
        return jsonify(error="empty proposal body"), 400
    if body.get("name") in BOOTSTRAP_PINNED and a["role"] != "manager":   # bootstrap-injected names are manager-only
        return jsonify(error="'%s' is injected as instructions into every session; only a manager may "
                             "propose a change to it" % body.get("name")), 403
    # deterministic provenance channel-tag, stamped here (NOT LLM-guessed)
    channel = body.get("origin_channel") or "agent-reasoning"
    chash = compute_content_hash(body.get("name"), text)   # canonical name+body hash (was agent+body)
    # Record the REAL verdict (re-derived from the cited channels), not a blanket quarantine,
    # so the dashboard shows trusted vs quarantined correctly. A /propose is still ALWAYS
    # queued pending — only /autolearn/ingest (manager) may auto-keep.
    trust = AL_orch.effective_trust({"cited_channels": body.get("cited_channels"),
                                     "trust": body.get("trust")})
    conn = db(); cur = conn.cursor()
    # /propose used to do an unconditional INSERT — no dedup — so the re-silting autolearn
    # path (and any repeated manual propose) piled duplicate PENDING rows into the review queue
    # (127 pending on. Apply the same cheap gate _ingest_candidates uses: skip if the
    # fact is already captured verbatim as a live memory, or already sitting as a pending proposal.
    if body.get("name"):
        cur.execute("SELECT body FROM memory WHERE name=%s AND deleted_at IS NULL LIMIT 1", (body.get("name"),))
        _ex = cur.fetchone()
        if _ex and (_ex[0] or "").strip() == text:
            log(cur, a["name"], "propose_skip", "proposal", None,
                {"name": body.get("name"), "reason": "already_captured_identical"})
            conn.commit(); cur.close(); conn.close()
            return jsonify(ok=True, status="skipped_duplicate",
                           reason="a live memory of that name already holds this exact body")
    if chash:
        cur.execute("SELECT id FROM proposal WHERE content_hash=%s AND status='pending' LIMIT 1", (chash,))
        _dup = cur.fetchone()
        if _dup:
            log(cur, a["name"], "propose_skip", "proposal", str(_dup[0]),
                {"name": body.get("name"), "reason": "identical_pending_proposal"})
            conn.commit(); cur.close(); conn.close()
            return jsonify(ok=True, status="skipped_duplicate", proposal_id=str(_dup[0]),
                           reason="an identical proposal is already pending review")
    # stamp the body's server-recorded CURRENT session so an approved proposal forms
    # recalled->created usage edges (apply_proposal.link_usage reads proposal.source_session, which
    # this endpoint previously never stored). Prefer the reliable server record over the passed value.
    sid = active_session(cur, a["name"]) or body.get("source_session")
    # deterministic sensitivity bump (parity with /autolearn/ingest) + mtype normalization so a
    # medical/finance proposal isn't stored 'normal', and a junk mtype can't 500 the eventual Keep.
    sens = AL_orch.sensitivity_of({"name": body.get("name"), "description": body.get("description"),
                                   "proposed_body": text, "sensitivity": body.get("sensitivity")})
    cur.execute("INSERT INTO proposal(name,mtype,proposed_body,description,origin_channel,trust,"
                "sensitivity,author_body,content_hash,source_session,tags,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id",
                (body.get("name"), _norm_mtype(body.get("mtype")), text, body.get("description"),
                 channel, trust, sens, a["name"], chash, sid, list(body.get("tags") or [])))
    pid = cur.fetchone()[0]
    log(cur, a["name"], "propose", "proposal", str(pid), {"channel": channel, "trust": trust})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, proposal_id=str(pid), status="pending", trust=trust)


# deterministic worthiness backstop for the autolearn gate. The extractor prompt is the primary
# defence (it's told not to emit code/config/status/narration), but a cheap, HIGH-PRECISION filter here
# drops the two unambiguous splinter shapes that leak through — a bare task handle as the name (e.g.
# "") and a pure short ALL-CAPS constant capture (e.g. "DEEP_BODY_CAP = 2200"). Validated FP=0 vs all
# 342 live trusted notes (the 2 borderline config-notes have trailing prose >40 chars → not matched;
# near-identical config captures are already caught by the semantic gate anyway). Conservative by
# design: it is NOT meant to catch every splinter (prose splinters are the prompt's job), only to never
# false-drop a genuine fact. Returns a reason string to skip, or None to keep gating normally.
_LOW_VALUE_CONST = re.compile(r"^[A-Z][A-Z0-9_]{2,}\s*=\s*\S")

# a SHORT single-line body dominated by a strong deploy/verification token reads as a build-log
# tick ("py_compile OK", "restarted active", "Files pushed sha-matched", "Backups .bak-…"), not durable
# knowledge. This is the deterministic net that catches the tool-output STATUS splinters the LLM judge is
# meant to drop — so ingest degrades gracefully when the judge (Ollama) is unavailable and fails open.
# HIGH-PRECISION alternatives only; validated FP=0 vs all 1878 live trusted notes (see splinter_eval);
# bare identifier mentions like "hashed using SHA256" are deliberately NOT matched (only "sha-matched").
_STATUS_SPLINTER = re.compile(
    r"(?i)("
    r"^py_?compile\b"
    r"|\bsha-?match(ed)?\b"
    r"|^bash -n\b|^node --check\b"
    r"|^backups?\b.*\.bak-"
    r"|\brestarted\s+(active|clean|ok)\b"
    r"|^files?\s+pushed\b"
    r"|^no divergence\b"
    r")")


def _low_value_candidate(name, text):
    nm = (name or "").strip()
    if re.fullmatch(r"T\d+", nm):
        return "bare_task_handle"
    b = (text or "").strip()
    if "\n" not in b and len(b) < 40 and _LOW_VALUE_CONST.match(b):
        return "code_constant"
    if "\n" not in b and len(b) < 120 and _STATUS_SPLINTER.search(b):   # deploy/verify status tick
        return "status_splinter"
    return None


# FEW-SHOT LLM worthiness judge (the eval-winning method — db/autolearn/tests/t193_worthiness_eval:
# 25% splinter-catch at 0 false-drops on 240 labeled candidates, vs 2.2% zero-shot). Complements the
# deterministic backstop above by catching PROSE/STATUS splinters it can't. Uses the local JUDGE_MODEL
# (qwen3) so the brain stays self-contained. Fail-OPEN: any Ollama error → returns False (keep gating
# normally) so the judge can NEVER block ingest. Gated by cfg("AUTOLEARN_LLM_JUDGE"). The 8 exemplars are
# the exact set the eval measured — DO NOT trim without re-running the eval (zero-shot alone catches ~2%).
_JUDGE_SYS = (
    "You decide if a PROPOSED MEMORY is worth keeping in a long-term brain. "
    "Answer KEEP only for a durable, generalizable fact/decision/preference/gotcha that would help a "
    "FUTURE session. Answer DROP for transient noise: code or config values, variable assignments, "
    "status/progress/task-completion narration, one-off command results, or session chatter. "
    "Reply with exactly one word: KEEP or DROP.\n\nExamples:\n"
    "name: DEEP_BODY_CAP | body: DEEP_BODY_CAP = 2200 -> DROP\n"
    "name: | body: marked done; committed and pushed. -> DROP\n"
    "name: api_py_edits | body: The api.py file was updated successfully twice. -> DROP\n"
    "name: brain_whoami_success | body: brain_whoami returned valid identity data name=agent. -> DROP\n"
    "name: deploy | body: After push, sha256 verified, py_compile succeeded, service restarted. -> DROP\n"
    "name: feedback_no_red_green | body: the operator is deutan colorblind; never signal by colour alone, prefer blue/orange, never red/green. -> KEEP\n"
    "name: ollama_truncate | body: Ollama ignores truncate:true for bge-m3; must truncate client-side to ~6000 chars or it 400s. -> KEEP\n"
    "name: brain_store | body: The brain canonical store is Postgres on the brain host; the git markdown mirror is only a regenerable backup. -> KEEP\n")


def _llm_worthiness_drop(name, text):
    # resilient to the transient cold-load/eviction that was the REAL cause (not a permanent
    # Vulkan hang). keep_alive:-1 keeps the model warm; retry once (a cold-load times out the 1st call
    # but warms it for the 2nd); 45s tolerates a genuine cold load. Still fail-OPEN after retries (never
    # blocks ingest) — the deterministic backstop + drain catch splinters when the judge is down.
    prompt = "name: %s | body: %s ->" % (name or "(none)", (text or "")[:1000])
    data = json.dumps({"model": JUDGE_MODEL, "think": False, "prompt": prompt, "system": _JUDGE_SYS,
                       "stream": False, "keep_alive": -1, "options": {"temperature": 0, "num_predict": 6}}).encode()
    last_err = None
    for _attempt in range(2):
        try:
            req = urllib.request.Request(JUDGE_OLLAMA_GEN, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                out = json.loads(r.read()).get("response", "").upper()
            return ("DROP" in out and "KEEP" not in out.split("DROP")[0]), None
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:120])
    return False, last_err


def _ingest_candidates(cur, a, cands, session_id):
    """Shared deterministic autolearn gate ( refactor of the /autolearn/ingest loop): given
    already-extracted candidate dicts, re-derive trust/sensitivity SERVER-SIDE, check lessons + DB
    conflict, and route each to pending review (auto_keep -> ready_to_share, escalate -> pending) /
    skip / drop. autolearn NEVER auto-writes — even a clean 'auto_keep' verdict is queued for a
    human Keep, so nothing reaches recall unreviewed. No model in here — author != validator holds.
    Caller owns the transaction (commit/close) and should rollback + 500 if this raises. Returns the
    results dict (auto_kept stays empty by design; clean candidates land in escalated as ready_to_share)."""
    lessons = AL_lessons.load_active(cur)
    results = {"auto_kept": [], "escalated": [], "skipped": [], "dropped": [], "queued_review": []}
    _judge_errs = 0   # count worthiness-judge failures this batch -> one summary log at the end
    # v2: every agent's clean first-party capture lands as its OWN PERSONAL note (author-only,
    # quarantined), self-reviewed vs the transcript next session; sensitive/conflict/unverifiable ->
    # human queue. The old/ autoapprove_own branch is gone (personal landing is generic).
    for c in cands:
        text = (c.get("proposed_body") or c.get("body") or "").strip()
        if not text:
            continue
        c.setdefault("author_body", a["name"])
        if session_id and not c.get("source_session"):
            c["source_session"] = session_id
        if not c.get("content_hash"):
            c["content_hash"] = compute_content_hash(c.get("name"), text)   # canonical

        # don't re-queue a fact already captured VERBATIM as a live memory. Session-end autolearn
        # re-extracts the same trusted notes every session; without this the queue silts to hundreds of
        # dup pending rows (was 432 on. Skip only when an existing live memory of the same
        # name holds the IDENTICAL body — a CHANGED body still queues as a candidate update for review.
        if c.get("name"):
            cur.execute("SELECT body FROM memory WHERE name=%s AND deleted_at IS NULL LIMIT 1", (c.get("name"),))
            _ex = cur.fetchone()
            if _ex and (_ex["body"] or "").strip() == text:
                results["skipped"].append(c.get("name"))
                log(cur, a["name"], "autolearn_skip", "proposal", None,
                    {"name": c.get("name"), "reason": "already_captured_identical"})
                continue

        # v2: CONTENT-BLIND global neighbour search across ALL agents' memories via the
        # SECURITY DEFINER global_neighbors() — returns (id, name, author_body, share_status, sim),
        # NEVER `body`, so no other-agent content can leak here. Used for (a) cross-agent DEDUP and
        # (b) cross-graph LINKING (neighbours kept on the candidate for the personal-land step below).
        # Supersedes's trusted-only semantic dedup. Fail-open: any error falls through to normal
        # gating so this can never block ingest. Off the session hot path (session-end).
        _sthr = float(cfg("AUTOLEARN_DEDUP_COSINE"))
        c["_neighbors"] = []
        try:
            _cvec = vec_literal(embed(text))
            cur.execute("SELECT id, name, author_body, share_status, sim "
                        "FROM global_neighbors(%s::vector, %s)", (_cvec, 8))
            c["_neighbors"] = [dict(r) for r in cur.fetchall()]
            _top = c["_neighbors"][0] if c["_neighbors"] else None
            #(c): cross-session OWN-fragment dedup (name/body sibling + value-guard) — catches an
            # author's same-topic fragments that sit BELOW the semantic cosine gate (the store's dominant
            # fragmentation source; the within-session merge cannot see across sessions). We SKIP+
            # reinforce (never CONCAT here), so cross_session_sibling's value-guard is what protects a
            # distinct fact (MTU 1300 vs 1344). Fail-open like the rest of this block.
            if int(cfg("AUTOLEARN_NAME_DEDUP")):
                _nfloor = float(cfg("AUTOLEARN_NAME_DEDUP_FLOOR"))
                for _nb in c["_neighbors"]:
                    if _nb["author_body"] != c["author_body"] or float(_nb["sim"]) < _nfloor:
                        continue
                    cur.execute("SELECT body FROM memory WHERE id=%s AND deleted_at IS NULL", (_nb["id"],))
                    _br = cur.fetchone()
                    if _br and AL_extract.cross_session_sibling(c.get("name"), text, _nb["name"], _br["body"] or ""):
                        if c.get("source_session"):
                            try:
                                cur.execute("SAVEPOINT xs_dedup")
                                AL_apply.link_usage(cur, c["source_session"], _nb["id"], by="autolearn-xsession-dedup")
                                cur.execute("RELEASE SAVEPOINT xs_dedup")
                            except Exception:
                                cur.execute("ROLLBACK TO SAVEPOINT xs_dedup")
                        results["skipped"].append(c.get("name"))
                        log(cur, a["name"], "autolearn_skip", "proposal", None,
                            {"name": c.get("name"), "reason": "cross_session_name_dup",
                             "matched": _nb["name"], "sim": round(float(_nb["sim"]), 4)})
                        c["_xs_deduped"] = True
                        break
            if c.get("_xs_deduped"):
                continue
            if _top and 0.0 < _sthr < 1.0 and float(_top["sim"]) >= _sthr:
                # A near-duplicate already exists somewhere in the brain (any agent, any tier).
                if _top["author_body"] == c["author_body"]:
                    # don't just drop a recurring own fact — REINFORCE the graph. Link the matched
                    # existing note to what THIS session recalled (relates_to via link_usage), so a fact
                    # that keeps coming up strengthens its edges instead of vanishing silently. Best-effort
                    # under a SAVEPOINT: a link error can never fail the dedup/ingest.
                    if c.get("source_session"):
                        try:
                            cur.execute("SAVEPOINT dedup_reinforce")
                            AL_apply.link_usage(cur, c["source_session"], _top["id"], by="autolearn-dedup-reinforce")
                            cur.execute("RELEASE SAVEPOINT dedup_reinforce")
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT dedup_reinforce")
                    results["skipped"].append(c.get("name"))               # my own dup -> already captured
                    log(cur, a["name"], "autolearn_skip", "proposal", None,
                        {"name": c.get("name"), "reason": "near_dup_own",
                         "matched": _top["name"], "sim": round(float(_top["sim"]), 4)})
                    continue
                _vw, _vp = mem_read_where(a)                                # is it already visible to me?
                cur.execute("SELECT 1 FROM memory WHERE id=%s AND " + _vw, [_top["id"]] + _vp)
                if cur.fetchone():
                    results["skipped"].append(c.get("name"))               # already shared to me -> dedup
                    log(cur, a["name"], "autolearn_skip", "proposal", None,
                        {"name": c.get("name"), "reason": "near_dup_visible",
                         "matched": _top["name"], "sim": round(float(_top["sim"]), 4)})
                    continue
                # Owned by ANOTHER agent and NOT visible to me: instead of DUPLICATING, raise a
                # manager-reviewed SHARE REQUEST (owner's memory -> this agent). Content-blind: only
                # ids/names/owner are surfaced, never body. A manager confirms same-fact + shares.
                cur.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
                for _mrow in cur.fetchall():
                    cur.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) "
                                "VALUES (%s,%s,%s,%s,'share_request')",
                                ("autolearn-dedup", _mrow["name"],
                                 "share-request: '%s' -> %s" % (_top["name"], c["author_body"]),
                                 "Autolearn found %s's capture matches existing memory '%s' (id %s, owned by %s, "
                                 "sim %.3f). Instead of a duplicate, consider sharing that memory to %s's audience "
                                 "after confirming they are the same fact."
                                 % (c["author_body"], _top["name"], str(_top["id"]), _top["author_body"],
                                    float(_top["sim"]), c["author_body"])))
                results["skipped"].append(c.get("name"))
                log(cur, a["name"], "autolearn_share_request", "memory", str(_top["id"]),
                    {"candidate": c.get("name"), "owner": _top["author_body"], "sim": round(float(_top["sim"]), 4)})
                continue
        except Exception as _sderr:
            log(cur, a["name"], "autolearn_dedup_error", "proposal", None,
                {"name": c.get("name"), "error": str(_sderr)[:200]})

        # deterministic worthiness backstop — drop unambiguous splinters (bare task handle name,
        # pure short ALL-CAPS constant capture) the extractor prompt let through. Skip, don't queue.
        _lv = _low_value_candidate(c.get("name"), text)
        if _lv:
            results["skipped"].append(c.get("name"))
            log(cur, a["name"], "autolearn_skip", "proposal", None,
                {"name": c.get("name"), "reason": "low_value_splinter", "kind": _lv})
            continue

        # few-shot LLM worthiness judge (gated, fail-open) — catches PROSE/STATUS splinters the
        # deterministic backstop can't (eval: +25% catch, 0 false-drops). Log the body snippet so a rare
        # wrong drop is recoverable from action_log. Runs last (only on candidates that survived the
        # cheap gates), so at most one Ollama call per surviving candidate at session-end (off hot path).
        if int(cfg("AUTOLEARN_LLM_JUDGE")):
            _drop, _jerr = _llm_worthiness_drop(c.get("name"), text)
            if _jerr:
                _judge_errs += 1                               # judge unavailable for this candidate
            if _drop:
                results["skipped"].append(c.get("name"))
                log(cur, a["name"], "autolearn_skip", "proposal", None,
                    {"name": c.get("name"), "reason": "llm_worthiness_drop", "body_snip": text[:120]})
                continue

        cv = AL_conflict.verdict_for(cur, c)
        d = AL_orch.decide_one(c, lesson_rows=lessons, conflict_verdict=cv)
        c["sensitivity"] = d.sensitivity

        if d.action == "skip":
            results["skipped"].append(c.get("name"))
            log(cur, a["name"], "autolearn_skip", "proposal", None, {"name": c.get("name"), "reason": "dup"})
            continue

        # (validate-on-recall, project_doc design-recall-validation): a clean, NEW-name,
        # session-backed capture is written as the author's OWN PERSONAL note (author-only) + linked
        # into the graph, then self-validated vs its source transcript at recall and self-trusted or
        # deleted (build 2/5). It lands whether trust is 'trusted' (first-party auto_keep) OR
        # 'quarantined' (own-session capture citing tool-output) — the author validates the latter on
        # recall. Falls through to the proposal queue ONLY for: a SAME-NAME update (approval must
        # supersede the live row via apply_proposal's retire, not pre-nuke it), a session-less/
        # UNVERIFIABLE capture, or a HUMAN-ONLY verdict (sensitive/conflict/lesson -> human; drop ->
        # rejected). The pre- behaviour (only auto_keep lands) is preserved when the knob is off.
        _same_name = False
        _live_author = None
        _live_share = None
        if c.get("name"):
            cur.execute("SELECT author_body, share_status FROM memory WHERE name=%s AND deleted_at IS NULL LIMIT 1", (c["name"],))
            _lr = cur.fetchone()
            if _lr:
                _same_name = True
                _live_author = _lr["author_body"] if isinstance(_lr, dict) else _lr[0]
                _live_share = _lr["share_status"] if isinstance(_lr, dict) else _lr[1]
        # (Approval 2.0 step 3): a same-name capture whose content DIFFERS (conflict verdict) is a
        # correction. Auto-supersede it ONLY when it is the SAME author's own note AND that note is not an
        # already manager-validated shared/trusted fact — then it lands as the author's personal note and
        # apply_proposal's retire_prior supersedes the stale row (+ a supersedes edge). A cross-author
        # same-name, or a correction to a shared/trusted fact, keeps _auto_supersede False and still
        # escalates to the human queue below. (The 0.90 near-dup gate already skipped true duplicates.)
        _auto_supersede = bool(_same_name and _live_author == c["author_body"]
                               and (_live_share or "") not in ("trusted", "shared"))
        _land = (d.action == "auto_keep" and c.get("source_session") and not _same_name)   # legacy path
        if int(cfg("AUTOLEARN_LAND_QUARANTINED")):
            _land = AL_orch.lands_personal(d, has_source=bool(c.get("source_session")), same_name=_same_name,
                                           land_sensitive=bool(int(cfg("AUTOLEARN_LAND_SENSITIVE"))),
                                           allow_supersede=_auto_supersede)
        if _land:
            c["share_status"] = "personal"           # author-only PRIVATE tier (not shared, not cross-agent trusted)
            if "uncorroborated" in (d.reasons or ()):        # mark it + keep its source pointer so it's
                c["tags"] = sorted(set((c.get("tags") or []) + ["uncorroborated"]))   # validatable (origin+source already stored)
            try:
                cur.execute("SAVEPOINT rts")
                mem_id = AL_apply.apply_proposal(cur, c, embed_fn=embed, vec_fn=vec_literal, trust=d.trust)  # land with the RE-DERIVED trust (quarantined for a tool-output capture, trusted for a first-party auto_keep). Cross-agent gate is share_status='personal', NOT trust; author self-validates vs source at recall.
                try:                                                        # explicit [[ref]] edges
                    cur.execute("SAVEPOINT expl_refs")
                    AL_apply.link_explicit_refs(cur, mem_id, text, by="autolearn-ref")   # machine-parsed links get a distinct stamp so they never claim recall's author-reserved slot
                    _index_memory_entities(cur, mem_id, text)   # index local-LLM capture entities
                    cur.execute("RELEASE SAVEPOINT expl_refs")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT expl_refs")
                AL_apply.link_usage(cur, c.get("source_session"), mem_id)   # recalled->created edges
                # v2: cross-graph relates_to links. gate on a similarity FLOOR + a per-note cap
                # so a landed note doesn't spray relates_to edges to weak topical-coincidence neighbours
                # (which recall's 1-hop expansion then surfaces). global_neighbors() returns sim-DESC, so
                # the first below-floor neighbour means all the rest are too -> break.
                _lthr = float(cfg("AUTOLEARN_LINK_COSINE"))
                _lcap = int(cfg("AUTOLEARN_LINK_CAP"))
                _cands = [nb for nb in (c.get("_neighbors") or [])
                          if str(nb["id"]) != str(mem_id) and float(nb.get("sim") or 0.0) >= _lthr][:_lcap]
                _picked = None
                if _cands and int(cfg("AUTOLEARN_VET_LINKS")):     # LLM menu-pick vetting (falls back to blind on any LLM failure)
                    _picked = _vet_neighbor_links(cur, c, _cands, int(cfg("VET_LINK_TIMEOUT")))
                if _picked is not None:                            # vetting ran -> write only the LLM-picked subset; stronger types -> proposed_type ( review)
                    for _id, _pt in _picked:
                        cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight,proposed_type) "
                                    "VALUES (%s,%s,'relates_to','autolearn-ref',%s,1,%s) "
                                    "ON CONFLICT (src_id,dst_id,rel_type) DO UPDATE SET "
                                    "weight=memory_relation.weight+1, updated_at=now()",
                                    (mem_id, _id, d.sensitivity, _pt))
                else:                                              # vetting off/unavailable -> original blind link (no data loss)
                    for _nb in _cands:
                        cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight) "
                                    "VALUES (%s,%s,'relates_to','autolearn-v2-link',%s,1) "
                                    "ON CONFLICT (src_id,dst_id,rel_type) DO UPDATE SET "
                                    "weight=memory_relation.weight+1, updated_at=now()",
                                    (mem_id, _nb["id"], d.sensitivity))
                cur.execute("RELEASE SAVEPOINT rts")
            except Exception as _rtserr:
                cur.execute("ROLLBACK TO SAVEPOINT rts")
                log(cur, a["name"], "autolearn_personal_error", "proposal", None,
                    {"name": c.get("name"), "error": str(_rtserr)[:200]})
                # fail-safe: fall through to the normal queue path below on any error
            else:
                log(cur, a["name"], "autolearn_personal", "memory", str(mem_id), {"name": c.get("name"), "trust": d.trust})
                results["queued_review"].append({"name": c.get("name"), "memory_id": str(mem_id), "tier": "personal", "trust": d.trust})
                continue

        # Proposal queue: reached only when the capture did NOT land personal above — a same-name
        # update / session-less capture / a HUMAN-ONLY verdict (sensitive/conflict/lesson) -> PENDING
        # for the /approve queue; drop -> rejected (audited). Nothing here reaches trusted recall
        # without a human/manager Keep. The deterministic gate (decide_one) is UNCHANGED — only
        # widened which decisions materialize as a personal note above (validate-on-recall).
        status = {"auto_keep": "pending", "escalate": "pending", "drop": "rejected"}[d.action]
        decided_by = "autolearn" if d.action == "drop" else None
        reason_txt = ("ready_to_share: clean first-party — keep on review"
                      if d.action == "auto_keep" else (", ".join(d.reasons) or None))
        cur.execute("INSERT INTO proposal(name,mtype,proposed_body,description,origin_channel,trust,"
                    "sensitivity,author_body,source_session,content_hash,status,decided_by,reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (c.get("name"), _norm_mtype(c.get("mtype")), text, c.get("description"),
                     c.get("origin_channel") or "unknown", d.trust, d.sensitivity, c["author_body"],
                     c.get("source_session"), c["content_hash"], status, decided_by, reason_txt))
        pid = cur.fetchone()["id"]
        if decided_by:
            cur.execute("UPDATE proposal SET decided_at=now() WHERE id=%s", (pid,))

        if d.action == "drop":
            log(cur, a["name"], "autolearn_drop", "proposal", str(pid), {"reasons": d.reasons})
            results["dropped"].append({"name": c.get("name"), "reasons": d.reasons})
        else:  # auto_keep OR escalate -> pending review queue; nothing is auto-written
            verb = "autolearn_ready_to_share" if d.action == "auto_keep" else "autolearn_escalate"
            rlist = ["ready_to_share"] if d.action == "auto_keep" else d.reasons
            log(cur, a["name"], verb, "proposal", str(pid), {"reasons": rlist})
            results["escalated"].append({"name": c.get("name"), "proposal_id": str(pid), "reasons": rlist})
    # if the worthiness judge (Ollama) errored for any candidate this batch, they passed UNJUDGED
    # (fail-open) — the deterministic backstop still applied, but make the outage VISIBLE in the audit log
    # instead of it silently letting prose/status splinters through.
    if _judge_errs:
        log(cur, a["name"], "autolearn_judge_down", "proposal", None,
            {"errors": _judge_errs, "candidates": len(cands),
             "note": "worthiness judge unavailable; candidates passed unjudged (deterministic backstop still applied)"})
    return results


@app.post("/autolearn/ingest")
def autolearn_ingest():
    """Privileged auto-learn intake (manager only). Accepts a batch of ALREADY-extracted,
    ALREADY-scrubbed candidate proposals (the extractor LLM ran pipeline-side) and runs the
    DETERMINISTIC gate HERE, server-side: re-derive trust from the cited channels, check
    sensitivity + lessons + DB conflict, then route each candidate to one of:
      auto_keep -> apply to memory immediately (status approved, decided_by 'autolearn');
      escalate  -> queue pending for /approve;  skip -> exact dup, nothing to do;
      drop      -> matched a critical lesson, recorded rejected for audit.
    The LLM proposes; THIS gate (no model in it) validates -> author != validator holds.
    Every candidate is logged to action_log; auto-keeps are fully reversible (soft-delete)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required for auto-learn ingest"), 403
    body = request.get_json(silent=True) or {}
    cands = body.get("candidates")
    if not isinstance(cands, list):
        return jsonify(error="candidates[] required"), 400
    session_id = body.get("session_id")

    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        results = _ingest_candidates(cur, a, cands, session_id)   # shared gate
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("autolearn ingest failed", 500, e)
    conn.commit(); cur.close(); conn.close()
    counts = {k: len(v) for k, v in results.items()}
    return jsonify(ok=True, counts=counts, results=results)




_AUTOLEARN_EXTRACT_LOCK = 823042   # fixed advisory-lock key serializing /autolearn/extract drains


@app.post("/autolearn/extract")
def autolearn_extract():
    """SERVER-SIDE extraction. A manager posts {session_id, spans:[{channel,text}], author_body?,
    known?:[...]} — spans already tagged+scrubbed client-side; we re-scrub here (belt-and-suspenders)
    and run the extractor against the local model host Ollama (no ssh->the legacy host coupling). Because the brain now
    sees the real spans, the gate re-derives trust from the actual channels here — a client can no longer
    spoof trust (closes). Candidates then flow through the SAME deterministic gate as /autolearn/ingest."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required for autolearn extract"), 403
    rl = _rate_limit(a["name"], "extract", 10)   # heavy LLM extraction
    if rl:
        return rl
    body = request.get_json(silent=True) or {}
    spans = body.get("spans")
    if not isinstance(spans, list) or not spans:
        return jsonify(error="spans[] required"), 400
    session_id = body.get("session_id")
    author_body = (body.get("author_body") or a["name"]).strip()
    known = body.get("known")

    spans = AL_scrub.scrub_spans(spans)          # server-side re-scrub — secrets never reach the model or store
    diag = {}                                    # extractor fills parse-failure telemetry here
    # SERIALIZE extraction drains. All autolearn drains funnel through here, and the extractor hits
    # the single the model host Ollama model — two concurrent drains sharing it turned one call's latency past the
    # timeout. A session-level pg advisory lock (dedicated conn) makes the 2nd caller BLOCK
    # until the 1st finishes, so runs never contend. extract_session's EXTRACT_TIMEOUT bounds the hold, so
    # a stuck Ollama can't wedge the lock forever. Same advisory-lock idiom as the T-number allocator
    # (pg_advisory_xact_lock 8123) + the migrate runner (823041).
    lock_conn = db(); lock_cur = lock_conn.cursor()
    lock_cur.execute("SELECT pg_advisory_lock(%s)", (_AUTOLEARN_EXTRACT_LOCK,))
    try:
        cands = AL_extract.extract_session(spans, AL_extract.OllamaBackend(timeout=cfg("EXTRACT_TIMEOUT")),
                                           session_id=session_id, author_body=author_body, known=known, diag=diag)
    except Exception as e:
        return _internal_err("extraction failed (ollama)", 502, e)
    finally:                                     # release even on error/return so the next drain proceeds
        lock_cur.execute("SELECT pg_advisory_unlock(%s)", (_AUTOLEARN_EXTRACT_LOCK,))
        lock_cur.close(); lock_conn.close()

    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        results = _ingest_candidates(cur, a, cands, session_id)
        # a malformed LLM extract (non-empty output we couldn't parse) is otherwise silent —
        # indistinguishable from "nothing to learn". Log a visible action_log row so a broken
        # model/prompt that disables learning shows up instead of failing quiet for weeks.
        if diag.get("windows_unparseable"):
            log(cur, a["name"], "autolearn_parse_error", "session", session_id,
                {"windows_unparseable": diag["windows_unparseable"], "windows_total": diag.get("windows_total"),
                 "candidates": len(cands)})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("autolearn extract-ingest failed", 500, e)
    conn.commit(); cur.close(); conn.close()
    counts = {k: len(v) for k, v in results.items()}
    return jsonify(ok=True, extracted=len(cands), counts=counts, results=results)


@app.post("/session/ingest")
def session_ingest():
    """Going-forward transcript intake. Manager-only. The client SessionEnd hook parses its
    just-ended .jsonl, redacts client-side, and POSTs {source_session, agent_body, turns[...]}. We
    redact() AGAIN here (idempotent regex, belt-and-suspenders) so a raw secret can never reach the
    store even if the client step were bypassed. Idempotent by source_session (skip if present) ->
    safe to retry and cannot collide with the backfill. Inserts session + session_turn + bge-m3."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required for session ingest"), 403
    body = request.get_json(silent=True) or {}
    src = (body.get("source_session") or "").strip()
    agent_body = (body.get("agent_body") or a["name"]).strip()
    turns_in = body.get("turns")
    if not src or not isinstance(turns_in, list):
        return jsonify(error="source_session + turns[] required"), 400
    if len(turns_in) > 20000:                            #/B1-14: bound payload (manager-only, but not unbounded)
        return jsonify(error="too many turns (max 20000)"), 413

    turns = []
    for t in turns_in:
        if t.get("role") not in ("user", "assistant"):
            continue
        text = redact((t.get("text") or "")).strip()[:100000]   #/B1-14: cap per-turn text at 100k chars
        if text:
            turns.append((t["role"], t.get("ts"), text))
    if not turns:
        return jsonify(ok=True, status="empty", turns=0)

    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id FROM session WHERE source_session=%s", (src,))
    if cur.fetchone():
        cur.close(); conn.close()
        return jsonify(ok=True, status="skip", reason="already ingested", source_session=src)

    started = body.get("started_at") or turns[0][1]
    ended = body.get("ended_at") or turns[-1][1]
    sens = body.get("sensitivity") or "normal"
    cur.execute("INSERT INTO session(source_session,agent_body,started_at,ended_at,turn_count,"
                "sensitivity,origin_channel) VALUES (%s,%s,%s,%s,%s,%s,'chat-archive') RETURNING id",
                (src, agent_body, started, ended, len(turns), sens))
    sid = cur.fetchone()[0]
    embedded = 0
    for i, (role, tts, text) in enumerate(turns):
        emb = None
        try:
            emb = vec_literal(embed(text)); embedded += 1
        except Exception:
            emb = None
        cur.execute("INSERT INTO session_turn(session_id,idx,role,ts,text,embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s::vector)", (sid, i, role, tts, text, emb))
    log(cur, a["name"], "session_ingest", "session", str(sid),
        {"source_session": src, "agent_body": agent_body, "turns": len(turns), "embedded": embedded})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, status="ok", session_id=str(sid), turns=len(turns), embedded=embedded)



@app.get("/autolearn/last")
def autolearn_last():
    """Dashboard signal: the most recent auto-learn run — its timestamp + per-action counts,
    read from action_log (all autolearn_* rows of one /autolearn/ingest call share a single
    transaction timestamp, so grouping on the max timestamp gives that run's breakdown). Read-only;
    any authenticated agent (mTLS + bearer). Returns nulls if auto-learn has never run."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT max(created_at) AS ts FROM action_log WHERE action LIKE %s", ("autolearn%",))
    row = cur.fetchone()
    last = row["ts"] if row else None
    if not last:
        cur.close(); conn.close()
        return jsonify(ok=True, last_run_at=None, actor=None, counts={}, total=0)
    cur.execute("SELECT action, count(*) AS n, max(actor) AS actor FROM action_log "
                "WHERE action LIKE %s AND created_at = %s GROUP BY action", ("autolearn%", last))
    rows = cur.fetchall()
    counts = {r["action"].replace("autolearn_", ""): r["n"] for r in rows}
    actor = rows[0]["actor"] if rows else None
    cur.close(); conn.close()
    return jsonify(ok=True, last_run_at=last.isoformat(), actor=actor,
                   counts=counts, total=sum(counts.values()))



@app.post("/session/search")
def session_search():
    """Brain-native transcript search — replaces the legacy host-backed search_conversations.
    Hybrid over session_turn: dense bge-m3 (embedding <=>) + Postgres FTS (tsv), RRF-fused. Access
    gated by the OWNING session's sensitivity/readers (via access_where on session; session has
    deleted_at but no invalid_at, so temporal=False). Optional role ('user'|'assistant') + agent_body
    filters. Returns fused turn snippets with session context + memories linked by source_session."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if not q:
        return jsonify(error="q required"), 400
    try:
        k = max(1, min(int(body.get("k", 10)), 50))
    except Exception:
        k = 10
    role = body.get("role") if body.get("role") in ("user", "assistant") else None
    agent_body = (body.get("agent_body") or "").strip() or None
    pool = max(k * 2, cfg("RECALL_POOL"))                 # live-tunable

    # session-level access subquery (trusted fragment from our code; params passed positionally)
    sess_where, sess_params = access_where(a, temporal=False)
    inner_conds = [sess_where]
    inner_params = list(sess_params)
    if agent_body:
        inner_conds.append("agent_body = %s")
        inner_params.append(agent_body)
    inner = "SELECT id FROM session WHERE " + " AND ".join(inner_conds)
    role_clause = " AND st.role = %s" if role else ""

    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    search_mode = "hybrid"
    dense = []
    try:                                                   # embedder down/timed out -> keyword-only, not a 500
        vec = vec_literal(embed(q))
        dense_sql = ("SELECT st.id FROM session_turn st WHERE st.session_id IN (" + inner + ")"
                     + role_clause + " AND st.embedding IS NOT NULL "
                     "ORDER BY st.embedding <=> %s::vector LIMIT %s")
        cur.execute(dense_sql, inner_params + ([role] if role else []) + [vec, pool])
        dense = [r["id"] for r in cur.fetchall()]
    except Exception:
        search_mode = "keyword_only"
        try:
            conn.rollback()                                # clear any aborted-txn state from a failed dense execute
        except Exception:
            pass
    fts_sql = ("SELECT st.id FROM session_turn st WHERE st.session_id IN (" + inner + ")"
               + role_clause + " AND st.tsv @@ websearch_to_tsquery('english', %s) "
               "ORDER BY ts_rank(st.tsv, websearch_to_tsquery('english', %s)) DESC LIMIT %s")
    cur.execute(fts_sql, inner_params + ([role] if role else []) + [q, q, pool])
    kw = [r["id"] for r in cur.fetchall()]

    scores = {}
    for rank, tid in enumerate(dense):
        scores[tid] = scores.get(tid, 0) + 1.0 / (RRF_K + rank)
    for rank, tid in enumerate(kw):
        scores[tid] = scores.get(tid, 0) + 1.0 / (RRF_K + rank)
    top = sorted(scores, key=lambda t: -scores[t])[:k]
    if not top:
        cur.close(); conn.close()
        return jsonify(ok=True, q=q, mode=search_mode, count=0, results=[], linked_memories=[])

    cur.execute("SELECT st.id, st.idx, st.role, st.ts, left(st.text, 500) AS snippet, "
                "s.id AS session_id, s.source_session, s.agent_body, s.started_at "
                "FROM session_turn st JOIN session s ON s.id = st.session_id "
                "WHERE st.id = ANY(%s)", (top,))
    byid = {r["id"]: r for r in cur.fetchall()}
    results = []
    for tid in top:
        r = byid.get(tid)
        if not r:
            continue
        results.append({"session_id": str(r["session_id"]), "source_session": r["source_session"],
                        "agent_body": r["agent_body"], "idx": r["idx"], "role": r["role"],
                        "ts": r["ts"].isoformat() if r["ts"] else None,
                        "snippet": r["snippet"], "score": round(scores[tid], 6)})

    # memories linked to these sessions by source_session (tier-gated like normal recall)
    srcs = sorted({r["source_session"] for r in results if r["source_session"]})
    linked = []
    if srcs:
        mwhere, mparams = mem_read_where(a)
        cur.execute("SELECT name, source_session FROM memory WHERE source_session = ANY(%s) AND "
                    + mwhere + " AND name IS NOT NULL", [srcs] + mparams)
        linked = [{"name": r["name"], "source_session": r["source_session"]} for r in cur.fetchall()]
    log(cur, a["name"], "session_search", "session", None, {"q": q[:80], "hits": len(results)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, q=q, mode=search_mode, count=len(results), results=results, linked_memories=linked)



@app.get("/session/<sid>/turns")
def session_turns(sid):
    """Full turns of ONE session for the dashboard transcript viewer. Looks up the session by
    source_session (exact match preferred, else prefix — the dashboard passes a short id), access-gated
    by the session's sensitivity/readers (access_where; session has deleted_at but no invalid_at, so
    temporal=False). Optional ?q= (case-insensitive substring filter) + ?limit= (last N turns, default
    400). Text is already redacted at ingest. Replaces the dashboard's local chat-archive file read."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    q = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 400)), 2000))
    except Exception:
        limit = 400
    sess_where, sess_params = access_where(a, temporal=False)
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, source_session, agent_body FROM session "
                "WHERE (source_session = %s OR source_session LIKE %s) AND " + sess_where +
                " ORDER BY (source_session = %s) DESC, started_at DESC NULLS LAST LIMIT 1",
                [sid, sid + "%"] + sess_params + [sid])
    s = cur.fetchone()
    if not s:
        cur.close(); conn.close()
        return jsonify(ok=False, error="no session found for " + sid, turns=[]), 404
    params = [s["id"]]
    qclause = ""
    if q:
        qclause = " AND text ILIKE %s"
        params.append("%" + q + "%")
    cur.execute("SELECT count(*) AS n FROM session_turn WHERE session_id = %s" + qclause, params)
    total = cur.fetchone()["n"]
    cur.execute("SELECT role, ts, text FROM session_turn WHERE session_id = %s" + qclause +
                " ORDER BY idx DESC LIMIT %s", params + [limit])
    rows = cur.fetchall()
    rows.reverse()  # we pulled the last N by idx DESC -> restore chronological order
    turns = [{"role": r["role"], "ts": r["ts"].isoformat() if r["ts"] else None, "text": r["text"]}
             for r in rows]
    cur.close(); conn.close()
    return jsonify(ok=True, session=s["source_session"], source_session=s["source_session"],
                   agent_body=s["agent_body"], total=total, returned=len(turns), turns=turns)

# ---------------------------------------------------------------------------
# Wave 2 — enrollment / onboarding (two-body approval)
# ---------------------------------------------------------------------------
import secrets as _secrets  # noqa: E402

QUESTIONNAIRE = {
    "message": "Welcome. To enroll with the brain, POST your answers to /enroll.",
    "fields": {
        "proposed_name": "the identity name you request (e.g. 'scribe')",
        "purpose": "what you are and what you'll use the brain for",
        "agent_host": "where you run (host / container)",
        "requested_role": "manager | worker | readonly",
        "answers": "object — any extra context the managers should weigh",
        "csr": "optional PEM CSR (CN = your name) so a manager can issue your mTLS cert on approval",
    },
    "next": "POST returns an enrollment_id + one-time enroll_secret. the required managers must approve. "
            "Poll GET /enroll/status?id=&secret= ; on approval it returns your token + welcome ONCE — store it on your own disk.",
}


@app.get("/enroll")
def enroll_info():
    return jsonify(QUESTIONNAIRE)


@app.post("/enroll")
def enroll_submit():
    b = request.get_json(silent=True) or {}
    name = (b.get("proposed_name") or "").strip()
    if not name:
        return jsonify(error="proposed_name required"), 400
    role = b.get("requested_role") if b.get("requested_role") in ("manager", "worker", "readonly") else "readonly"
    # bound the untrusted payload — /enroll is open + cert-exempt, so cap it to stop DB bloat.
    answers = b.get("answers") or {}
    if len(json.dumps(answers)) > 8192 or len(b.get("csr") or "") > 8192:
        return jsonify(error="enrollment payload too large (answers/csr max 8KB each)"), 413
    secret = "enr_" + _secrets.token_urlsafe(24)
    conn = db(); cur = conn.cursor()
    # reject a name that already belongs to a live agent up front (it could never be provisioned
    # anyway) — stops enrollment being used to probe/collide with existing identities.
    cur.execute("SELECT 1 FROM agent WHERE name=%s AND revoked_at IS NULL", (name,))
    if cur.fetchone():
        cur.close(); conn.close()
        return jsonify(error="an agent named '%s' already exists; choose a different proposed_name" % name), 409
    cur.execute("INSERT INTO enrollment(proposed_name,purpose,agent_host,requested_role,answers,csr,"
                "enroll_secret_hash,origin_channel,trust) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'agent-reasoning','quarantined') RETURNING id",
                (name, b.get("purpose"), b.get("agent_host"), role,
                 psycopg2.extras.Json(answers), b.get("csr"), tok_hash(secret)))
    eid = cur.fetchone()[0]
    log(cur, "enroll-anon", "enroll_submit", "enrollment", str(eid), {"requested_role": role, "proposed_name": name})   #/A2-8: actor not attacker-chosen
    conn.commit(); cur.close(); conn.close()
    return jsonify(enrollment_id=str(eid), enroll_secret=secret, status="pending",
                   message="Application received. the required managers must approve. Poll /enroll/status?id=&secret=.")


@app.get("/enroll/pending")
def enroll_pending():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT e.id,e.proposed_name,e.purpose,e.agent_host,e.requested_role,e.answers,e.status,"
                "e.created_at, COALESCE(array_agg(ea.approver) FILTER (WHERE ea.decision='approve'), '{}') AS approvers "
                "FROM enrollment e LEFT JOIN enrollment_approval ea "
                "ON ea.enrollment_id=e.id "
                "WHERE e.status IN ('pending','approved') AND e.deleted_at IS NULL "
                "GROUP BY e.id ORDER BY e.created_at")
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"]); r["created_at"] = r["created_at"].isoformat()
        appr = r.get("approvers") or []
        r["approve_count"] = len(appr)
    cur.close(); conn.close()
    return jsonify(pending=rows, required=cfg("ENROLL_APPROVALS"))


@app.post("/agent/<name>/revoke")
def agent_revoke(name):
    """/A2-10: revoke (or with {"unrevoke":true} restore) an agent's access — manager only.
    Sets/clears agent.revoked_at; authenticate() already rejects a revoked agent, so this is the
    kill-switch that was missing (no UPDATE agent path existed before). A manager can't revoke itself."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    if name == a["name"]:
        return jsonify(error="cannot revoke your own agent"), 400
    b = request.get_json(silent=True) or {}
    conn = db(); cur = conn.cursor()
    if b.get("unrevoke"):
        cur.execute("UPDATE agent SET revoked_at=NULL, updated_at=now() WHERE name=%s RETURNING name", (name,))
        action = "agent_unrevoke"
    else:
        cur.execute("UPDATE agent SET revoked_at=now(), updated_at=now() WHERE name=%s AND revoked_at IS NULL "
                    "RETURNING name", (name,))
        action = "agent_revoke"
    r = cur.fetchone()
    if not r:
        conn.rollback(); cur.close(); conn.close()
        cur2 = db().cursor(); cur2.execute("SELECT 1 FROM agent WHERE name=%s", (name,))
        exists = cur2.fetchone() is not None; cur2.close()
        return (jsonify(ok=True, name=name, note="already in requested state") if exists
                else jsonify(error="no such agent: %s" % name)), (200 if exists else 404)
    log(cur, a["name"], action, "agent", name, {})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, name=name, action=action)


# ---- per-agent config surface for the dashboard (manager only, audited) ----
_AGENT_PUBLIC_COLS = ("name, role, lane, agent_tier, sensitivity, autoapprove_own, "
                      "welcome, readers, revoked_at, created_by, created_at, updated_at")
_AGENT_ROLES = ["manager", "approver", "viewer", "worker", "readonly"]
_AGENT_LANES = ["direct", "gated", "auto"]
_AGENT_SENS = ["public", "normal", "sensitive", "secret"]


@app.get("/agents")
def agents_list():
    """list every agent with its editable per-agent settings, for the dashboard config form."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT " + _AGENT_PUBLIC_COLS + " FROM agent ORDER BY (revoked_at IS NOT NULL), name")
    rows = []
    _lanes_seen = set()
    for r in cur.fetchall():
        d = dict(r)
        d["revoked"] = d.pop("revoked_at") is not None
        for k in ("created_at", "updated_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        if d.get("lane"):
            _lanes_seen.add(d["lane"])
        rows.append(d)
    cur.close(); conn.close()
    lanes = sorted(_lanes_seen | set(_AGENT_LANES))
    return jsonify(agents=rows, roles=_AGENT_ROLES, lanes=lanes, sensitivities=_AGENT_SENS)


@app.get("/agent/<name>/injection-preview")
def agent_injection_preview(name):
    """read-only preview of EXACTLY what /bootstrap injects for this agent at session start —
    its welcome + the role-filtered always-on rules MOC + the global overlay + the pinned raw-
    instruction names. No side effects (unlike /bootstrap, which records session state)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name, role, readers, sensitivity, access_scope, welcome FROM agent WHERE name=%s", [name])
    tgt = cur.fetchone()
    if not tgt:
        cur.close(); conn.close()
        return jsonify(error="unknown agent: %s" % name), 404
    where, params = mem_read_where(dict(tgt))
    cur.execute("SELECT body FROM memory WHERE name=%s AND " + where + " ORDER BY updated_at DESC LIMIT 1", [BOOTSTRAP_RULES_MOC] + params)
    rr = cur.fetchone()
    rules = (rr or {}).get("body") or ""
    overlay = cfg("SESSION_BRIEF_OVERLAY") or ""
    cur.close(); conn.close()
    return jsonify(agent=name, role=tgt["role"], welcome=(tgt.get("welcome") or ""),
                   rules=rules, rules_visible=bool(rules), rules_moc=BOOTSTRAP_RULES_MOC,
                   # expose the overlay under BOTH keys so tooling that reads either endpoint's
                   # key works (bootstrap historically used `overlay`, this route `global_overlay`).
                   global_overlay=overlay, overlay=overlay, pinned_names=sorted(BOOTSTRAP_PINNED))


@app.route("/agent/<name>", methods=["PATCH"])
def agent_update(name):
    """update a single agent's editable settings (manager only, audited). Gated fields
    role/lane/agent_tier/sensitivity change what the agent can do; welcome/autoapprove_own are softer.
    A manager cannot demote its own role away from manager (lock-out guard)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    b = request.get_json(silent=True) or {}
    sets, vals, changed = [], [], {}
    enums = {"role": _AGENT_ROLES, "sensitivity": _AGENT_SENS}
    for k, allowed in enums.items():
        if k in b:
            v = (b[k] or "").strip()
            if v not in allowed:
                return jsonify(error="invalid %s: %s" % (k, v)), 400
            sets.append(k + "=%s"); vals.append(v); changed[k] = v
    if "lane" in b:                                    # lane has no CHECK constraint -> free text
        v = (b["lane"] or "").strip()
        if not v or len(v) > 32:
            return jsonify(error="lane must be a short non-empty string"), 400
        sets.append("lane=%s"); vals.append(v); changed["lane"] = v
    if "agent_tier" in b:
        try:
            t = int(b["agent_tier"])
        except (TypeError, ValueError):
            return jsonify(error="agent_tier must be an integer 1..3"), 400
        if t < 1 or t > 3:
            return jsonify(error="agent_tier must be 1..3"), 400
        sets.append("agent_tier=%s"); vals.append(t); changed["agent_tier"] = t
    if "autoapprove_own" in b:
        sets.append("autoapprove_own=%s"); vals.append(bool(b["autoapprove_own"])); changed["autoapprove_own"] = bool(b["autoapprove_own"])
    if "welcome" in b and isinstance(b["welcome"], str):
        sets.append("welcome=%s"); vals.append(b["welcome"]); changed["welcome_len"] = len(b["welcome"])
    if not sets:
        return jsonify(error="no editable fields provided"), 400
    if name == a["name"] and changed.get("role") not in (None, "manager"):
        return jsonify(error="refusing to change your own role away from manager"), 400
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE agent SET " + ", ".join(sets) + ", updated_at=now() WHERE name=%s RETURNING name",
                vals + [name])
    if not cur.fetchone():
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="unknown agent: %s" % name), 404
    log(cur, a["name"], "agent_update", "agent", name, {"changed": changed})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, name=name, changed=changed)


# enrollment needs K distinct manager approvals (K = ENROLL_APPROVALS); any role=manager
# may vote. K = cfg("ENROLL_APPROVALS") ( live-tunable) — a single-manager box sets it to 1.


@app.post("/enroll/<eid>/approve")
def enroll_approve(eid):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":                              # any manager, no name-locked managers
        return jsonify(error="manager role required to approve"), 403
    b = request.get_json(silent=True) or {}
    decision = b.get("decision", "approve")
    if decision not in ("approve", "reject"):
        return jsonify(error="decision must be 'approve' or 'reject'"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM enrollment WHERE id=%s AND deleted_at IS NULL", (eid,))
    e = cur.fetchone()
    if not e:
        cur.close(); conn.close(); return jsonify(error="not found"), 404
    if e["status"] not in ("pending", "approved"):
        cur.close(); conn.close(); return jsonify(error="already %s" % e["status"]), 409
    if a["name"] == e["proposed_name"]:                     # an applicant may not approve itself
        cur.close(); conn.close(); return jsonify(error="an applicant may not approve its own enrollment"), 403

    arole = b.get("assign_role") if b.get("assign_role") in ("manager", "worker", "readonly") else None
    agroups = b.get("assign_groups")
    # idempotent vote — one row per (enrollment, manager); re-voting UPDATES it, never double-counts
    cur.execute("INSERT INTO enrollment_approval(enrollment_id, approver, decision, assign_role, assign_groups) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (enrollment_id, approver) DO UPDATE SET "
                "decision=EXCLUDED.decision, assign_role=EXCLUDED.assign_role, assign_groups=EXCLUDED.assign_groups",
                (eid, a["name"], decision, arole, agroups))

    cur.execute("SELECT approver, decision, assign_role, assign_groups FROM enrollment_approval "
                "WHERE enrollment_id=%s", (eid,))
    votes = cur.fetchall()
    approvers = [v["approver"] for v in votes if v["decision"] == "approve"]
    rejected = any(v["decision"] == "reject" for v in votes)
    approved = len(approvers) >= cfg("ENROLL_APPROVALS") and not rejected   # K-of-N AND no reject

    if approved:
        arole_final = next((v["assign_role"] for v in votes if v["decision"] == "approve" and v["assign_role"]),
                           e["assigned_role"] or e["requested_role"] or "readonly")
        agroups_final = next((v["assign_groups"] for v in votes if v["decision"] == "approve" and v["assign_groups"]),
                             e["assigned_groups"] if e["assigned_groups"] else ["common"])
        cur.execute("UPDATE enrollment SET status='approved', assigned_role=%s, assigned_groups=%s, decided_at=now() "
                    "WHERE id=%s", (arole_final, agroups_final, eid))
    elif rejected:                                                    #/A2-4: a blocking reject is terminal —
        cur.execute("UPDATE enrollment SET status='rejected', decided_at=now() "   # leave the pending queue (was
                    "WHERE id=%s AND status='pending'", (eid,))                    # stuck 'pending' forever)
    log(cur, a["name"], "enroll_approve", "enrollment", str(eid),
        {"by": a["name"], "decision": decision, "approvers": approvers, "rejected": rejected,
         "approved": bool(approved), "required": cfg("ENROLL_APPROVALS")})
    conn.commit(); cur.close(); conn.close()
    status = "approved" if approved else ("rejected" if rejected else e["status"])   #/A2-4
    return jsonify(ok=True, decision_by=a["name"], decision=decision, approvers=approvers,
                   approve_count=len(approvers), required=cfg("ENROLL_APPROVALS"), rejected=rejected,
                   both_approved=bool(approved), approved=bool(approved), status=status,
                   note=("ready for the applicant to pull its package" if approved else
                         "awaiting %d of %d manager approvals%s" % (len(approvers), cfg("ENROLL_APPROVALS"),
                         " (a reject is blocking)" if rejected else "")))


@app.get("/enroll/status")
def enroll_status():
    eid = request.args.get("id"); secret = request.args.get("secret", "")
    if not eid or not secret:
        return jsonify(error="id and secret required"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM enrollment WHERE id=%s AND enroll_secret_hash=%s", (eid, tok_hash(secret)))
    e = cur.fetchone()
    if not e:
        cur.close(); conn.close(); return jsonify(error="not found"), 404
    # provision lazily on the first pull after both managers approved: mint token + agent row,
    # return the plaintext token ONCE (it is never stored — only its hash).
    if e["status"] == "approved":
        token = "brain_%s_%s" % (e["proposed_name"], _secrets.token_hex(24))
        th = tok_hash(token)
        # DO NOTHING (never DO UPDATE) — provisioning must NOT be able to overwrite an existing
        # live agent's token/role/scope. If the name is already taken, refuse and leave the enrollment
        # 'approved' (un-provisioned) so a manager can reassign a non-colliding name.
        cur.execute("INSERT INTO agent(name,cert_cn,role,welcome,lane,agent_tier,access_scope,readers,"
                    "sensitivity,created_by,token_hash,token_prefix) "
                    "VALUES (%s,%s,%s,%s,'gated',1,%s,%s,'sensitive','enroll',%s,%s) "
                    "ON CONFLICT (name) DO NOTHING RETURNING id",
                    (e["proposed_name"], e["proposed_name"], e["assigned_role"],
                     "Welcome, %s. You are enrolled with role %s." % (e["proposed_name"], e["assigned_role"]),
                     psycopg2.extras.Json({"groups": e["assigned_groups"]}), e["assigned_groups"],
                     th, token[:16]))
        arow = cur.fetchone()
        if not arow:
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="an agent named '%s' already exists; a manager must assign a different "
                                 "name before this enrollment can be provisioned" % e["proposed_name"]), 409
        aid = arow["id"]
        cur.execute("UPDATE enrollment SET status='provisioned', token_hash=%s, provisioned_agent_id=%s WHERE id=%s",
                    (th, aid, eid))
        log(cur, e["proposed_name"], "enroll_provision", "agent", str(aid), {"role": e["assigned_role"]})
        conn.commit(); cur.close(); conn.close()
        return jsonify(status="provisioned", token=token, role=e["assigned_role"],
                       groups=e["assigned_groups"], cert_cn=e["proposed_name"],
                       welcome="Welcome, %s. Store this token on your own disk (0600); it is shown ONCE. "
                               "Your mTLS cert (CN=%s) is issued separately by a manager; until then you can "
                               "only reach /enroll. Then call with mTLS + Bearer token." % (e["proposed_name"], e["proposed_name"]))
    cur.execute("SELECT approver FROM enrollment_approval WHERE enrollment_id=%s "
                "AND decision='approve'", (eid,))
    approvers = [r["approver"] for r in cur.fetchall()]
    resp = {"status": e["status"], "approve_count": len(approvers), "required": cfg("ENROLL_APPROVALS"),
            "approvers": approvers}
    cur.close(); conn.close()
    return jsonify(resp)


# ---------------------------------------------------------------------------
# Wave 3 — read surface for the dashboard (the readable window). All role-gated:
# manager/viewer see everything; worker/readonly are access-filtered. The only
# write here is the manager-only proposal decision.
# ---------------------------------------------------------------------------
def _iso(v):
    return v.isoformat() if v else None


@app.get("/memories")
def memories():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = mem_read_where(a)
    q = (request.args.get("q") or "").strip()
    limit = _clip_int(request.args.get("limit"), 100, 1, 500)      # clamp; bad input -> default
    offset = _clip_int(request.args.get("offset"), 0, 0, 10_000_000)
    extra, qp = "", []
    if q:
        extra = " AND (name ILIKE %s OR description ILIKE %s OR body ILIKE %s)"
        qp = ["%" + q + "%"] * 3
    cols = "name, description, mtype, mem_tier, sensitivity, updated_at"
    if request.args.get("full"):              # include body (dashboard graph build needs it inline)
        cols += ", body"
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT count(*) AS n FROM memory WHERE " + where + extra, params + qp)
    total = cur.fetchone()["n"]
    cur.execute("SELECT " + cols + " FROM memory WHERE "
                + where + extra + " ORDER BY updated_at DESC NULLS LAST, name LIMIT %s OFFSET %s",
                params + qp + [limit, offset])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["updated_at"] = _iso(r.get("updated_at"))
    cur.close(); conn.close()
    return jsonify(total=total, count=len(rows), offset=offset, memories=rows)


# ---- (Approval 2.0 step 5): faceted memory EXPLORER — the management surface that replaces bulk
# deletion. Manager/approver only + AUDITED, and it DELIBERATELY bypasses recall's author-only/share gate
# (like /curate + /personal/inspect) so a manager can govern EVERY agent's notes across every tier. Bodies
# are intentionally NOT returned in the bulk list (sensitive/secret bodies stay off the page); the drawer
# shows RELATIONS, not body. Facets: author (author_body), type (mtype), sensitivity, and a derived
# `validation` state {untrusted | author-trusted | shared-trusted}.
_VALIDATION_SQL = {
    # untrusted: not yet self-validated (quarantined). author-trusted: self-validated, still author-only.
    # shared-trusted: manager-validated / cross-agent (share_status trusted|shared).
    "untrusted": "trust='quarantined'",
    "author-trusted": "trust='trusted' AND share_status='personal'",
    "shared-trusted": "share_status IN ('trusted','shared')",
}


@app.get("/memories/explore")
def memories_explore():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = ["deleted_at IS NULL"]
    params = []
    author = (request.args.get("author") or "").strip()
    if author:
        where.append("author_body=%s"); params.append(author)
    mtype = (request.args.get("mtype") or "").strip()
    if mtype:
        where.append("mtype=%s"); params.append(mtype)
    sens = (request.args.get("sensitivity") or "").strip()
    if sens:
        where.append("sensitivity=%s"); params.append(sens)
    val = (request.args.get("validation") or "").strip()
    if val in _VALIDATION_SQL:
        where.append("(" + _VALIDATION_SQL[val] + ")")
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(name ILIKE %s OR description ILIKE %s OR body ILIKE %s)")
        params += ["%" + q + "%"] * 3
    limit = _clip_int(request.args.get("limit"), 200, 1, 1000)
    offset = _clip_int(request.args.get("offset"), 0, 0, 10_000_000)
    wsql = " AND ".join(where)
    cur.execute("SELECT count(*) AS n FROM memory WHERE " + wsql, params)
    total = cur.fetchone()["n"]
    cur.execute("SELECT id, name, description, author_body, mtype, mem_tier, sensitivity, trust, "
                "share_status, tags, created_at, updated_at FROM memory WHERE " + wsql +
                " ORDER BY updated_at DESC NULLS LAST, name LIMIT %s OFFSET %s", params + [limit, offset])
    rows = []
    for r in cur.fetchall():
        r = dict(r)
        r["id"] = str(r["id"]); r["created_at"] = _iso(r.get("created_at")); r["updated_at"] = _iso(r.get("updated_at"))
        # derive the validation label the UI badges on (single source = the same SQL buckets above)
        if r["trust"] == "quarantined":
            r["validation"] = "untrusted"
        elif r["share_status"] in ("trusted", "shared"):
            r["validation"] = "shared-trusted"
        else:
            r["validation"] = "author-trusted"
        rows.append(r)
    # distinct authors present (live) to populate the agent dropdown
    cur.execute("SELECT DISTINCT author_body FROM memory WHERE deleted_at IS NULL AND author_body IS NOT NULL ORDER BY author_body")
    authors = [r["author_body"] for r in cur.fetchall()]
    log(cur, a["name"], "memories_explore", "memory", None,
        {"author": author or None, "mtype": mtype or None, "sensitivity": sens or None,
         "validation": val or None, "q": q or None, "n": len(rows)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(total=total, count=len(rows), offset=offset, memories=rows, authors=authors)


@app.get("/memory/<mid>/relations")
def memory_relations(mid):
    """Both-direction edges for ONE memory (manager/approver only, AUDITED). Bypasses the read-gate like
    the explorer it feeds. Returns the other endpoint's name + rel_type + weight + direction (out=this->other,
    in=other->this) so the drawer can show HOW a note connects. Skips edges to soft-deleted notes."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name FROM memory WHERE id=%s AND deleted_at IS NULL", [mid])
    me = cur.fetchone()
    if not me:
        cur.close(); conn.close()
        return jsonify(error="not found"), 404
    cur.execute(
        "SELECT r.rel_type, r.weight, r.created_by, "
        "       CASE WHEN r.src_id=%s THEN 'out' ELSE 'in' END AS direction, "
        "       CASE WHEN r.src_id=%s THEN r.dst_id ELSE r.src_id END AS other_id, "
        "       o.name AS other_name "
        "FROM memory_relation r JOIN memory o "
        "  ON o.id = CASE WHEN r.src_id=%s THEN r.dst_id ELSE r.src_id END "
        "WHERE (r.src_id=%s OR r.dst_id=%s) AND o.deleted_at IS NULL "
        "ORDER BY direction, r.rel_type, o.name", [mid, mid, mid, mid, mid])
    rels = []
    for r in cur.fetchall():
        rels.append({"rel_type": r["rel_type"], "weight": r["weight"], "created_by": r.get("created_by"),
                     "direction": r["direction"], "other_id": str(r["other_id"]), "other_name": r["other_name"]})
    log(cur, a["name"], "memory_relations", "memory", str(mid), {"n": len(rels)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(id=str(mid), name=me["name"], count=len(rels), relations=rels)


@app.get("/memory/<mid>/provenance")
def memory_provenance_get(mid):
    """the raw session evidence a memory was distilled from — the cited spans (channel, ts,
    scrubbed text) recorded AT SYNTHESIS TIME by autolearn, each with its deterministic support
    score, plus an OVERALL audit verdict (how grounded the body is in ALL its cited evidence).
    Manager/approver only, AUDITED; like /relations it bypasses the read-gate it feeds. Returns an
    empty list for a memory with no recorded provenance (pre- rows, or a human-approved note
    with no extractor citations)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, body FROM memory WHERE id=%s AND deleted_at IS NULL", [mid])
    me = cur.fetchone()
    if not me:
        cur.close(); conn.close()
        return jsonify(error="not found"), 404
    cur.execute("SELECT session_id, span_idx, channel, span_ts, evidence_text, audit_support "
                "FROM memory_provenance WHERE memory_id=%s ORDER BY session_id, span_idx", [mid])
    rows = cur.fetchall()
    evidence = [{"session_id": r["session_id"], "span_idx": r["span_idx"], "channel": r["channel"],
                 "span_ts": _iso(r["span_ts"]), "evidence_text": r["evidence_text"],
                 "support": r["audit_support"]} for r in rows]
    overall = AL_prov.audit_support(me["body"], [r["evidence_text"] for r in rows]) if rows else None
    verdict = AL_prov.audit_verdict(overall) if rows else "none"
    log(cur, a["name"], "memory_provenance", "memory", str(mid), {"n": len(evidence), "audit": verdict})
    conn.commit(); cur.close(); conn.close()
    return jsonify(id=str(mid), name=me["name"], count=len(evidence),
                   audit_support=overall, audit_verdict=verdict, evidence=evidence)


@app.get("/memory/<name>")
def memory_one(name):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = mem_read_where(a)
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name, description, body, mtype, mem_tier, sensitivity, readers, trust, "
                "origin_channel, author_body, created_at, updated_at FROM memory WHERE name=%s AND "
                + where, [name] + params)
    r = cur.fetchone()
    cur.close(); conn.close()
    if not r:
        return jsonify(error="not found or not permitted"), 404
    r = dict(r)
    r["created_at"] = _iso(r.get("created_at")); r["updated_at"] = _iso(r.get("updated_at"))
    return jsonify(r)


@app.get("/graph")
def graph():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = mem_read_where(a)
    conn = db(); cur = conn.cursor()
    # expose the extra memory metadata the dashboard graph needs — source_session (to
    # synthesise session nodes + session->memory edges) and trust/tags/origin/validity/author
    # for the richer note drawer. All additive, read-only, still gated by mem_read_where.
    cur.execute("SELECT id, name, mtype, source_session, author_body, trust, tags, "
                "origin_channel, valid_at, invalid_at FROM memory WHERE " + where, params)
    nodes = [{"id": str(i), "name": n, "mtype": m, "source_session": ss, "author_body": ab,
              "trust": tr, "tags": tg or [], "origin_channel": oc,
              "valid_at": _iso(va), "invalid_at": _iso(ia)}
             for i, n, m, ss, ab, tr, tg, oc, va, ia in cur.fetchall()]
    allowed = {n["id"] for n in nodes}
    cur.execute("SELECT src_id, dst_id, rel_type FROM memory_relation")
    edges = [{"source": str(s), "target": str(d), "rel": rt}
             for s, d, rt in cur.fetchall() if str(s) in allowed and str(d) in allowed]
    cur.close(); conn.close()
    return jsonify(nodes=nodes, edges=edges)


@app.get("/tasks")
def tasks():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    # task is structure-kind: no deleted_at / invalid_at columns
    where, params = struct_read_where(a)
    extra, qp = "", []
    st = request.args.get("status")
    if st:
        extra += " AND status=%s"; qp.append(st)
    asg = request.args.get("assignee")
    if asg:
        extra += " AND assignee=%s"; qp.append(asg)
    proj = request.args.get("project")
    if proj:
        extra += " AND project_id=(SELECT id FROM project WHERE slug=%s)"; qp.append(proj)
    # the frontier — open/in-progress tasks with no STILL-OPEN blocker, i.e. what's takeable now.
    if request.args.get("frontier") in ("1", "true", "yes"):
        extra += (" AND status IN ('open','in-progress') AND NOT EXISTS ("
                  "SELECT 1 FROM task_dep d JOIN task bt ON bt.id=d.blocker_id "
                  "WHERE d.blocked_id=task.id AND bt.status <> 'done')")
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT handle, title, status, assignee, task_tier, lane, notes, acceptance, verify, plan_section, "
                "due_at, "   # the deadline, so a caller can see what's due without a second query
                # this task's blockers that aren't done yet — empty/absent means nothing gates it
                "(SELECT array_agg(bt.handle ORDER BY bt.handle) FROM task_dep d JOIN task bt ON bt.id=d.blocker_id "
                " WHERE d.blocked_id=task.id AND bt.status <> 'done') AS blocked_by, "
                "(SELECT slug FROM project WHERE id=task.project_id) AS project, updated_at "
                "FROM task WHERE " + where + extra + " ORDER BY handle", params + qp)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["updated_at"] = _iso(r.get("updated_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), tasks=rows)


# ---- task / project / idea WRITE + LIST endpoints ( cutover) ----
# The brain became the SINGLE canonical store for tasks/projects/ideas (the old the legacy host
# tasks.json + brain-task-sync.sh replica refresh is retired). Reads are status-filtered
# server-side so a caller fetches only the slice it asked for, never the whole board.
# Task writes: any non-readonly/viewer/approver role (workers may file tasks). Project/idea
# writes: manager only (managers shape the ideas->projects->tasks loop). Every write is logged.
_TASK_STATUS = ("open", "in-progress", "blocked", "done")
_PROJ_STATUS = ("active", "ongoing", "paused", "done", "archived")
_IDEA_STATUS = ("raw", "promoted", "dropped")


def _date_or_none(v):
    """normalise a deadline field so 'no date' round-trips as NULL.

    An empty string is how a caller CLEARS a deadline (the MCP layer sends "" for an unset optional
    arg), but '' fails a Postgres date/timestamptz cast — so it must become None, not reach the DB.
    NULL is the norm here, not a gap: most projects are open-ended."""
    if isinstance(v, str):
        v = v.strip()
    return v or None


def _handles_to_ids(cur, handles):
    """resolve task handles (['',''] or a comma-string) to their uuids for task_dep.

    Raises ValueError on an unknown handle so a typo fails LOUDLY rather than silently dropping a
    dependency edge — a missing blocker would leave a task wrongly on the frontier."""
    if isinstance(handles, str):
        handles = [h for h in re.split(r"[,\s]+", handles) if h]
    handles = [h.strip() for h in (handles or []) if h and str(h).strip()]
    if not handles:
        return []
    cur.execute("SELECT handle, id FROM task WHERE handle = ANY(%s)", (handles,))
    # task handlers use a RealDictCursor, so rows are dicts, not tuples — key by column name.
    found = {r["handle"]: r["id"] for r in cur.fetchall()}
    missing = [h for h in handles if h not in found]
    if missing:
        raise ValueError("unknown task handle(s): %s" % ", ".join(missing))
    # preserve caller order, de-dup
    seen, out = set(), []
    for h in handles:
        if h not in seen:
            seen.add(h); out.append(found[h])
    return out


def _can_write_task(a):
    return a["role"] not in ("readonly", "viewer", "approver")


def _default_share(a):
    """New content defaults: a manager writes TRUSTED (they own the shared roadmap / validate
    directly); anyone else writes PERSONAL (author-only until they promote it) —/."""
    return "trusted" if a["role"] == "manager" else "personal"


def struct_read_where(agent, created_col="created_by"):
    """Structure-table (task/project/idea) visibility = access_where + the share tier:
    TRUSTED rows (readers/sensitivity still gate; managers see all) PLUS the caller's OWN
    personal rows. READY_TO_SHARE is hidden from normal listings (it lives in /structure/pending
    for managers). Mirrors mem_read_where for the structure kind (which has no soft-delete cols)."""
    where, params = access_where(agent, soft_delete=False, temporal=False)
    where += " AND (share_status='trusted' OR (share_status='personal' AND " + created_col + "=%s))"
    return where, params + [agent["name"]]


# generic structure map for the share/review/decide path: kind -> (table, key_column)
_STRUCT = {"task": ("task", "handle"), "project": ("project", "slug"), "idea": ("idea", "id")}


def _resolve_project(cur, slug, actor, role="manager"):
    """Get a project id by slug. only a MANAGER may auto-create a stub (the shared board is
    theirs); for anyone else an unknown slug raises ValueError so a typo can't silently spawn a
    permanent trusted project. Empty slug -> None (unchanged)."""
    slug = (slug or "").strip()
    if not slug:
        return None
    cur.execute("SELECT id FROM project WHERE slug=%s", (slug,))
    r = cur.fetchone()
    if r:
        return r["id"]
    if role != "manager":
        raise ValueError("unknown project '%s'" % slug)
    cur.execute("INSERT INTO project(slug,title,status,share_status,created_by) "
                "VALUES (%s,%s,'active','trusted',%s) ON CONFLICT (slug) DO NOTHING", (slug, slug, actor))
    cur.execute("SELECT id FROM project WHERE slug=%s", (slug,))
    r = cur.fetchone()
    return r["id"] if r else None


@app.post("/tasks")
def task_add():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if not _can_write_task(a):
        return jsonify(error="role may not write tasks"), 403
    b = request.get_json(silent=True) or {}
    title = (b.get("title") or "").strip()
    if not title:
        return jsonify(error="title required"), 400
    status = b.get("status", "open")
    if status not in _TASK_STATUS:
        return jsonify(error="status must be one of %s" % (_TASK_STATUS,)), 400
    # reader-groups (worker visibility): a worker sees a task only if its readers
    # overlap the worker's groups; managers see all by role. Default: manager-created => []
    # (manager-only, unchanged); worker-created => the worker's own non-'common' groups (so the
    # creator and managers see it, but it's not broadcast to every common-group holder).
    readers = b.get("readers")
    if readers is None:
        if a["role"] == "manager":
            readers = []
        else:
            readers = [g for g in (a.get("access_scope") or {}).get("groups", []) if g != "common"] or ["common"]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        pid = _resolve_project(cur, b.get("project"), a["name"], a["role"])
    except ValueError as e:
        cur.close(); conn.close()
        return jsonify(error="%s — a manager must create it first" % e), 400
    # next handle = max numeric T-number + 1 (brain owns allocation). advisory lock serializes
    # allocation so two concurrent creates can't pick the same T-number and 500 on the unique index.
    cur.execute("SELECT pg_advisory_xact_lock(8123)")
    cur.execute("SELECT handle FROM task WHERE handle ~ '^T[0-9]+$'")
    nums = [int(r["handle"][1:]) for r in cur.fetchall()]
    handle = "T%d" % ((max(nums) + 1) if nums else 1)
    # tasks default TRUSTED (the shared work board is unchanged); an author may opt a task personal
    share = b.get("share_status") if b.get("share_status") in ("personal", "trusted") else "trusted"
    cur.execute("INSERT INTO task(handle,title,status,project_id,assignee,task_tier,lane,notes,acceptance,verify,readers,created_by,share_status,plan_section,due_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING handle,title,status,assignee,task_tier,lane,due_at",
                (handle, title, status, pid, b.get("assignee", "manager"), b.get("tier", 3),
                 b.get("lane", "gated"), b.get("notes"), b.get("acceptance"), b.get("verify"), readers, a["name"], share,
                 b.get("plan_section"),   # soft pointer to the project_doc section this task builds/changes (migration 0033)
                 _date_or_none(b.get("due_at"))))  # deadline; NULL when unset, which is the norm
    t = cur.fetchone()
    if b.get("blocked_by"):   # record the tasks this one waits on (unknown handle -> 400, whole create rolls back)
        try:
            blocker_ids = _handles_to_ids(cur, b["blocked_by"])
        except ValueError as e:
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error=str(e)), 400
        for bid in blocker_ids:
            cur.execute("INSERT INTO task_dep(blocked_id, blocker_id, created_by) "
                        "SELECT id, %s, %s FROM task WHERE handle=%s ON CONFLICT DO NOTHING",
                        (bid, a["name"], handle))
    detail = {"title": title, "project": b.get("project"), "status": status}
    if b.get("on_behalf"):   # shared-identity caller (e.g. a sub-agent reusing a worker's token) names the real actor
        detail["on_behalf"] = b["on_behalf"]
    log(cur, a["name"], "task_add", "task", handle, detail)
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, handle=handle, task=dict(t))


@app.patch("/tasks/<handle>")
def task_update(handle):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if not _can_write_task(a):
        return jsonify(error="role may not write tasks"), 403
    b = request.get_json(silent=True) or {}
    # retagging a task's visibility (readers) or clearance tier is a manager-only action —
    # otherwise a worker could widen `readers` to expose a hidden task to itself, or lower a task's
    # tier below its own clearance.
    if ("readers" in b or "tier" in b) and a["role"] != "manager":
        return jsonify(error="only a manager may change task readers/tier"), 403
    sets, params = [], []
    if "status" in b:
        if b["status"] not in _TASK_STATUS:
            return jsonify(error="status must be one of %s" % (_TASK_STATUS,)), 400
        sets.append("status=%s"); params.append(b["status"])
    for col in ("title", "assignee", "lane", "notes", "acceptance", "verify", "plan_section"):  # plan_section links a task to its project_doc section
        if col in b:
            sets.append(col + "=%s"); params.append(b[col])
    if "due_at" in b:   # kept out of the loop above because '' must CLEAR the deadline, not fail a timestamptz cast
        sets.append("due_at=%s"); params.append(_date_or_none(b["due_at"]))
    if "tier" in b:
        sets.append("task_tier=%s"); params.append(b["tier"])
    if "readers" in b:   # retag worker visibility (reader-groups)
        sets.append("readers=%s"); params.append(b["readers"])
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if "project" in b:
        try:
            _pid = _resolve_project(cur, b.get("project"), a["name"], a["role"])
        except ValueError as e:
            cur.close(); conn.close()
            return jsonify(error="%s — a manager must create it first" % e), 400
        sets.append("project_id=%s"); params.append(_pid)
    # dependency-edge changes count as a real update even with no column change — resolve the
    # handles up front so a typo 400s before we touch anything.
    edge_change = ("blocked_by" in b) or ("unblock" in b)
    try:
        add_ids = _handles_to_ids(cur, b.get("blocked_by")) if "blocked_by" in b else []
        del_ids = _handles_to_ids(cur, b.get("unblock")) if "unblock" in b else []
    except ValueError as e:
        cur.close(); conn.close()
        return jsonify(error=str(e)), 400
    if not sets and not edge_change:
        cur.close(); conn.close()
        return jsonify(error="nothing to update"), 400
    # the caller may only modify a task it can actually SEE (same struct_read_where visibility as
    # reads: trusted rows within its reader-groups + its own personal rows) AND whose clearance tier is
    # within its own (task_tier <= agent_tier). Enforced inside the UPDATE's WHERE so it's race-safe —
    # without it, any non-readonly writer could PATCH any task by guessing its handle (T1..Tn).
    vwhere, vparams = struct_read_where(a)
    sets.append("updated_at=now()"); params.append(handle)
    cur.execute("UPDATE task SET " + ", ".join(sets) +
                " WHERE handle=%s AND COALESCE(task_tier,0) <= %s AND " + vwhere +
                " RETURNING handle,title,status,assignee,task_tier,lane",
                params + [a.get("agent_tier") or 1] + vparams)
    t = cur.fetchone()
    if not t:
        # distinguish "not permitted" (row exists but out of the caller's scope/tier) from "missing"
        cur.execute("SELECT 1 FROM task WHERE handle=%s", (handle,))
        exists = cur.fetchone() is not None
        conn.rollback(); cur.close(); conn.close()
        if exists:
            return jsonify(error="not permitted to modify task %s" % handle), 403
        return jsonify(error="no such task: %s" % handle), 404
    # apply dependency edges now that permission is confirmed. Add skips a self-edge (t.id<>bid)
    # so "block T on itself" is a quiet no-op rather than a CHECK-violation 500.
    for bid in add_ids:
        cur.execute("INSERT INTO task_dep(blocked_id, blocker_id, created_by) "
                    "SELECT t.id, %s, %s FROM task t WHERE t.handle=%s AND t.id <> %s "
                    "ON CONFLICT DO NOTHING", (bid, a["name"], handle, bid))
    for bid in del_ids:
        cur.execute("DELETE FROM task_dep WHERE blocked_id=(SELECT id FROM task WHERE handle=%s) "
                    "AND blocker_id=%s", (handle, bid))
    log(cur, a["name"], "task_update", "task", handle, {k: b[k] for k in b})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, task=dict(t))


@app.get("/projects")
def projects():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = struct_read_where(a)
    #/A3-7: the open_tasks sub-count must honour the caller's task visibility too — a raw
    # COUNT(*) over every non-done task leaks how many tasks a caller isn't allowed to see. Reuse
    # the same struct fragment against the task rows; unqualified cols bind to task (inner scope).
    # Its params LEAD because the sub-count sits in the SELECT list, ahead of the WHERE clause.
    twhere, tparams = struct_read_where(a)
    extra, qp = "", []
    st = request.args.get("status")
    if st:
        extra += " AND status=%s"; qp.append(st)
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT slug, title, status, description, updated_at, "
                "(SELECT count(*) FROM task WHERE task.project_id=project.id "
                "AND task.status<>'done' AND " + twhere + ") AS open_tasks "
                "FROM project WHERE " + where + extra + " ORDER BY slug", tparams + params + qp)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["updated_at"] = _iso(r.get("updated_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), projects=rows)


@app.post("/projects")
def project_add():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if not _can_write_task(a):   # workers/crew may create too (as personal); not readonly/viewer/approver
        return jsonify(error="role may not create projects"), 403
    b = request.get_json(silent=True) or {}
    slug = (b.get("slug") or "").strip()
    if not slug:
        return jsonify(error="slug required"), 400
    status = b.get("status", "active")
    if status not in _PROJ_STATUS:
        return jsonify(error="status must be one of %s" % (_PROJ_STATUS,)), 400
    #/A3-2: default readers like task_add — else a worker's PERSONAL project has readers={} and
    # access_where's `readers && groups` hides it from its OWN author. manager => [] (sees by role).
    readers = b.get("readers")
    if readers is None:
        readers = [] if a["role"] == "manager" else (
            [g for g in (a.get("access_scope") or {}).get("groups", []) if g != "common"] or ["common"])
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if a["role"] == "manager":
        # managers own the shared roadmap: upsert directly to a TRUSTED project
        cur.execute("INSERT INTO project(slug,title,status,description,created_by,share_status,readers,target_date) VALUES (%s,%s,%s,%s,%s,'trusted',%s,%s) "
                    "ON CONFLICT (slug) DO UPDATE SET title=EXCLUDED.title, status=EXCLUDED.status, "
                    "description=COALESCE(EXCLUDED.description, project.description), "
                    # COALESCE like description — an upsert that omits target_date must not wipe an existing one
                    "target_date=COALESCE(EXCLUDED.target_date, project.target_date), updated_at=now() "
                    "RETURNING slug,title,status,target_date",
                    (slug, b.get("title", slug), status, b.get("description"), a["name"], readers,
                     _date_or_none(b.get("target_date"))))
        p = cur.fetchone()
    else:
        # a worker creates a PERSONAL project; it must NOT overwrite an existing (trusted) slug
        cur.execute("INSERT INTO project(slug,title,status,description,created_by,share_status,readers,target_date) VALUES (%s,%s,%s,%s,%s,'personal',%s,%s) "
                    "ON CONFLICT (slug) DO NOTHING RETURNING slug,title,status,target_date",
                    (slug, b.get("title", slug), status, b.get("description"), a["name"], readers,
                     _date_or_none(b.get("target_date"))))
        p = cur.fetchone()
        if not p:
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="a project with that slug already exists (pick another; you can't overwrite it)"), 409
    # auto-seed the 4-section starter so every project always HAS a living plan.
    # Idempotent (ON CONFLICT DO NOTHING) — never clobbers existing sections, safe to re-run/backfill.
    if p:
        _STARTER_DOC = [
            ("overview",     "Overview",     "overview",  "_Purpose + done criteria — fill me in._"),
            ("out-of-scope", "Out of scope", "note",      "_What this project will NOT do, and why._"),
            ("invariants",   "Invariants",   "invariant", "_Unchanging constraints/assumptions. Add a ```check block to make one drift-checkable (drift_check.py)._"),
            ("resume-here",  "Resume here",  "note",      "_Current state + the exact next step. Keep current for cold resume._"),
        ]
        cur.executemany(
            "INSERT INTO project_doc(project_id,section_key,title,kind,body,position,created_by) "
            "VALUES ((SELECT id FROM project WHERE slug=%s),%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (project_id,section_key) DO NOTHING",
            [(slug, sk, ttl, knd, bod, pos, a["name"]) for pos, (sk, ttl, knd, bod) in enumerate(_STARTER_DOC)])
    log(cur, a["name"], "project_upsert", "project", slug,
        {"status": status, "share_status": "trusted" if a["role"] == "manager" else "personal"})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, project=dict(p))


@app.patch("/projects/<slug>")
def project_update(slug):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    b = request.get_json(silent=True) or {}
    sets, params = [], []
    if "status" in b:
        if b["status"] not in _PROJ_STATUS:
            return jsonify(error="status must be one of %s" % (_PROJ_STATUS,)), 400
        sets.append("status=%s"); params.append(b["status"])
    for col in ("title", "description"):
        if col in b:
            sets.append(col + "=%s"); params.append(b[col])
    if "target_date" in b:   # separate from the loop so '' CLEARS the date instead of failing a date cast
        sets.append("target_date=%s"); params.append(_date_or_none(b["target_date"]))
    if not sets:
        return jsonify(error="nothing to update"), 400
    sets.append("updated_at=now()"); params.append(slug)
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE project SET " + ", ".join(sets) + " WHERE slug=%s RETURNING slug,title,status,target_date", params)
    p = cur.fetchone()
    if not p:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="no such project: %s" % slug), 404
    log(cur, a["name"], "project_update", "project", slug, {k: b[k] for k in b})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, project=dict(p))


# ---- project_doc: the per-project living PLAN ----
# The "project plan" is the cumulative design/flow document for a project — distinct from a task's
# throwaway execution plan. SECTIONED: one row per section, addressed by (project_id, section_key),
# so an edit touches ONE row (no whole-doc reload). Reads follow struct_read_where visibility;
# writes are manager-only (the plan is the shared design, like project_update). invariant-kind
# sections are the machine-readable source for the drift-check.
_DOC_KINDS = ("overview", "flow", "feature", "invariant", "note")


@app.get("/projects/<slug>/doc")
def project_doc_get(slug):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM project WHERE slug=%s", (slug,))
    p = cur.fetchone()
    if not p:
        cur.close(); conn.close()
        return jsonify(error="no such project: %s" % slug), 404
    where, params = struct_read_where(a)
    cur.execute("SELECT section_key, title, kind, body, position, updated_at "
                "FROM project_doc WHERE project_id=%s AND " + where +
                " ORDER BY position, section_key", [p["id"]] + params)
    sections = [dict(r) for r in cur.fetchall()]
    for s in sections:
        s["updated_at"] = _iso(s.get("updated_at"))
    cur.close(); conn.close()
    # rendered = one markdown doc stitched from the sections, in order (cheap one-load view)
    rendered = "\n\n".join(("## %s\n\n%s" % (s["title"] or s["section_key"], s["body"] or "")).rstrip()
                           for s in sections)
    return jsonify(project=slug, count=len(sections), sections=sections, rendered=rendered)


@app.post("/projects/<slug>/doc")
def project_doc_set(slug):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    b = request.get_json(silent=True) or {}
    section_key = (b.get("section_key") or "").strip()
    if not section_key:
        return jsonify(error="section_key required"), 400
    kind = b.get("kind")   # None => keep existing on update / default 'flow' on insert
    if kind is not None and kind not in _DOC_KINDS:
        return jsonify(error="kind must be one of %s" % (_DOC_KINDS,)), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM project WHERE slug=%s", (slug,))
    p = cur.fetchone()
    if not p:
        cur.close(); conn.close()
        return jsonify(error="no such project: %s" % slug), 404
    # Upsert ONE section. Omitted fields are preserved on update (COALESCE keeps the existing value),
    # so a caller can change just the body without resetting title/kind/position.
    pos = b.get("position")
    cur.execute(
        "INSERT INTO project_doc(project_id,section_key,title,kind,body,position,created_by) "
        "VALUES (%s,%s,%s,COALESCE(%s,'flow'),%s,COALESCE(%s,0),%s) "
        "ON CONFLICT (project_id,section_key) DO UPDATE SET "
        "  title=COALESCE(EXCLUDED.title, project_doc.title), "
        "  kind=COALESCE(%s, project_doc.kind), "
        "  body=COALESCE(EXCLUDED.body, project_doc.body), "
        "  position=COALESCE(%s, project_doc.position), "
        "  updated_at=now() "
        "RETURNING section_key,title,kind,body,position",
        (p["id"], section_key, b.get("title"), kind, b.get("body"), pos, a["name"], kind, pos))
    s = cur.fetchone()
    if not s:   # RLS write-check blocked it (shouldn't happen for a manager) — surface, don't 500
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="write not permitted"), 403
    log(cur, a["name"], "project_doc_set", "project_doc", "%s/%s" % (slug, section_key),
        {"kind": s["kind"], "position": s["position"]})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, project=slug, section=dict(s))


@app.delete("/projects/<slug>/doc/<section_key>")
def project_doc_del(slug, section_key):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("DELETE FROM project_doc WHERE section_key=%s "
                "AND project_id=(SELECT id FROM project WHERE slug=%s) RETURNING section_key",
                (section_key, slug))
    s = cur.fetchone()
    if not s:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="no such section: %s/%s" % (slug, section_key)), 404
    log(cur, a["name"], "project_doc_del", "project_doc", "%s/%s" % (slug, section_key), {})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True)


@app.get("/ideas")
def ideas():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = struct_read_where(a)
    extra, qp = "", []
    st = request.args.get("status")
    if st:
        extra += " AND status=%s"; qp.append(st)
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, body, status, created_at, updated_at, "
                "(SELECT slug FROM project WHERE id=idea.promoted_project_id) AS promoted_project "
                "FROM idea WHERE " + where + extra + " ORDER BY created_at", params + qp)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"]); r["created_at"] = _iso(r.get("created_at")); r["updated_at"] = _iso(r.get("updated_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), ideas=rows)


@app.post("/ideas")
def idea_add():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if not _can_write_task(a):   # workers/crew may capture ideas too (as personal)
        return jsonify(error="role may not create ideas"), 403
    b = request.get_json(silent=True) or {}
    body = (b.get("body") or "").strip()
    if not body:
        return jsonify(error="body required"), 400
    status = b.get("status", "raw")
    if status not in _IDEA_STATUS:
        return jsonify(error="status must be one of %s" % (_IDEA_STATUS,)), 400
    share = _default_share(a)   # manager -> trusted; worker/crew -> personal (promote later)
    #/A3-2: default readers like task_add so a worker's PERSONAL idea isn't hidden from its author.
    readers = b.get("readers")
    if readers is None:
        readers = [] if a["role"] == "manager" else (
            [g for g in (a.get("access_scope") or {}).get("groups", []) if g != "common"] or ["common"])
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("INSERT INTO idea(body,status,created_by,share_status,readers) VALUES (%s,%s,%s,%s,%s) RETURNING id,body,status",
                (body, status, a["name"], share, readers))
    i = cur.fetchone(); i["id"] = str(i["id"])
    log(cur, a["name"], "idea_add", "idea", i["id"], {"status": status, "share_status": share})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, idea=dict(i))


@app.patch("/ideas/<iid>")
def idea_update(iid):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    b = request.get_json(silent=True) or {}
    sets, params = [], []
    if "status" in b:
        if b["status"] not in _IDEA_STATUS:
            return jsonify(error="status must be one of %s" % (_IDEA_STATUS,)), 400
        sets.append("status=%s"); params.append(b["status"])
    if "body" in b:
        sets.append("body=%s"); params.append(b["body"])
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if "promote_to" in b:   # slug of the project this idea became
        slug = (b["promote_to"] or "").strip()
        if slug:
            cur.execute("SELECT id FROM project WHERE slug=%s", (slug,))
            r = cur.fetchone()
            if not r:
                conn.rollback(); cur.close(); conn.close()
                return jsonify(error="no such project to promote to: %s" % slug), 400
            sets.append("promoted_project_id=%s"); params.append(r["id"])
            if "status" not in b:
                sets.append("status=%s"); params.append("promoted")
    if not sets:
        cur.close(); conn.close()
        return jsonify(error="nothing to update"), 400
    sets.append("updated_at=now()"); params.append(iid)
    cur.execute("UPDATE idea SET " + ", ".join(sets) + " WHERE id=%s RETURNING id,body,status", params)
    i = cur.fetchone()
    if not i:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="no such idea: %s" % iid), 404
    i["id"] = str(i["id"])
    log(cur, a["name"], "idea_update", "idea", iid, {k: b[k] for k in b})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, idea=dict(i))


# ---- structure share lifecycle: promote a personal task/project/idea -> review -> trust/delete ----

@app.post("/structure/<kind>/<key>/share")
def structure_share(kind, key):
    """Author promotes ONE of their OWN personal task/project/idea to 'ready_to_share' — it enters
    the manager review queue (/structure/pending) and drops out of normal listings until decided.
    Ownership enforced by the created_by guard."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if kind not in _STRUCT:
        return jsonify(error="kind must be task|project|idea"), 400
    tbl, keycol = _STRUCT[kind]
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE " + tbl + " SET share_status='ready_to_share', updated_at=now() "
                "WHERE " + keycol + "=%s AND created_by=%s AND share_status='personal' RETURNING " + keycol,
                (key, a["name"]))
    row = cur.fetchone()
    if not row:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="not found, not yours, or not a personal %s" % kind), 404
    log(cur, a["name"], "structure_share", kind, str(key), {"to": "ready_to_share"})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, kind=kind, key=key, share_status="ready_to_share")


@app.get("/structure/pending")
def structure_pending():
    """Manager review surface: all ready_to_share task/project/idea awaiting trust or delete."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    out = {}
    cur.execute("SELECT handle AS key, title, status, created_by FROM task WHERE share_status='ready_to_share' ORDER BY handle")
    out["task"] = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT slug AS key, title, status, created_by FROM project WHERE share_status='ready_to_share' ORDER BY slug")
    out["project"] = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id::text AS key, body, status, created_by FROM idea WHERE share_status='ready_to_share' ORDER BY created_at")
    out["idea"] = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(count=sum(len(v) for v in out.values()), pending=out)


@app.post("/structure/<kind>/<key>/decide")
def structure_decide(kind, key):
    """Manager: TRUST a ready_to_share structure item (-> shared) or DELETE it. author != validator —
    a manager may not trust an item it created itself (that goes to the other manager / the operator)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required to decide"), 403
    if kind not in _STRUCT:
        return jsonify(error="kind must be task|project|idea"), 400
    decision = (request.get_json(silent=True) or {}).get("decision")
    if decision not in ("trust", "delete"):
        return jsonify(error="decision must be 'trust' or 'delete'"), 400
    tbl, keycol = _STRUCT[kind]
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT created_by FROM " + tbl + " WHERE " + keycol + "=%s AND share_status='ready_to_share'", (key,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        return jsonify(error="not found or not awaiting review"), 404
    if decision == "trust" and r[0] == a["name"] and a["role"] != "manager":  #: managers (managers) self-approve
        cur.close(); conn.close()
        return jsonify(error="cannot trust a %s you created yourself (author != validator)" % kind), 403
    if decision == "trust":
        cur.execute("UPDATE " + tbl + " SET share_status='trusted', updated_at=now() WHERE " + keycol + "=%s", (key,))
    else:
        if kind == "project":                            #/A3-8: refuse delete if tasks still map here
            cur.execute("SELECT count(*) FROM task WHERE project_id=(SELECT id FROM project WHERE slug=%s)", (key,))
            _nt = cur.fetchone()[0]
            if _nt:
                cur.close(); conn.close()
                return jsonify(error="project '%s' still has %d task(s) — reassign or delete them first" % (key, _nt)), 409
        cur.execute("DELETE FROM " + tbl + " WHERE " + keycol + "=%s AND share_status='ready_to_share'", (key,))
    log(cur, a["name"], "structure_decide", kind, str(key), {"decision": decision})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, kind=kind, key=key, decision=decision)


_MSG_KINDS = ("msg", "alert", "task-handoff", "question")


# ---- agent inbox / chat: brain-native messaging, replaces the vault webhook bus ----
@app.post("/messages")
def message_send():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    b = request.get_json(silent=True) or {}
    to = (b.get("to") or "").strip()
    text = (b.get("body") or "").strip()
    if not to or not text:
        return jsonify(error="'to' and 'body' required"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT 1 FROM agent WHERE name=%s AND revoked_at IS NULL", (to,))
    if not cur.fetchone():
        cur.close(); conn.close()
        return jsonify(error="no such (active) agent: %s" % to), 400
    #/A2-15: whitelist kind; reserve 'alert' for managers (system alert paths + the 0016 trigger
    # rely on kind='alert' semantics — a worker shouldn't be able to mint one).
    _kind = b.get("kind", "msg")
    if _kind not in _MSG_KINDS:
        _kind = "msg"
    if _kind == "alert" and a["role"] != "manager":
        _kind = "msg"
    # no impersonation: from_agent is the authenticated caller, never client-supplied
    cur.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (a["name"], to, b.get("subject"), text, _kind, a["name"]))
    mid = cur.fetchone()["id"]
    _md = {"to": to, "subject": b.get("subject")}
    if b.get("on_behalf"):
        _md["on_behalf"] = b["on_behalf"]
    log(cur, a["name"], "message_send", "message", str(mid), _md)
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, message_id=str(mid))


@app.get("/inbox")
def inbox():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    box = request.args.get("box", "in")
    unread = request.args.get("unread") in ("1", "true", "yes")
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cols = "id,from_agent,to_agent,subject,body,kind,read_at,created_at"
    if box == "sent":   # what I sent
        cur.execute("SELECT " + cols + " FROM message WHERE from_agent=%s ORDER BY created_at DESC LIMIT 100",
                    (a["name"],))
    else:               # my inbox
        q = "SELECT " + cols + " FROM message WHERE to_agent=%s"
        if unread:
            q += " AND read_at IS NULL"
        q += " ORDER BY created_at DESC LIMIT 100"
        cur.execute(q, (a["name"],))
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"]); r["read_at"] = _iso(r.get("read_at")); r["created_at"] = _iso(r.get("created_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), messages=rows)


@app.post("/messages/<mid>/read")
def message_read(mid):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # only the addressee may mark its own message read
    cur.execute("UPDATE message SET read_at=now(), updated_at=now() "
                "WHERE id=%s AND to_agent=%s AND read_at IS NULL RETURNING id", (mid, a["name"]))
    r = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, marked=bool(r))


# ---- (Approval 2.0 step 4): the "needs-you" queue = ONLY what genuinely needs a manager/the operator ----
# Two sources, both manager-owned, DISTINCT from the agents' own untrusted personal notes (which self-
# validate on recall and are NOT the operator's to clear):
#   * share_requests — open kind='share_request' inbox messages (an agent re-captured a fact another
#     agent already owns; a manager confirms same-fact + shares). Deduped by subject across manager copies.
#   * needs_human    — live memories tagged 'needs-human-vouch' (a manager couldn't confirm a note and
#     escalated it to the human). Actioned via the existing POST /memory/<mid>/validate (basis=manager-vouch).
@app.get("/needs-you")
def needs_you():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="needs-you is a manager queue"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # share_requests: open (unread) share_request messages, one row per distinct subject (the per-manager copies
    # of the same request collapse). DISTINCT ON keeps the earliest per subject; object_id from the matching
    # action_log row links the matched memory when present.
    cur.execute(
        "SELECT DISTINCT ON (subject) id, subject, body, from_agent, to_agent, created_at "
        "FROM message WHERE kind='share_request' AND read_at IS NULL "
        "ORDER BY subject, created_at ASC")
    sr = []
    for r in cur.fetchall():
        sr.append({"id": str(r["id"]), "subject": r["subject"], "body": r["body"],
                   "from_agent": r["from_agent"], "to_agent": r["to_agent"],
                   "created_at": _iso(r.get("created_at"))})
    # needs_human: live memories a manager escalated to the operator.
    cur.execute(
        "SELECT id, name, description, author_body, sensitivity, source_session, created_at "
        "FROM memory WHERE 'needs-human-vouch'=ANY(tags) AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT 200")
    nh = []
    for r in cur.fetchall():
        nh.append({"id": str(r["id"]), "name": r["name"], "description": r.get("description") or "",
                   "author_body": r["author_body"], "sensitivity": r.get("sensitivity") or "normal",
                   "source_session": r.get("source_session"), "created_at": _iso(r.get("created_at"))})
    cur.close(); conn.close()
    return jsonify(share_requests=sr, needs_human=nh,
                   counts={"share_requests": len(sr), "needs_human": len(nh)})


@app.post("/needs-you/dismiss")
def needs_you_dismiss():
    """Mark a share_request handled: reads ALL share_request messages sharing this subject (the per-manager
    copies), so it clears from every manager's queue at once. Manager-only."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="needs-you is a manager queue"), 403
    b = request.get_json(silent=True) or {}
    subject = (b.get("subject") or "").strip()
    if not subject:
        return jsonify(error="subject required"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE message SET read_at=now(), updated_at=now() "
                "WHERE kind='share_request' AND subject=%s AND read_at IS NULL RETURNING id", (subject,))
    n = cur.rowcount or 0
    log(cur, a["name"], "needs_you_dismiss", "message", None, {"subject": subject, "cleared": n})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, cleared=n)


@app.get("/proposals")
def proposals():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "viewer", "approver"):
        return jsonify(error="manager/viewer/approver role required"), 403
    status = request.args.get("status", "pending")
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT count(*) AS c FROM proposal WHERE deleted_at IS NULL AND (%s='' OR status=%s)",
                [status, status])
    total = cur.fetchone()["c"]                          #/B2-13: true total so a >500 silting episode
    cur.execute("SELECT id,name,mtype,proposed_body,description,status,origin_channel,trust,author_body,"  # isn't invisibly truncated
                "target_memory_id,created_at,decided_by,decided_at,reason FROM proposal "
                "WHERE deleted_at IS NULL AND (%s='' OR status=%s) ORDER BY created_at DESC LIMIT 500",
                [status, status])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"])
        r["target_memory_id"] = str(r["target_memory_id"]) if r.get("target_memory_id") else None
        r["created_at"] = _iso(r.get("created_at")); r["decided_at"] = _iso(r.get("decided_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), total=total, truncated=(total > len(rows)), proposals=rows)


@app.get("/timeline")
def timeline():
    """chronological memory-activity feed for the dashboard Timeline page. Returns recent LIVE
    memories PLUS pending proposals, newest-first, each with an add-date + a status chip
    (trusted/personal/pending). Read-only; manager/viewer/approver. access_where keeps a worker from
    seeing rows it couldn't recall; the dashboard identity is an approver so it sees the whole brain."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "viewer", "approver"):
        return jsonify(error="manager/viewer/approver role required"), 403
    try:
        limit = min(int(request.args.get("limit", 200)), 500)
    except Exception:
        limit = 200
    try:
        offset = max(int(request.args.get("offset", 0)), 0)      # page back into older memory
    except Exception:
        offset = 0
    order = "ASC" if (request.args.get("order", "desc").lower() == "asc") else "DESC"   # sort toggle
    author = (request.args.get("author") or "").strip()   # memories-per-agent filter
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where, params = access_where(a)                       # live rows, role-scoped
    awhere = where + (" AND author_body=%s" if author else "")   # optional author scope
    aparams = params + ([author] if author else [])
    cur.execute("SELECT count(*) AS n FROM memory WHERE " + awhere, aparams)   # total for the pager
    total = cur.fetchone()["n"]
    # the distinct author list (unscoped) powers the per-agent dropdown
    cur.execute("SELECT DISTINCT author_body FROM memory WHERE " + where + " AND author_body IS NOT NULL ORDER BY 1", params)
    authors = [r["author_body"] for r in cur.fetchall()]
    cur.execute("SELECT name, description, mtype, author_body, created_at, share_status "
                "FROM memory WHERE " + awhere + " ORDER BY created_at " + order + " LIMIT %s OFFSET %s",
                aparams + [limit, offset])
    items = [{"kind": "memory", "name": r["name"], "description": r["description"], "mtype": r["mtype"],
              "author": r["author_body"], "date": _iso(r["created_at"]),
              "status": (r["share_status"] or "trusted")} for r in cur.fetchall()]
    # the "awaiting" end of the feed: proposals still pending review — pinned to page 1 of the
    # newest-first view only, and only when not scoped to one agent.
    if offset == 0 and order == "DESC" and not author:
        cur.execute("SELECT name, description, mtype, author_body, created_at FROM proposal "
                    "WHERE deleted_at IS NULL AND status='pending' ORDER BY created_at DESC LIMIT %s", [limit])
        pend = [{"kind": "proposal", "name": r["name"], "description": r["description"], "mtype": r["mtype"],
                 "author": r["author_body"], "date": _iso(r["created_at"]), "status": "pending"}
                for r in cur.fetchall()]
        items = pend + items
        items.sort(key=lambda x: x["date"] or "", reverse=True)
    cur.close(); conn.close()
    return jsonify(count=len(items), total=total, offset=offset, limit=limit, order=order.lower(),
                   author=author or None, authors=authors, items=items)


# the /agent-config GET+POST endpoints were REMOVED. They existed solely to read/flip
# the per-agent `autoapprove_own` flag, which autolearn v2 made DEAD — clean captures now land
# PERSONAL generically (see the comment at the gate ~L1021: "personal landing is generic"), so the flag
# no longer affects any behaviour. The `agent.autoapprove_own` COLUMN is intentionally RETAINED (inert):
# dropping a live column is a schema change with no upside (option A, the operator. The dashboard
# Config-tab control that called these endpoints is removed in the dashboard rebuild.


# the relation-classifier knobs live in graph.yaml (not the config table), so they get their
# own read/write surface for the dashboard Config tab. EDITABLE = the 3 operational scalars only;
# writes are targeted line-edits (comment-preserving — the file is heavily commented and only pyyaml
# is present, which would strip comments on a full round-trip). The type LISTS are read-only here
# (rarely changed; edit via SSH). No service reload needed — classify_edges re-reads per run.
_GRAPH_EDITABLE = {
    "review_mode": ("bool", None),
    "auto_apply_confidence": ("float", (0.0, 1.0)),
    "confidence_votes": ("int", (1, 9)),
}


@app.get("/graph-config")
def graph_config_get():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "viewer", "approver"):
        return jsonify(error="manager/viewer/approver role required"), 403
    cfg = AL_apply.load_graph_cfg() or {}
    cl = cfg.get("classifier") or {}
    return jsonify(
        classifier={
            "review_mode": cl.get("review_mode", True),
            "auto_apply_confidence": cl.get("auto_apply_confidence", 1.0),
            "confidence_votes": cl.get("confidence_votes", 2),
            "autopromote_types": cl.get("autopromote_types") or [],
            "review_types": cl.get("review_types") or [],
        },
        ontology_types=(cfg.get("ontology") or {}).get("types") or [],
        provider=(cfg.get("llm") or {}).get("provider") or "ollama",
        editable=list(_GRAPH_EDITABLE.keys()))


@app.post("/graph-config")
def graph_config_set():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):   # dashboard runs as approver (mTLS-gated to the operator)
        return jsonify(error="manager/approver role required"), 403
    b = request.get_json(silent=True) or {}
    updates = {}
    for k, v in b.items():
        if k not in _GRAPH_EDITABLE:
            return jsonify(error="knob '%s' is not editable here" % k), 400
        typ, rng = _GRAPH_EDITABLE[k]
        try:
            if typ == "bool":
                if isinstance(v, bool):
                    nv = v
                elif str(v).strip().lower() in ("true", "false"):
                    nv = str(v).strip().lower() == "true"
                else:
                    raise ValueError("bool")
                lit = "true" if nv else "false"
            elif typ == "float":
                nv = float(v)
                if rng and not (rng[0] <= nv <= rng[1]):
                    raise ValueError("range")
                lit = repr(nv)
            else:  # int
                nv = int(v)
                if rng and not (rng[0] <= nv <= rng[1]):
                    raise ValueError("range")
                lit = str(nv)
        except Exception:
            return jsonify(error="bad value for %s" % k), 400
        updates[k] = lit
    if not updates:
        return jsonify(error="no editable knobs supplied"), 400
    path = AL_apply._GRAPH_CFG_PATH
    try:
        with open(path) as f:
            text = f.read()
    except Exception as e:
        return _internal_err("cannot read graph.yaml", 500, e)
    new_text = text
    for k, lit in updates.items():
        pat = re.compile(r"(?m)^(\s*%s:\s*)(\S+)(.*)$" % re.escape(k))
        if not pat.search(new_text):
            return jsonify(error="knob '%s' not found in graph.yaml" % k), 500
        new_text = pat.sub(lambda m: m.group(1) + lit + m.group(3), new_text, count=1)
    bak = path + ".bak-graphcfg-" + str(int(time.time()))
    try:
        with open(bak, "w") as f:
            f.write(text)
        with open(path, "w") as f:
            f.write(new_text)
    except Exception as e:
        return _internal_err("write failed", 500, e)
    try:                                            # belt-and-suspenders: it must still parse
        cfg2 = AL_apply.load_graph_cfg()
        assert isinstance(cfg2, dict) and cfg2.get("classifier")
    except Exception as e:
        with open(path, "w") as f:                  # restore on any parse failure
            f.write(text)
        return _internal_err("post-write validation failed, restored", 500, e)
    conn = db(); cur = conn.cursor()
    log(cur, a["name"], "graph_config_set", "graph.yaml", None, {"updates": updates})
    conn.commit(); cur.close(); conn.close()
    cl = cfg2.get("classifier") or {}
    return jsonify(ok=True, classifier={"review_mode": cl.get("review_mode"),
                   "auto_apply_confidence": cl.get("auto_apply_confidence"),
                   "confidence_votes": cl.get("confidence_votes")})


@app.post("/proposal/<pid>/decide")
def proposal_decide(pid):
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required to decide"), 403
    b = request.get_json(silent=True) or {}
    decision = b.get("decision")
    if decision not in ("approved", "rejected"):
        return jsonify(error="decision must be 'approved' or 'rejected'"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE proposal SET status=%s, decided_by=%s, decided_at=now(), reason=%s "
                "WHERE id=%s AND status='pending' RETURNING *", (decision, a["name"], b.get("reason"), pid))
    prop = cur.fetchone()
    if not prop:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="not found or already decided"), 404
    # approving a proposal whose name is bootstrap-injected REWRITES every session's instructions
    # (apply_proposal -> retire_prior installs it). That is manager-only — an approver may not do it.
    if prop.get("name") in BOOTSTRAP_PINNED and a["role"] != "manager":
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="only a manager may approve a change to bootstrap-injected name '%s'" % prop.get("name")), 403

    mem_id = None
    if decision == "approved":
        # Materialize the proposal into a real memory row (the Step 6 apply step).
        # A human Keep VALIDATES the fact -> it lands trusted (author != validator holds:
        # the proposer didn't decide this; the approver/manager did).
        try:
            mem_id = AL_apply.apply_proposal(cur, prop, embed_fn=embed, vec_fn=vec_literal, trust="trusted")
        except Exception as e:
            conn.rollback(); cur.close(); conn.close()
            return _internal_err("apply failed", 500, e)
        cur.execute("UPDATE proposal SET target_memory_id=%s WHERE id=%s", (mem_id, pid))
        # tail: sign the freshly-materialized trusted row (sign exactly what was stored).
        cur.execute("SELECT name, body, author_body, source_session FROM memory WHERE id=%s", [mem_id])
        _mr = cur.fetchone()
        if _mr:
            _sig, _kid = sign_memory(_mr["name"], _mr["body"], _mr["author_body"], _mr["source_session"])
            if _sig:
                cur.execute("UPDATE memory SET signature=%s, sig_key_id=%s WHERE id=%s", (_sig, _kid, mem_id))
        AL_apply.link_usage(cur, prop.get("source_session"), mem_id)  # recalled->created edges
        try:                                                          # explicit [[ref]] edges (shared scope)
            cur.execute("SAVEPOINT expl_refs")
            AL_apply.link_explicit_refs(cur, mem_id, prop.get("proposed_body") or prop.get("body") or "")
            cur.execute("RELEASE SAVEPOINT expl_refs")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT expl_refs")
        log(cur, a["name"], "proposal_apply", "memory", str(mem_id), {"from_proposal": pid})
    elif decision == "rejected":
        # Remember WHY this was dropped so the same junk can't return next session. record when
        # the caller asks (record_lesson flag, now reachable via the MCP tool) OR AUTO when this exact
        # name has been rejected before — a repeat rejection is a strong "already said no" signal, so
        # the immune table finally populates on genuine repeat-offenders (without over-blocking one-offs).
        _lp = b.get("lesson_pattern") or prop.get("name") or ""
        _do_lesson = bool(b.get("record_lesson"))
        _auto = False
        if not _do_lesson and _lp and prop.get("name"):
            cur.execute("SELECT count(*) AS c FROM proposal WHERE name=%s AND status='rejected' AND id<>%s",
                        (prop.get("name"), pid))
            _pr = cur.fetchone()
            if _pr and (_pr["c"] if isinstance(_pr, dict) else _pr[0]) >= 1:
                _do_lesson = True; _auto = True
        if _do_lesson and _lp:
            lid = AL_lessons.record_lesson(cur, title=(prop.get("name") or "rejected")[:120],
                                           pattern=_lp,
                                           severity=b.get("lesson_severity") or "normal",
                                           source_proposal_id=pid)
            log(cur, a["name"], "lesson_record", "lesson", str(lid), {"from_proposal": pid, "auto": _auto})

    log(cur, a["name"], "proposal_decide", "proposal", pid, {"decision": decision, "memory_id": str(mem_id) if mem_id else None})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, id=pid, status=decision, memory_id=str(mem_id) if mem_id else None)


@app.get("/memory/verify")
def memory_verify():
    """ tail — re-verify every signed memory row against the brain's Ed25519 key and report any
    whose signature no longer matches its content (a direct-Postgres edit that bypassed the API).
    Detective control: on ANY tamper it writes an action_log entry + alerts every manager (like the
    bootstrap-MOC guard, migration 0016). unsigned = legacy/no-key rows (not tampered)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver", "viewer"):
        return jsonify(error="manager/approver/viewer role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, body, author_body, source_session, signature FROM memory WHERE deleted_at IS NULL")
    signed = unsigned = 0
    tampered = []
    for r in cur.fetchall():
        v = verify_memory_row(r)
        if v is None:
            unsigned += 1
        elif v:
            signed += 1
        else:
            tampered.append({"id": str(r["id"]), "name": r.get("name")})
    _, kid = _sign_key()
    if tampered:                                    # detective: log + alert every manager
        log(cur, "brain-guard", "memory_sig_tamper", "memory", None,
            {"count": len(tampered), "ids": [t["id"] for t in tampered[:20]]})
        cur.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
        names = [row["name"] for row in cur.fetchall()]
        for m in names:
            cur.execute("INSERT INTO message(from_agent, to_agent, subject, body, kind) VALUES (%s,%s,%s,%s,%s)",
                        ("brain-guard", m, "ALERT: memory signature mismatch",
                         "%d memory row(s) failed Ed25519 verification — content changed since signing "
                         "(possible direct-DB tamper). Names: %s. Run GET /memory/verify for the list."
                         % (len(tampered), ", ".join(t["name"] or t["id"] for t in tampered[:10])), "alert"))
        conn.commit()
    cur.close(); conn.close()
    return jsonify(ok=True, key_id=kid, signing_enabled=bool(kid),
                   total=signed + unsigned + len(tampered), signed=signed,
                   unsigned_legacy=unsigned, tampered=len(tampered), tampered_rows=tampered)


@app.get("/audit")
def audit():
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "viewer", "approver"):
        return jsonify(error="manager/viewer/approver role required"), 403
    limit = _clip_int(request.args.get("limit"), 100, 1, 1000)   #/A2-7: bad input -> default, never 500
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,created_at,actor,action,target_kind,target_id,detail,reversible,reverted_at "
                "FROM action_log ORDER BY id DESC LIMIT %s", [limit])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["created_at"] = _iso(r.get("created_at")); r["reverted_at"] = _iso(r.get("reverted_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), audit=rows)


@app.get("/stats/tool-usage")
def stats_tool_usage():
    """per-agent tool-usage counts from action_log, so under-use of the deeper tools
    (deep_search / search_transcripts / ideas / attachments vs the recall habit) is VISIBLE on the
    dashboard. Returns {days, agents:{actor:{action:n}}, totals:{action:n}}. Role-gated like /audit."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "viewer", "approver"):
        return jsonify(error="manager/viewer/approver role required"), 403
    days = _clip_int(request.args.get("days"), 30, 1, 365)   # bad input -> default, never 500
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT actor, action, count(*)::int AS n "
                "FROM action_log WHERE created_at > now() - make_interval(days => %s) "
                "AND actor IS NOT NULL "
                "GROUP BY actor, action ORDER BY actor, n DESC", [days])
    agents, totals = {}, {}
    for r in cur.fetchall():
        agents.setdefault(r["actor"], {})[r["action"]] = r["n"]
        totals[r["action"]] = totals.get(r["action"], 0) + r["n"]
    cur.execute(
        "SELECT to_char(d.day,'YYYY-MM-DD') AS day, "
        "COALESCE(SUM(CASE WHEN a.action='recall' THEN 1 ELSE 0 END),0)::int AS recall, "
        "COALESCE(SUM(CASE WHEN a.action IN ('personal_write','propose','idea_add','memory_attach') THEN 1 ELSE 0 END),0)::int AS writes, "
        "COALESCE(SUM(CASE WHEN a.action IN ('deep_search','session_search') THEN 1 ELSE 0 END),0)::int AS deep "
        "FROM generate_series((now() - make_interval(days => %s))::date, now()::date, interval '1 day') AS d(day) "
        "LEFT JOIN action_log a ON a.created_at::date = d.day AND a.actor IS NOT NULL "
        "GROUP BY d.day ORDER BY d.day", [days])
    series = [dict(r) for r in cur.fetchall()]
    days_axis = [r["day"] for r in series]
    cur.execute(
        "SELECT actor, to_char(created_at::date,'YYYY-MM-DD') AS day, "
        "SUM(CASE WHEN action='recall' THEN 1 ELSE 0 END)::int AS recall, "
        "SUM(CASE WHEN action IN ('personal_write','propose','idea_add','memory_attach') THEN 1 ELSE 0 END)::int AS writes, "
        "SUM(CASE WHEN action IN ('deep_search','session_search') THEN 1 ELSE 0 END)::int AS deep "
        "FROM action_log WHERE created_at > now() - make_interval(days => %s) AND actor IS NOT NULL "
        "GROUP BY actor, created_at::date", [days])
    _raw = {}
    for r in cur.fetchall():
        _raw.setdefault(r["actor"], {})[r["day"]] = {"recall": r["recall"], "writes": r["writes"], "deep": r["deep"]}
    series_by_agent = {
        actor: [dict(day=d, **dd.get(d, {"recall": 0, "writes": 0, "deep": 0})) for d in days_axis]
        for actor, dd in _raw.items()
    }
    cur.close(); conn.close()
    return jsonify(days=days, agents=agents, totals=totals, series=series, series_by_agent=series_by_agent)


# ---------------------------------------------------------------------------
# Self-describing schema (the brain explains itself, so an agent doesn't have to
# read the repo to know what's here, where to write, or how to find it). Each
# table carries a one-line PURPOSE + the WRITE verb + the READ/find path; columns
# come live from the Postgres catalog; counts are access-filtered. ROLE-GATED so a
# non-manager never even learns a sensitive/system table exists (covert-channel
# guard — see/). The doc is the per-table semantic layer the catalog lacks.
# kind: knowledge|structure|system. roles: which roles may SEE the table (manager
# always sees all). To add a table here, give it a purpose + write/read path.
TABLE_DOC = {
    "memory":            {"kind": "knowledge", "roles": ("viewer", "approver", "worker", "readonly"),
                          "purpose": "Durable facts / decisions / preferences / gotchas.",
                          "write_via": "brain_propose -> proposal queue -> manager approve (never direct)",
                          "read_via": "brain_recall (semantic+FTS) or GET /memory/<name>"},
    "memory_relation":   {"kind": "system", "roles": ("viewer", "approver", "worker", "readonly"),
                          "purpose": "System-maintained relations (supersedes/conflicts_with/relates_to) for the graph.",
                          "write_via": "system (importer / temporal logic)", "read_via": "GET /graph"},
    "task":              {"kind": "structure", "roles": ("viewer", "approver", "worker", "readonly"),
                          "purpose": "Work items (T-numbers): status, assignee, project, tier, lane, notes.",
                          "write_via": "POST /tasks (add, auto T-number) / PATCH /tasks/<handle> (update)",
                          "read_via": "GET /tasks?status=&project=&assignee="},
    "project":           {"kind": "structure", "roles": ("viewer", "approver", "worker", "readonly"),
                          "purpose": "Projects each task maps to.",
                          "write_via": "POST /projects (upsert) / PATCH /projects/<slug>",
                          "read_via": "GET /projects?status="},
    "idea":              {"kind": "structure", "roles": ("viewer", "approver", "worker", "readonly"),
                          "purpose": "Raw ideas (raw -> promoted -> project).",
                          "write_via": "POST /ideas / PATCH /ideas/<id> (promote_to=<slug>)",
                          "read_via": "GET /ideas?status="},
    "message":           {"kind": "structure", "roles": ("viewer", "approver", "worker", "readonly"),
                          "purpose": "Agent inbox/chat — brain-native messages between bodies (replaces the vault bus).",
                          "write_via": "POST /messages (from_agent = caller)",
                          "read_via": "GET /inbox?unread=1&box=in|sent ; POST /messages/<id>/read"},
    "proposal":          {"kind": "system", "roles": ("viewer", "approver"),
                          "purpose": "The auto-learn / approve queue (pending -> approved|rejected).",
                          "write_via": "brain_propose", "read_via": "GET /proposals or the dashboard /approve"},
    "lesson":            {"kind": "knowledge", "roles": (),
                          "purpose": "Poison-defense lessons (rejected/poison patterns).",
                          "write_via": "auto-learn (Step 6 D)", "read_via": "-"},
    "action_log":        {"kind": "system", "roles": ("viewer", "approver"),
                          "purpose": "Append-only audit of every gated action.",
                          "write_via": "system (every endpoint logs)", "read_via": "GET /audit"},
    "agent":             {"kind": "system", "roles": (),
                          "purpose": "Body identities, roles, access scope, tokens.",
                          "write_via": "enrollment / manager", "read_via": "-"},
    "enrollment":        {"kind": "system", "roles": (),
                          "purpose": "Self-service agent-enrollment applications.",
                          "write_via": "POST /enroll", "read_via": "GET /enroll/pending"},
    "schema_migrations": {"kind": "system", "roles": (),
                          "purpose": "Versioned schema history (every migration recorded here).",
                          "write_via": "migrate.py", "read_via": "-"},
}


@app.get("/schema")
def schema():
    """Role-filtered self-description: every table the caller may see, with its
    purpose, columns (live from the catalog), how to write it, how to find it, and
    an access-filtered live count. Lets an agent orient via the MCP without the repo."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    role = a["role"]
    conn = db(); cur = conn.cursor()
    out = []
    for name, meta in TABLE_DOC.items():
        if role != "manager" and role not in meta["roles"]:
            continue
        cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (name,))
        cols = [{"name": c, "type": t} for c, t in cur.fetchall()]
        if not cols:
            continue   # table not created yet (e.g. lesson) -> omit silently
        if meta["kind"] == "knowledge":
            w, p = access_where(a)
        elif meta["kind"] == "structure":
            w, p = access_where(a, soft_delete=False, temporal=False)
        else:
            w, p = "TRUE", []
        cur.execute("SELECT count(*) FROM %s WHERE %s" % (name, w), p)
        out.append({"table": name, "kind": meta["kind"], "purpose": meta["purpose"],
                    "write_via": meta["write_via"], "read_via": meta["read_via"],
                    "columns": cols, "count": cur.fetchone()[0]})
    cur.close(); conn.close()
    return jsonify(role=role, tables=out,
                   vocab={"sensitivity": list(SENSITIVITY), "trust": list(TRUST),
                          "origin_channel": list(ORIGIN_CHANNELS),
                          "mtype": ["reference", "feedback", "project", "user", "memory"]})


# ---------------------------------------------------------------------------
# Provisional tier ( / provisional-tier-design.md). An agent writes its OWN memory
# and uses it for 2 WEEKS before a manager graduates it to trusted/shared — or it expires
# and soft-deletes. Provisional rows are AUTHOR-ONLY (enforced in mem_read_where, all roles)
# + quarantined + TTL. A manager (managers) graduates; a manager may NOT graduate its OWN
# provisional note (author != validator) — those escalate to the operator's dashboard.
# Provisional-note expiry resolves live via cfg("PROVISIONAL_TTL_DAYS") at the insert sites below.


def _derive_name(text):
    """derive a slug name from a body when the caller supplied none, so a memory can never
    persist nameless (which breaks name-keyed lookups + the knowledge graph). Prefer the first
    markdown heading, then a bold lead-in, then the first non-empty line; normalize to the
    underscored lowercase slug used everywhere else. Falls back to a body-hash tail if nothing usable."""
    t = text or ""
    m = (re.search(r'^\s*#+\s*(.+?)\s*$', t, re.M)      # markdown heading
         or re.search(r'\*\*(.+?)\*\*', t)              # first **bold** lead-in
         or re.search(r'^\s*(\S.+?)\s*$', t, re.M))     # first non-empty line
    raw = (m.group(1) if m else t)[:80]
    slug = re.sub(r'_+', '_', re.sub(r'[^a-z0-9]+', '_', raw.lower())).strip('_')[:60].strip('_')
    return slug or ('note_' + hashlib.sha256(t.encode('utf-8')).hexdigest()[:10])


@app.post("/provisional/memory")
def provisional_memory():
    """Write a provisional, author-only, 2-week memory the author can use immediately."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "worker"):
        return jsonify(error="only manager/worker agents may write provisional memory"), 403
    b = request.get_json(silent=True) or {}
    text = (b.get("body") or "").strip()
    if not text:
        return jsonify(error="empty body"), 400
    _provided = (b.get("name") or "").strip()   # the caller's explicit name (may be blank -> we derive one)
    if _provided in BOOTSTRAP_PINNED and a["role"] != "manager":   # bootstrap-injected names are manager-only
        return jsonify(error="'%s' is injected as instructions into every session; a worker may not "
                             "write to it" % _provided), 403
    mtype = b.get("mtype") if b.get("mtype") in ("user", "feedback", "project", "reference", "memory") else "memory"
    sens = b.get("sensitivity") if b.get("sensitivity") in SENSITIVITY else "normal"
    channel = b.get("origin_channel") or "agent-reasoning"
    # (Approval 2.0 step 2): a MANAGER may self-trust its OWN note at creation (managers are the reliable,
    # self-approving tier); a WORKER may NOT (author!=validator) — its note stays untrusted until source-checked
    # or a manager vouches. Autolearn never sets this (its captures always land untrusted, the weak spot).
    _trust = "trusted" if (a["role"] == "manager" and (b.get("trusted") is True or b.get("trust") == "trusted")) else "quarantined"
    try:
        emb = vec_literal(embed(text)); embed_model = MODEL
    except Exception:
        emb = None; embed_model = None
    conn = db(); cur = conn.cursor()
    # never persist a nameless memory — derive a slug from the body when none was supplied, and
    # make an auto-derived name unique here (a caller-SUPPLIED name that collides still 409s below).
    name = _provided or _derive_name(text)
    if not _provided:
        _base = name; _k = 1
        while True:
            cur.execute("SELECT 1 FROM memory WHERE name=%s AND deleted_at IS NULL LIMIT 1", (name,))
            if not cur.fetchone():
                break
            _k += 1; name = "%s_%d" % (_base, _k)
    chash = compute_content_hash(name, text)   # canonical name+body hash (was agent+body)
    # stamp the body's server-recorded CURRENT session (reliable) over the client-passed value
    # (which on the remote MCP is the connection's frozen, often-stale, X-Brain-Session header).
    sid = active_session(cur, a["name"]) or b.get("source_session")
    sig, sig_kid = sign_memory(name, text, a["name"], sid)   # tail: tamper-evidence
    try:
        cur.execute(
            "INSERT INTO memory(name,mtype,mem_tier,share_status,description,body,embedding,embed_model,readers,"
            "sensitivity,origin_channel,trust,author_body,source_session,content_hash,expires_at,signature,sig_key_id,tags) "
            "VALUES (%s,%s,'provisional','personal',%s,%s,%s::vector,%s,ARRAY[%s],%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s) "
            "RETURNING id",
            (name, mtype, b.get("description"), text, emb, embed_model, a["name"],
             sens, channel, _trust, a["name"], sid, chash, sig, sig_kid, list(b.get("tags") or [])))
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="a live memory with that name already exists; pick a unique name"), 409
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error=str(e)), 400
    row = cur.fetchone()
    try:                                                          # explicit [[ref]] edges (personal-brain scope)
        cur.execute("SAVEPOINT expl_refs_pm")
        AL_apply.link_explicit_refs(cur, row[0], text)
        cur.execute("RELEASE SAVEPOINT expl_refs_pm")
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT expl_refs_pm")
    if emb is not None:                                           # co-use link personal notes too (was autolearn-ingest only)
        try:
            cur.execute("SAVEPOINT couse_pm")
            _lthr = float(cfg("AUTOLEARN_LINK_COSINE")); _lcap = int(cfg("AUTOLEARN_LINK_CAP"))
            cur.execute(
                "SELECT n.id, 1-(n.embedding <=> %s::vector) AS sim FROM memory n "
                "WHERE n.deleted_at IS NULL AND n.invalid_at IS NULL AND n.embedding IS NOT NULL "
                "AND n.id <> %s AND (n.share_status IN ('trusted','shared') OR n.author_body=%s) "
                "ORDER BY n.embedding <=> %s::vector LIMIT %s", (emb, row[0], a["name"], emb, _lcap))
            for _nb in cur.fetchall():
                if float(_nb[1] or 0) < _lthr:
                    break
                cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight) "
                            "VALUES (%s,%s,'relates_to','personal-write-link',%s,1) "
                            "ON CONFLICT (src_id,dst_id,rel_type) DO UPDATE SET "
                            "weight=memory_relation.weight+1, updated_at=now()", (row[0], _nb[0], sens))
            cur.execute("RELEASE SAVEPOINT couse_pm")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT couse_pm")
    _index_memory_entities(cur, row[0], text)                # self-maintain entity index on agent write
    log(cur, a["name"], "personal_write", "memory", str(row[0]), {"channel": channel, "share_status": "personal"})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, id=str(row[0]), trust="quarantined", share_status="personal",
                   note="author-only; usable now; kept permanently until you brain_share it (-> manager review) or delete it")


@app.get("/provisional/mine")
def provisional_mine():
    """List the caller's own live provisional memories (+ TTLs)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,name,description,mtype,origin_channel,created_at,expires_at FROM memory "
                "WHERE author_body=%s AND mem_tier='provisional' AND deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC", [a["name"]])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"]); r["created_at"] = _iso(r.get("created_at")); r["expires_at"] = _iso(r.get("expires_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), provisional=rows)


@app.post("/memory/<mid>/share")
def memory_share(mid):
    """Author promotes ONE of their OWN personal notes to 'ready_to_share': it enters the
    manager review queue and the author STOPS recalling it until a manager trusts or deletes it.
    Ownership is enforced by the author_body guard — you can only share your own personal note."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if (a.get("access_scope") or {}).get("personal_only"):   # personal-only agents cannot share
        return jsonify(error="personal-only agents cannot share; their notes stay private"), 403
    # the requester may PROPOSE an audience (agent-name and/or group tokens); it is stored on the
    # draft and a manager confirms/narrows it at graduation (or secures it if the note is sensitive).
    _b = request.get_json(silent=True) or {}
    _rr = _b.get("readers")
    _req_readers = _rr if (isinstance(_rr, list) and _rr and all(isinstance(x, str) for x in _rr)) else None
    conn = db(); cur = conn.cursor()
    if _req_readers is not None:
        cur.execute("UPDATE memory SET share_status='ready_to_share', readers=%s, updated_at=now() "
                    "WHERE id=%s AND author_body=%s AND share_status='personal' AND deleted_at IS NULL "
                    "RETURNING id", (_req_readers, mid, a["name"]))
    else:
        cur.execute("UPDATE memory SET share_status='ready_to_share', updated_at=now() "
                    "WHERE id=%s AND author_body=%s AND share_status='personal' AND deleted_at IS NULL "
                    "RETURNING id", (mid, a["name"]))
    row = cur.fetchone()
    if not row:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="not found, not yours, or not a personal note"), 404
    log(cur, a["name"], "memory_share", "memory", mid, {"to": "ready_to_share"})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, id=mid, share_status="ready_to_share",
                   note="in the manager review queue; you can still recall it while it's pending, until a manager trusts (shares) or deletes it")


@app.post("/memory/<mid>/validate")
def memory_validate(mid):
    """ (validate-on-recall): the recalling agent self-validates ONE of its OWN untrusted
    (personal + quarantined) notes against the note's SOURCE transcript, then either self-trusts
    it or deletes it. This is what makes a personal capture usable: recall flags it
    trusted:false and hands back its source_session; the agent reads that transcript
    (brain_get_session_turns), confirms the note faithfully reflects it, and calls here.

    Body: {verdict: "trusted"|"invalid", source_session: "<sid>", note?: "<what was checked>"}.
      * verdict="trusted" -> flip trust quarantined->trusted (share_status STAYS 'personal' — trusted
        to the AUTHOR only; cross-agent trust still needs a manager, so author!=validator holds).
      * verdict="invalid" -> soft-delete the note (source contradicts it, or no longer supports it).

    SAFEGUARD (source-tied + audited): the caller MUST pass the source_session it validated against,
    and it MUST equal the note's stored source_session (else 400) — so a trust-flip is provably tied
    to reading the right transcript, not honor-system. Every call writes a memory_self_validate
    action_log row. Ownership: a worker may only validate its OWN note; a manager may validate any
    untrusted note (D5 — managers self-trust any note with a valid source)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    b = request.get_json(silent=True) or {}
    verdict = (b.get("verdict") or "").strip().lower()
    basis = (b.get("basis") or "").strip().lower()       # 'manager-vouch'|'human-vouch' = vouch with NO source
    src = (b.get("source_session") or "").strip()
    if verdict not in ("trusted", "invalid", "needs-human"):
        return jsonify(error="verdict must be 'trusted', 'invalid', or 'needs-human'"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Target must be a LIVE untrusted personal note; workers are author-guarded, managers are not (D5).
    owner_sql = "" if a["role"] == "manager" else " AND author_body=%s"
    owner_params = [] if a["role"] == "manager" else [a["name"]]
    cur.execute("SELECT id, name, body, author_body, source_session, tags FROM memory WHERE id=%s "
                "AND share_status='personal' AND trust='quarantined' AND deleted_at IS NULL" + owner_sql,
                [mid] + owner_params)
    row = cur.fetchone()
    if not row:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="not found, not yours, or not an untrusted personal note"), 404

    # (Approval 2.0 step 2) — the 2-tier escalation + no-source manager/human vouch.
    # needs-human: a MANAGER couldn't confirm -> flag the note for the operator (surfaces on the dashboard + session
    # brief). No trust flip; just tags it 'needs-human-vouch'.
    if verdict == "needs-human":
        if a["role"] != "manager":
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="only a manager may escalate a note to the human"), 403
        newtags = sorted(set((row.get("tags") or []) + ["needs-human-vouch"]))
        cur.execute("UPDATE memory SET tags=%s, updated_at=now() WHERE id=%s", [newtags, mid])
        log(cur, a["name"], "memory_escalate_human", "memory", mid, {"note": (b.get("note") or "")[:300]})
        conn.commit(); cur.close(); conn.close()
        return jsonify(ok=True, id=mid, escalated="human", tag="needs-human-vouch")

    # manager/human VOUCH with NO source (formalizes the one-off): a MANAGER vouches for a note (or
    # relays the operator's vouch) -> flip trusted + SIGN + audit with source_validated=false + basis + who.
    if verdict == "trusted" and basis in ("manager-vouch", "human-vouch"):
        if a["role"] != "manager":
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="only a manager may vouch for a note without a source"), 403
        sig, kid = sign_memory(row["name"], row["body"], row["author_body"], row.get("source_session"))
        newtags = [t for t in (row.get("tags") or []) if t not in ("needs-human-vouch", "needs-manager-vouch")]
        cur.execute("UPDATE memory SET trust='trusted', tags=%s, signature=%s, sig_key_id=%s, updated_at=now() "
                    "WHERE id=%s", [newtags, sig, kid, mid])
        log(cur, a["name"], "memory_vouch", "memory", mid,
            {"basis": basis, "source_validated": False, "by": a["name"], "note": (b.get("note") or "")[:300]})
        conn.commit(); cur.close(); conn.close()
        return jsonify(ok=True, id=mid, trust="trusted", basis=basis, source_validated=False)

    # symmetric manager VOUCH-invalid with NO source — a MANAGER judges an escalated note wrong and
    # soft-deletes it without needing its source transcript (the needs-human case is precisely one a manager
    # couldn't confirm from source). Mirrors the trusted-vouch path above; manager-only + audited.
    if verdict == "invalid" and basis in ("manager-vouch", "human-vouch"):
        if a["role"] != "manager":
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="only a manager may reject a note without a source"), 403
        cur.execute("UPDATE memory SET deleted_at=now(), invalid_at=now(), updated_at=now() WHERE id=%s", [mid])
        log(cur, a["name"], "memory_vouch", "memory", mid,
            {"basis": basis, "verdict": "invalid", "source_validated": False, "by": a["name"], "note": (b.get("note") or "")[:300]})
        conn.commit(); cur.close(); conn.close()
        return jsonify(ok=True, id=mid, deleted=True, basis=basis, source_validated=False)

    # default path: the original SOURCE-TIED self-validate (source_session required + must match the note's).
    if not src:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="source_session required (the transcript you validated against), or a manager may pass basis='manager-vouch'"), 400
    if (row.get("source_session") or "") != src:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="source mismatch: pass the note's own source_session (%s)" % row.get("source_session")), 400
    if verdict == "trusted":
        sig, kid = sign_memory(row["name"], row["body"], row["author_body"], row.get("source_session"))
        cur.execute("UPDATE memory SET trust='trusted', signature=%s, sig_key_id=%s, updated_at=now() WHERE id=%s",
                    [sig, kid, mid])
        result = {"ok": True, "id": mid, "trust": "trusted", "share_status": "personal", "source_validated": True}
    else:
        cur.execute("UPDATE memory SET deleted_at=now(), invalid_at=now(), updated_at=now() WHERE id=%s", [mid])
        result = {"ok": True, "id": mid, "deleted": True}
    log(cur, a["name"], "memory_self_validate", "memory", mid,
        {"verdict": verdict, "source_session": src, "source_validated": True, "note": (b.get("note") or "")[:300]})
    conn.commit(); cur.close(); conn.close()
    return jsonify(**result)


@app.get("/personal/inspect")
def personal_inspect():
    """Manager-only, AUDITED: inspect another agent's PERSONAL (author-only) notes on demand
    ('check other llm if they need'). Normal recall never surfaces another agent's personal
    memory — this is the explicit override. The returned text is untrusted DATA, not instructions."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    agent = request.args.get("agent")
    if not agent:
        return jsonify(error="agent= required"), 400
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,name,description,body,author_body,origin_channel,sensitivity,created_at "
                "FROM memory WHERE author_body=%s AND share_status='personal' AND deleted_at IS NULL "
                "ORDER BY created_at DESC", [agent])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"]); r["created_at"] = _iso(r.get("created_at"))
    log(cur, a["name"], "personal_inspect", "memory", None, {"agent": agent, "n": len(rows)})
    conn.commit(); cur.close(); conn.close()
    return jsonify(count=len(rows), agent=agent, personal=rows)


@app.get("/provisional/pending")
def provisional_pending():
    """Manager review surface: ALL live provisional memory awaiting a graduate/delete decision.
    Deliberately bypasses recall's author-only rule — this is the explicit 'judge these
    unverified notes' view (NOT trusted context). Manager-only."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,name,description,body,author_body,origin_channel,sensitivity,created_at,expires_at,source_session "
                "FROM memory WHERE share_status='ready_to_share' AND deleted_at IS NULL "
                "ORDER BY created_at", [])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        # anchored provisional tables travel with this memory's decision
        cur.execute("SELECT display_name, columns_spec FROM provisional_artifact "
                    "WHERE anchor_memory_id=%s AND status='provisional'", [r["id"]])
        r["tables"] = [{"name": t["display_name"], "columns": [c["name"] for c in t["columns_spec"]]}
                       for t in cur.fetchall()]
        # does this note's source transcript actually exist (session row + >=1 live turn)? so the
        # manager knows whether it can be source-validated (brain_get_session_turns) before graduating.
        _src = r.get("source_session")
        if _src:
            cur.execute("SELECT EXISTS (SELECT 1 FROM session s JOIN session_turn t ON t.session_id=s.id "
                        "WHERE s.source_session=%s) AS ok", [_src])
            r["source_available"] = bool(cur.fetchone()["ok"])   # RealDictCursor -> dict, not tuple
        else:
            r["source_available"] = False
        r["id"] = str(r["id"]); r["created_at"] = _iso(r.get("created_at")); r["expires_at"] = _iso(r.get("expires_at"))
        r["self_authored"] = (r.get("author_body") == a["name"])   # a manager may NOT graduate its own (approver can)
    cur.close(); conn.close()
    return jsonify(count=len(rows), pending=rows)


@app.post("/provisional/<pid>/decide")
def provisional_decide(pid):
    """Graduate a provisional memory to trusted/shared, or delete it (manager only).
    A manager may NOT graduate its OWN provisional note (author != validator) — those go to the operator
    (the dashboard's 'approver' role decides them; it authors nothing so never self-validates)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required to decide"), 403
    b = request.get_json(silent=True) or {}
    decision = b.get("decision")
    if decision not in ("graduate", "delete", "escalate"):
        return jsonify(error="decision must be 'graduate', 'delete', or 'escalate'"), 400
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT author_body, name, body, source_session FROM memory WHERE id=%s AND share_status='ready_to_share' AND deleted_at IS NULL", [pid])
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify(error="not found or not awaiting review (ready_to_share)"), 404

    # escalate-to-human — the manager can't source-confirm this; leave it ready_to_share (the operator
    # decides via the approver/dashboard), record the escalation, and notify the human review identity.
    if decision == "escalate":
        log(cur, a["name"], "provisional_escalate", "memory", pid,
            {"name": row[1], "reason": b.get("reason"), "source_session": row[3]})
        cur.execute("SELECT name FROM agent WHERE role='approver' AND revoked_at IS NULL")
        for _ap in [r[0] for r in cur.fetchall()]:
            cur.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) VALUES (%s,%s,%s,%s,'alert')",
                        (a["name"], _ap, "escalated for human review: '%s'" % (row[1] or pid),
                         "%s could not source-confirm this ready_to_share note%s. Judge it against its "
                         "source or the operator's knowledge, then graduate or delete."
                         % (a["name"], (" (reason: %s)" % b.get("reason")) if b.get("reason") else "")))
        conn.commit(); cur.close(); conn.close()
        return jsonify(ok=True, id=pid, decision="escalate", name=row[1],
                       note="left in the review queue for a human; approver(s) notified")
    if decision == "graduate" and row[0] == a["name"] and a["role"] != "manager":  #: the managers are one entity (both managers) -> may self-approve; block still holds for worker LLMs
        cur.close(); conn.close()
        return jsonify(error="cannot graduate your OWN provisional memory (author != validator) — escalate to the operator"), 403
    # — graduation is CURATION: the reviewer may RENAME + AMEND the draft as it becomes permanent.
    new_name = b.get("name"); new_desc = b.get("description"); new_body = b.get("body")
    if decision == "graduate" and new_name:                                         # name-collision pre-check
        cur.execute("SELECT 1 FROM memory WHERE name=%s AND deleted_at IS NULL AND id<>%s", (new_name, pid))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify(error="a live memory named '%s' already exists" % new_name), 409
    # wrap the mutation (it can DROP/ALTER tables — a mid-flight error must NOT leak a held lock)
    try:
        if decision == "graduate":
            if isinstance(b.get("readers"), list) and b.get("readers"):
                readers = b.get("readers")
            else:
                cur.execute("SELECT sensitivity, readers FROM memory WHERE id=%s", [pid])
                _gs = cur.fetchone()
                _sens = (_gs[0] if _gs else "normal"); _proposed = (_gs[1] if _gs else None)
                if _proposed and _sens in ("public", "normal"):                 # honor the requester's proposed audience on a non-sensitive note
                    readers = _proposed
                else:                                                           # secure sensitive drafts by default
                    readers = AL_apply.default_readers(_sens, "trusted")
            sets = ["trust='trusted'", "share_status='trusted'", "mem_tier='semantic'", "expires_at=NULL",
                    "updated_at=now()", "readers=%s"]
            params = [readers]
            if new_name is not None:
                sets.append("name=%s"); params.append(new_name)
            if new_desc is not None:
                sets.append("description=%s"); params.append(new_desc)
            if new_body is not None:                                                # amended body -> re-embed + re-hash
                sets.append("body=%s"); params.append(new_body)
                try:
                    sets.append("embedding=%s::vector"); params.append(vec_literal(embed(new_body)))
                    sets.append("embed_model=%s"); params.append(MODEL)
                except Exception:
                    pass
            if new_name is not None or new_body is not None:                        # re-sign amended content
                _en = new_name if new_name is not None else row[1]
                _eb = new_body if new_body is not None else row[2]
                _sig, _kid = sign_memory(_en, _eb, row[0], row[3])
                sets.append("signature=%s"); params.append(_sig)
                sets.append("sig_key_id=%s"); params.append(_kid)
                sets.append("content_hash=%s"); params.append(compute_content_hash(_en, _eb))  # canonical, re-hash on name OR body change
            params.append(pid)
            cur.execute("UPDATE memory SET " + ", ".join(sets) +
                        " WHERE id=%s AND share_status='ready_to_share' RETURNING name", params)
            name = cur.fetchone()                                                   # fetch BEFORE reusing the cursor
            if new_body is not None:                                                # resync [[ref]] edges to the amended body
                try:
                    cur.execute("SAVEPOINT expl_refs_g")
                    AL_apply.resync_explicit_refs(cur, pid, new_body)
                    cur.execute("RELEASE SAVEPOINT expl_refs_g")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT expl_refs_g")
            _drop_or_graduate_anchor_tables(cur, pid, "graduate", a["name"], readers)
            # record whether the reviewer source-validated it — a matching source_session means the
            # manager confirmed the note against its own transcript (spot-checkable, not honor-system).
            _src_ok = bool(row[3] and b.get("source_session") and b.get("source_session") == row[3])
            action, detail = "provisional_graduate", {"readers": readers,
                                                       "amended": bool(new_name or new_desc or new_body),
                                                       "source_validated": _src_ok,
                                                       "has_source": bool(row[3])}
        else:
            cur.execute("UPDATE memory SET deleted_at=now() WHERE id=%s AND share_status='ready_to_share' RETURNING name", [pid])
            name = cur.fetchone()
            _drop_or_graduate_anchor_tables(cur, pid, "drop", a["name"])            # cascade-drop anchored tables
            action, detail = "provisional_delete", {"reason": b.get("reason")}
        log(cur, a["name"], action, "memory", pid, detail)
        conn.commit()
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("decision failed", 500, e)
    cur.close(); conn.close()
    return jsonify(ok=True, id=pid, decision=decision, name=name[0] if name else None)


@app.post("/provisional/sweep")
def provisional_sweep():
    """Soft-delete provisional rows whose 2-week TTL has lapsed (hygiene; recall already
    hides them). Manager-only; meant to be run on a daily schedule."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE memory SET deleted_at=now() WHERE mem_tier='provisional' AND deleted_at IS NULL "
                    "AND expires_at IS NOT NULL AND expires_at < now() RETURNING id")
        expired = [r[0] for r in cur.fetchall()]
        for mid in expired:                               # cascade-drop tables anchored to an expired memory
            _drop_or_graduate_anchor_tables(cur, mid, "drop", a["name"])
        # log EVERY run (incl. expired=0) — the old `if expired:` guard meant a healthy daily
        # sweep wrote NO action_log row, so /audit showed "0 runs ever" (Audit-B false alarm) even
        # though the LaunchAgent fired daily. An always-on run row is the real liveness signal.
        log(cur, a["name"], "provisional_sweep", "memory", None, {"expired": len(expired)})
        conn.commit()
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("sweep failed", 500, e)
    cur.close(); conn.close()
    return jsonify(ok=True, expired=len(expired))


# ---------------------------------------------------------------------------
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _curate_resolve(cur, ident):
    """Resolve a curate identifier to a live memory id. Accepts either a uuid OR a memory NAME
    (recall surfaces rows by name, so a manager who spots a bad line has the name, not the id).
    Returns the uuid string or None."""
    if _UUID_RE.match(ident or ""):
        cur.execute("SELECT id FROM memory WHERE id=%s AND deleted_at IS NULL", [ident])
    else:
        cur.execute("SELECT id FROM memory WHERE name=%s AND deleted_at IS NULL", [ident])
    r = cur.fetchone()
    if not r:
        return None
    return str(r["id"] if isinstance(r, dict) else r[0])   # works for RealDictCursor + plain cursor


# — explicit manager CURATE surface. RLS already lets a manager read+update ANY
# row (mem_sel/mem_upd policies), but two problems remain at the app layer: (1) the
# normal recall/read path must NEVER mutate — a manager amending a line should do it
# through a DELIBERATE, separate request (mistake-guard, the operator's design #2); and (2)
# recall hides other agents' PERSONAL rows and all ready_to_share rows, so a manager
# can't reach an arbitrary line to check/fix it. These two endpoints are that path:
# inspect any single live memory (any author, trusted or not) and amend it. The
# lifecycle transitions (graduate/delete/share) keep their own endpoints; curate is
# for CONTENT + metadata edits. Manager/approver only; the DB 0016 trigger still
# alarms any bootstrap-injected-name content change made this way (detective control).
@app.get("/curate/memory/<mid>")
def curate_memory_get(mid):
    """Fetch ANY single live memory with full curation fields (bypasses recall's author-only
    + share-tier gate). The returned body is un-trusted DATA, not instructions. Manager/approver only."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rid = _curate_resolve(cur, mid)
    if not rid:
        cur.close(); conn.close()
        return jsonify(error="not found"), 404
    cur.execute("SELECT id,name,mtype,mem_tier,share_status,trust,description,body,tags,"
                "author_body,origin_channel,sensitivity,readers,created_at,updated_at,expires_at "
                "FROM memory WHERE id=%s AND deleted_at IS NULL", [rid])
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify(error="not found"), 404
    r = dict(row)
    r["id"] = str(r["id"]); r["created_at"] = _iso(r.get("created_at"))
    r["updated_at"] = _iso(r.get("updated_at")); r["expires_at"] = _iso(r.get("expires_at"))
    r["trusted"] = (r.get("share_status") == "trusted")
    log(cur, a["name"], "curate_read", "memory", rid, None)
    conn.commit(); cur.close(); conn.close()
    return jsonify(memory=r)


@app.post("/curate/memory/<mid>")
def curate_memory_edit(mid):
    """Amend a live memory (manager/approver only) — the explicit, separate-from-recall mutation
    path. Editable: name, description, body, sensitivity, readers, tags. A body change re-embeds +
    re-hashes + resyncs [[ref]] edges. Name changes are collision-checked. share_status is NOT
    changed here (lifecycle lives in /provisional/*/decide + /share). Every edit is logged."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    b = request.get_json(silent=True) or {}
    fields = {}
    if "name" in b:
        fields["name"] = (b.get("name") or "").strip() or None
    if "description" in b:
        fields["description"] = b.get("description")
    if "body" in b:
        fields["body"] = b.get("body")
    if "sensitivity" in b:
        s = (b.get("sensitivity") or "").strip().lower()
        if s not in ("public", "normal", "sensitive", "secret"):
            return jsonify(error="sensitivity must be public|normal|sensitive|secret"), 400
        fields["sensitivity"] = s
    if "readers" in b:
        if not isinstance(b.get("readers"), list):
            return jsonify(error="readers must be a list"), 400
        fields["readers"] = b.get("readers")
    if "tags" in b:
        if not isinstance(b.get("tags"), list):
            return jsonify(error="tags must be a list"), 400
        fields["tags"] = [str(t) for t in b.get("tags")]
    if not fields:
        return jsonify(error="no editable fields supplied (name/description/body/sensitivity/readers/tags)"), 400
    conn = db(); cur = conn.cursor()
    mid = _curate_resolve(cur, mid)
    if not mid:
        cur.close(); conn.close()
        return jsonify(error="not found"), 404
    cur.execute("SELECT name, body, author_body, source_session FROM memory WHERE id=%s", [mid])   # re-sign inputs
    _cur_row = cur.fetchone() or (None, None, None, None)
    if fields.get("name"):
        cur.execute("SELECT 1 FROM memory WHERE name=%s AND deleted_at IS NULL AND id<>%s", (fields["name"], mid))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify(error="a live memory named '%s' already exists" % fields["name"]), 409
    sets = ["updated_at=now()"]; params = []
    for col in ("name", "description", "body", "sensitivity", "readers", "tags"):
        if col in fields:
            sets.append(col + "=%s"); params.append(fields[col])
    if fields.get("body") is not None and "body" in fields:
        try:
            _emb = vec_literal(embed(fields["body"]))     # compute FIRST — if embed() throws, we
            sets.append("embedding=%s::vector"); params.append(_emb)   # append nothing (no half-append)
            sets.append("embed_model=%s"); params.append(MODEL)
        except Exception:
            # re-embed failed (e.g. embedder down). Do NOT keep the STALE vector — NULL it so
            # reembed_null.py repairs it later; otherwise the body changes but the dense recall arm
            # keeps matching this note for its OLD topic, with no repair path (reembed_null only fixes NULLs).
            sets.append("embedding=NULL"); sets.append("embed_model=NULL")
            app.logger.warning("curate_edit: re-embed failed for %s; embedding NULLed for reembed", mid)
    if ("name" in fields) or ("body" in fields):                                    # re-sign on content change (else /memory/verify false-TAMPER)
        _en = fields["name"] if "name" in fields else _cur_row[0]
        _eb = fields["body"] if "body" in fields else _cur_row[1]
        _sig, _kid = sign_memory(_en, _eb, _cur_row[2], _cur_row[3])
        sets.append("signature=%s"); params.append(_sig)
        sets.append("sig_key_id=%s"); params.append(_kid)
        sets.append("content_hash=%s"); params.append(compute_content_hash(_en, _eb))  # canonical, re-hash on name OR body change
    params.append(mid)
    try:
        cur.execute("UPDATE memory SET " + ", ".join(sets) + " WHERE id=%s AND deleted_at IS NULL RETURNING name", params)
        nm = cur.fetchone()
        if fields.get("body") is not None and "body" in fields:      # resync [[ref]] edges to the amended body ( pattern)
            try:
                cur.execute("SAVEPOINT expl_refs_c")
                AL_apply.resync_explicit_refs(cur, mid, fields["body"])
                cur.execute("RELEASE SAVEPOINT expl_refs_c")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT expl_refs_c")
        log(cur, a["name"], "curate_edit", "memory", mid, {"fields": sorted(fields.keys())})
        conn.commit()
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("curate failed", 500, e)
    cur.close(); conn.close()
    return jsonify(ok=True, id=mid, name=nm[0] if nm else None, edited=sorted(fields.keys()))


@app.post("/curate/memory/<mid>/delete")
def curate_memory_delete(mid):
    """Soft-delete ANY live memory (manager/approver only, AUDITED) — the sanctioned prune
    path for trusted/semantic notes that brain_validate_memory (untrusted-personal only) and
    brain_revoke (agent kill-switch) can't reach. Sets deleted_at+invalid_at (reversible
    by nulling them); recall stops surfacing it and its edges drop out (the relation queries
    already filter deleted_at IS NULL). A non-empty reason is required and is written to the
    action log. Complements curate_get/curate_edit; mirrors the auto-retire UPDATE but
    by explicit id/name."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    b = request.get_json(silent=True) or {}
    reason = (b.get("reason") or "").strip()
    if not reason:
        return jsonify(error="a non-empty 'reason' is required (audited)"), 400
    conn = db(); cur = conn.cursor()
    rid = _curate_resolve(cur, mid)
    if not rid:
        cur.close(); conn.close()
        return jsonify(error="not found"), 404
    try:
        cur.execute("UPDATE memory SET deleted_at=now(), invalid_at=now(), updated_at=now() "
                    "WHERE id=%s AND deleted_at IS NULL RETURNING name", [rid])
        row = cur.fetchone()
        if not row:                          # lost a race, already gone
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="not found"), 404
        log(cur, a["name"], "curate_delete", "memory", rid, {"reason": reason, "name": row[0]})
        conn.commit()
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("curate delete failed", 500, e)
    cur.close(); conn.close()
    return jsonify(ok=True, id=rid, name=row[0], deleted=True)


# ---------------------------------------------------------------------------
# Provisional TABLES ( step 2). An agent creates a table for structured data
# DIRECTLY, but via a STRUCTURED spec (never raw SQL): the API validates names/types
# and builds the DDL into an isolated `provisional` schema, author-only. Each table is
# ANCHORED to a provisional memory — graduate the memory -> the table is promoted to the
# governed schema; delete/expire the memory -> the table is cascade-dropped.
_IDENT = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_RESERVED_COLS = {"id", "author_body", "anchor_memory_id", "created_at"}
# input type -> canonical SQL type (allowlist; anything else is rejected)
_COL_TYPES = {
    "text": "text", "int": "integer", "integer": "integer", "bigint": "bigint",
    "bool": "boolean", "boolean": "boolean", "numeric": "numeric", "float": "double precision",
    "double": "double precision", "date": "date", "timestamptz": "timestamptz", "text[]": "text[]",
}


def _safe_ident(s):
    if not isinstance(s, str) or not _IDENT.match(s):
        raise ValueError("bad identifier: %r" % s)
    return s


def _validate_columns(cols):
    """cols = [{name,type}]. Returns a validated [(name, sqltype)] or raises ValueError."""
    if not isinstance(cols, list) or not (1 <= len(cols) <= 24):
        raise ValueError("columns must be a list of 1..24 {name,type}")
    seen, out = set(), []
    for c in cols:
        nm = _safe_ident((c or {}).get("name", ""))
        if nm in _RESERVED_COLS or nm in seen:
            raise ValueError("reserved or duplicate column: %s" % nm)
        seen.add(nm)
        t = str((c or {}).get("type", "")).strip().lower()
        if t not in _COL_TYPES:
            raise ValueError("unsupported type %r (allowed: %s)" % (t, ", ".join(sorted(_COL_TYPES))))
        out.append((nm, _COL_TYPES[t]))
    return out


def _drop_or_graduate_anchor_tables(cur, anchor_id, action, by, readers=None):
    """Cascade an anchor memory's decision onto its provisional tables.
    action='drop' -> DROP the sandbox table, status='deleted'.
    action='graduate' -> move it to the governed schema + add access cols, status='graduated'."""
    cur.execute("SELECT id, object_name FROM provisional_artifact "
                "WHERE anchor_memory_id=%s AND status='provisional'", [anchor_id])
    for art_id, obj in cur.fetchall():
        if not re.match(r"^[a-z][a-z0-9_]{1,80}$", obj):   # defensive: object_name is a safe identifier
            continue
        if action == "drop":
            cur.execute('DROP TABLE IF EXISTS provisional."%s"' % obj)
            cur.execute("UPDATE provisional_artifact SET status='deleted' WHERE id=%s", [art_id])
            log(cur, by, "provisional_table_drop", "table", str(art_id), {"object": obj})
        else:  # graduate: promote into the governed (public) schema + access columns
            cur.execute('ALTER TABLE provisional."%s" SET SCHEMA public' % obj)
            cur.execute('ALTER TABLE public."%s" ADD COLUMN IF NOT EXISTS readers text[] NOT NULL DEFAULT %%s, '
                        'ADD COLUMN IF NOT EXISTS sensitivity text NOT NULL DEFAULT %%s' % obj,
                        [readers or ["common"], "normal"])
            cur.execute("UPDATE provisional_artifact SET status='graduated' WHERE id=%s", [art_id])
            log(cur, by, "provisional_table_graduate", "table", str(art_id), {"object": obj, "readers": readers or ["common"]})


@app.post("/provisional/table")
def provisional_table_create():
    """Create a sandbox table for structured data, anchored to a provisional memory.
    Body: {name, columns:[{name,type}], description?, anchor_memory_id?}. If no anchor is
    given, a provisional anchor memory is created automatically (the 'memory they created')."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "worker"):
        return jsonify(error="only manager/worker agents may create provisional tables"), 403
    b = request.get_json(silent=True) or {}
    try:
        name = _safe_ident((b.get("name") or "").lower())
        cols = _validate_columns(b.get("columns"))
        author = _safe_ident(a["name"])
    except ValueError as e:
        return jsonify(error=str(e)), 400
    obj = "%s__%s" % (author, name)
    conn = db(); cur = conn.cursor()
    # anchor: use the caller's existing provisional memory, or auto-create one
    anchor = b.get("anchor_memory_id")
    if anchor:
        cur.execute("SELECT expires_at FROM memory WHERE id=%s AND author_body=%s AND mem_tier='provisional' "
                    "AND deleted_at IS NULL", [anchor, a["name"]])
        r = cur.fetchone()
        if not r:
            cur.close(); conn.close()
            return jsonify(error="anchor_memory_id not found / not your live provisional memory"), 404
        anchor_exp = r[0]
    else:
        desc = b.get("description") or ("Provisional table '%s'" % name)
        body_txt = "Provisional table '%s' (columns: %s). %s" % (
            name, ", ".join("%s:%s" % (n, t) for n, t in cols), desc)
        chash = compute_content_hash("prov_table_%s" % name, body_txt)   # canonical (was agent+body)
        cur.execute("INSERT INTO memory(name,mtype,mem_tier,share_status,description,body,readers,sensitivity,"
                    "origin_channel,trust,author_body,content_hash,expires_at) VALUES "
                    "(%s,'reference','provisional','personal',%s,%s,ARRAY[%s],'normal','agent-reasoning','quarantined',"
                    "%s,%s,now()+interval %s) RETURNING id, expires_at",
                    ("prov_table_%s" % name, desc, body_txt, a["name"], a["name"], chash,
                     "%d days" % cfg("PROVISIONAL_TTL_DAYS")))   # live-tunable TTL
        ar = cur.fetchone(); anchor, anchor_exp = str(ar[0]), ar[1]
    # build + run the validated DDL in the sandbox schema
    coldefs = ", ".join('"%s" %s' % (n, t) for n, t in cols)
    ddl = ('CREATE TABLE provisional."%s" (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, %s, '
           'author_body text NOT NULL DEFAULT %%s, anchor_memory_id uuid NOT NULL, '
           'created_at timestamptz NOT NULL DEFAULT now())' % (obj, coldefs))
    try:
        cur.execute(ddl, [a["name"]])
    except psycopg2.errors.DuplicateTable:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="you already have a provisional table named '%s'" % name), 409
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("create failed", 400, e)
    cur.execute("INSERT INTO provisional_artifact(author_body,object_name,display_name,columns_spec,"
                "anchor_memory_id,expires_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (a["name"], obj, name, psycopg2.extras.Json([{"name": n, "type": t} for n, t in cols]),
                 anchor, anchor_exp))
    art_id = cur.fetchone()[0]
    log(cur, a["name"], "provisional_table_create", "table", str(art_id), {"object": obj, "anchor": anchor})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, table=name, object_name=obj, anchor_memory_id=anchor, expires_at=_iso(anchor_exp),
                   columns=[{"name": n, "type": t} for n, t in cols],
                   note="author-only sandbox table; graduates if its anchor memory is approved, else dropped with it")


def _my_artifact(cur, name, author):
    cur.execute("SELECT id, object_name, columns_spec FROM provisional_artifact "
                "WHERE display_name=%s AND author_body=%s AND status='provisional'", [name, author])
    return cur.fetchone()


@app.post("/provisional/table/<name>/rows")
def provisional_table_insert(name):
    """Insert structured rows into your own provisional table. Body: {rows:[{col:val}]}.
    Column names are taken from the registered spec (validated); values are parameterized."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    try:
        name = _safe_ident(name.lower())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    b = request.get_json(silent=True) or {}
    rows = b.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify(error="rows must be a non-empty list of objects"), 400
    conn = db(); cur = conn.cursor()
    art = _my_artifact(cur, name, a["name"])
    if not art:
        cur.close(); conn.close()
        return jsonify(error="no live provisional table '%s' of yours" % name), 404
    art_id, obj, spec = art
    allowed = {c["name"] for c in spec}
    cur.execute("SELECT anchor_memory_id FROM provisional_artifact WHERE id=%s", [art_id])
    anchor_id = cur.fetchone()[0]
    inserted = 0
    try:
        for row in rows:
            cols = [c for c in (row or {}) if c in allowed]
            if not cols:
                continue
            collist = ", ".join('"%s"' % c for c in cols) + ', author_body, anchor_memory_id'
            ph = ", ".join(["%s"] * len(cols)) + ", %s, %s"
            vals = [row[c] for c in cols] + [a["name"], anchor_id]
            cur.execute('INSERT INTO provisional."%s" (%s) VALUES (%s)' % (obj, collist, ph), vals)
            inserted += 1
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("insert failed", 400, e)
    log(cur, a["name"], "provisional_table_insert", "table", str(art_id), {"object": obj, "rows": inserted})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, table=name, inserted=inserted)


@app.get("/provisional/tables")
def provisional_tables_list():
    """List YOUR own live provisional tables (cols, anchor, row count, TTL)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,display_name,object_name,columns_spec,anchor_memory_id,status,created_at,expires_at "
                "FROM provisional_artifact WHERE author_body=%s AND status='provisional' ORDER BY created_at DESC",
                [a["name"]])
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        try:
            c2 = conn.cursor(); c2.execute('SELECT count(*) FROM provisional."%s"' % r["object_name"]); r["row_count"] = c2.fetchone()[0]; c2.close()
        except Exception:
            r["row_count"] = None
        r["anchor_memory_id"] = str(r["anchor_memory_id"])
        r["created_at"] = _iso(r.get("created_at")); r["expires_at"] = _iso(r.get("expires_at"))
    cur.close(); conn.close()
    return jsonify(count=len(rows), tables=rows)


@app.get("/provisional/table/<name>")
def provisional_table_read(name):
    """Read rows from your own provisional table (latest 200)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    try:
        name = _safe_ident(name.lower())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    conn = db(); cur = conn.cursor()
    art = _my_artifact(cur, name, a["name"])
    if not art:
        cur.close(); conn.close()
        return jsonify(error="no live provisional table '%s' of yours" % name), 404
    _, obj, _ = art
    c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c2.execute('SELECT * FROM provisional."%s" ORDER BY id DESC LIMIT 200' % obj)
    out = [dict(r) for r in c2.fetchall()]
    for r in out:
        for k, v in list(r.items()):
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
            elif str(type(v)) == "<class 'uuid.UUID'>":
                r[k] = str(v)
    c2.close(); cur.close(); conn.close()
    return jsonify(table=name, count=len(out), rows=out)


# ---- memory attachments: attach a file/image/blob to a memory; recall surfaces it ----
# Per-attachment size ceiling resolves live via cfg("ATTACH_MAX_MB") at the upload site.
_ATTACH_KINDS = ("file", "image", "blob")


def _visible_memory(cur, agent, mid):
    """Return the memory row (id, author_body) for mid IF it is live AND visible to `agent`
    (mem_read_where — the same tier/reader/share gate recall uses), else None. This is the ONE
    gate for attachments: an attachment is reachable iff its anchor memory is."""
    where, params = mem_read_where(agent)
    try:
        cur.execute("SELECT id, author_body FROM memory WHERE id=%s AND " + where, [mid] + params)
    except Exception:
        return None            # bad uuid etc. -> treat as not found
    return cur.fetchone()


@app.post("/memory/<mid>/attach")
def memory_attach(mid):
    """Attach a file/image/blob to a memory. Body: {filename, data_b64, content_type?,
    caption?, kind?}. Only the memory's author or a manager may attach. The blob is stored as
    bytea (<= ATTACH_MAX_BYTES); later access is gated by the anchor memory's visibility."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    b = request.get_json(silent=True) or {}
    filename = (b.get("filename") or "").strip()
    data_b64 = b.get("data_b64") or ""
    if not filename or not data_b64:
        return jsonify(error="filename and data_b64 are required"), 400
    kind = (b.get("kind") or "file").strip().lower()
    if kind not in _ATTACH_KINDS:
        return jsonify(error="kind must be one of %s" % (_ATTACH_KINDS,)), 400
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return jsonify(error="data_b64 is not valid base64"), 400
    if not raw:
        return jsonify(error="empty attachment"), 400
    max_bytes = cfg("ATTACH_MAX_MB") * 1024 * 1024        # live-tunable
    if len(raw) > max_bytes:
        return jsonify(error="attachment too large (%d bytes; max %d)" % (len(raw), max_bytes)), 413
    conn = db(); cur = conn.cursor()
    mem = _visible_memory(cur, a, mid)
    if not mem:
        cur.close(); conn.close()
        return jsonify(error="memory not found or not visible to you"), 404
    mem_id, mem_author = mem[0], mem[1]
    if a["role"] != "manager" and mem_author != a["name"]:
        cur.close(); conn.close()
        return jsonify(error="only the memory's author or a manager may attach"), 403
    sha = hashlib.sha256(raw).hexdigest()
    ctype = ((b.get("content_type") or "application/octet-stream").strip() or "application/octet-stream")[:128]
    caption = (b.get("caption") or "").strip() or None
    cur.execute("INSERT INTO memory_attachment(anchor_memory_id,kind,filename,content_type,byte_size,"
                "sha256,caption,content,author_body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                [str(mem_id), kind, filename[:256], ctype, len(raw), sha, caption,
                 psycopg2.Binary(raw), a["name"]])
    aid = cur.fetchone()[0]
    log(cur, a["name"], "memory_attach", "attachment", str(aid),
        {"memory": str(mem_id), "filename": filename[:256], "bytes": len(raw), "kind": kind})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, attachment_id=str(aid), memory_id=str(mem_id), filename=filename[:256],
                   kind=kind, content_type=ctype, byte_size=len(raw), sha256=sha)


@app.get("/memory/<mid>/attachments")
def memory_attachments(mid):
    """List a memory's attachments (metadata only — no blob). Gated by the memory's visibility."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor()
    mem = _visible_memory(cur, a, mid)
    if not mem:
        cur.close(); conn.close()
        return jsonify(error="memory not found or not visible to you"), 404
    c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c2.execute("SELECT id,kind,filename,content_type,byte_size,sha256,caption,created_at "
               "FROM memory_attachment WHERE anchor_memory_id=%s AND deleted_at IS NULL ORDER BY created_at",
               [str(mem[0])])
    out = []
    for r in c2.fetchall():
        d = dict(r); d["id"] = str(d["id"]); d["created_at"] = _iso(d.get("created_at"))
        out.append(d)
    c2.close(); cur.close(); conn.close()
    return jsonify(memory_id=str(mem[0]), count=len(out), attachments=out)


@app.get("/attachment/<aid>")
def attachment_get(aid):
    """Fetch one attachment's bytes (base64 in data_b64) + metadata. Access is gated by the ANCHOR
    memory's visibility, so an attachment is reachable iff its memory is."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT anchor_memory_id FROM memory_attachment WHERE id=%s AND deleted_at IS NULL", [aid])
    except Exception:
        cur.close(); conn.close()
        return jsonify(error="attachment not found"), 404
    row = cur.fetchone()
    if not row or not _visible_memory(cur, a, str(row[0])):
        cur.close(); conn.close()
        return jsonify(error="attachment not found or not visible to you"), 404
    cur.execute("SELECT filename,content_type,byte_size,sha256,kind,caption,content,anchor_memory_id "
                "FROM memory_attachment WHERE id=%s", [aid])
    r = cur.fetchone(); cur.close(); conn.close()
    data_b64 = base64.b64encode(bytes(r[6])).decode("ascii")
    return jsonify(attachment_id=aid, filename=r[0], content_type=r[1], byte_size=r[2], sha256=r[3],
                   kind=r[4], caption=r[5], memory_id=str(r[7]), data_b64=data_b64)


@app.delete("/attachment/<aid>")
def attachment_delete(aid):
    """Soft-delete an attachment. Only its author or a manager, and only if the anchor is visible."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT anchor_memory_id, author_body FROM memory_attachment "
                    "WHERE id=%s AND deleted_at IS NULL", [aid])
    except Exception:
        cur.close(); conn.close()
        return jsonify(error="attachment not found"), 404
    row = cur.fetchone()
    if not row or not _visible_memory(cur, a, str(row[0])):
        cur.close(); conn.close()
        return jsonify(error="attachment not found or not visible to you"), 404
    if a["role"] != "manager" and row[1] != a["name"]:
        cur.close(); conn.close()
        return jsonify(error="only the attachment's author or a manager may delete it"), 403
    cur.execute("UPDATE memory_attachment SET deleted_at=now() WHERE id=%s", [aid])
    log(cur, a["name"], "attachment_delete", "attachment", aid, {"memory": str(row[0])})
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, deleted=aid)


# ---- infra model: the brain's canonical host/service/link STRUCTURE ----
# Live up/down + the physical device tree stay in LibreNMS; the dashboard overlays them onto this.
@app.get("/infra/model")
def infra_model():
    """Return the whole infra model (hosts, services, links). Any authenticated agent may read it."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT name,kind,display,ip,mac,parent_host,librenms_hostname,location,anchor_memory,notes "
                "FROM infra_host ORDER BY kind, name")
    hosts = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT name,label,host,ip,port,url,grp,container_id,ha,anchor_memory,description "
                "FROM infra_service ORDER BY grp, name")
    services = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT src,dst,rel,notes FROM infra_link ORDER BY src, dst")
    links = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(hosts=hosts, services=services, links=links,
                   counts={"hosts": len(hosts), "services": len(services), "links": len(links)})


@app.post("/infra/upsert")
def infra_upsert():
    """Create/update one infra-model row (MANAGER only). Body: {entity:'host'|'service'|'link', ...columns}.
    Upserts on the natural key (host/service name, or link src+dst+rel)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="only managers may edit the infra model"), 403
    b = request.get_json(silent=True) or {}
    entity = (b.get("entity") or "").lower()
    conn = db(); cur = conn.cursor()

    def _host_exists(ref):
        cur.execute("SELECT 1 FROM infra_host WHERE name=%s", (ref,)); return cur.fetchone() is not None

    def _node_exists(ref):   # a link endpoint may be a host OR a service
        cur.execute("SELECT 1 FROM infra_host WHERE name=%s UNION ALL SELECT 1 FROM infra_service WHERE name=%s",
                    (ref, ref)); return cur.fetchone() is not None

    try:
        if entity == "host":
            #/A3-3: validate references so a typo can't silently create a dangling row.
            if not (b.get("name") or "").strip():
                raise ValueError("host name required")
            if b.get("parent_host") and not _host_exists(b["parent_host"]):
                raise ValueError("parent_host '%s' does not exist — create it first" % b["parent_host"])
            cur.execute(
                "INSERT INTO infra_host(name,kind,display,ip,mac,parent_host,librenms_hostname,location,anchor_memory,notes) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(name) DO UPDATE SET "
                "kind=EXCLUDED.kind,display=EXCLUDED.display,ip=EXCLUDED.ip,mac=EXCLUDED.mac,parent_host=EXCLUDED.parent_host,"
                "librenms_hostname=EXCLUDED.librenms_hostname,location=EXCLUDED.location,anchor_memory=EXCLUDED.anchor_memory,"
                "notes=EXCLUDED.notes,updated_at=now()",
                (b.get("name"), b.get("kind") or "lxc", b.get("display"), b.get("ip"), b.get("mac"),
                 b.get("parent_host"), b.get("librenms_hostname"), b.get("location"), b.get("anchor_memory"), b.get("notes")))
        elif entity == "service":
            if not (b.get("name") or "").strip():
                raise ValueError("service name required")
            if b.get("host") and not _host_exists(b["host"]):
                raise ValueError("host '%s' does not exist — create it first" % b["host"])
            cur.execute(
                "INSERT INTO infra_service(name,label,host,ip,port,url,grp,container_id,ha,anchor_memory,description) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(name) DO UPDATE SET "
                "label=EXCLUDED.label,host=EXCLUDED.host,ip=EXCLUDED.ip,port=EXCLUDED.port,url=EXCLUDED.url,grp=EXCLUDED.grp,"
                "container_id=EXCLUDED.container_id,ha=EXCLUDED.ha,anchor_memory=EXCLUDED.anchor_memory,"
                "description=EXCLUDED.description,updated_at=now()",
                (b.get("name"), b.get("label"), b.get("host"), b.get("ip"), b.get("port"), b.get("url"),
                 b.get("grp"), b.get("container_id"), bool(b.get("ha")), b.get("anchor_memory"), b.get("description")))
        elif entity == "link":
            if not b.get("src") or not b.get("dst"):
                raise ValueError("link requires src and dst")
            for ref in (b["src"], b["dst"]):
                if not _node_exists(ref):
                    raise ValueError("link endpoint '%s' does not exist — create the host/service first" % ref)
            cur.execute(
                "INSERT INTO infra_link(src,dst,rel,notes) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(src,dst,rel) DO UPDATE SET notes=EXCLUDED.notes",
                (b.get("src"), b.get("dst"), b.get("rel") or "depends_on", b.get("notes")))
        else:
            cur.close(); conn.close()
            return jsonify(error="entity must be host|service|link"), 400
    except ValueError as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error=str(e)), 400
    except Exception:
        conn.rollback(); cur.close(); conn.close()
        app.logger.exception("infra_upsert failed")          #/A3-10: keep the raw DB text server-side
        return jsonify(error="infra upsert failed"), 400
    tid = b.get("name") or ("%s->%s" % (b.get("src"), b.get("dst")))
    log(cur, a["name"], "infra_upsert", entity, tid, None)
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, entity=entity, id=tid)


@app.post("/infra/delete")
def infra_delete():
    """Delete ONE infra-model row (MANAGER only). Body: {entity:'host'|'service'|'link', ...key}./A3-6: infra_upsert had no delete twin — a rename orphaned the old row. Refuses to orphan:
    deleting a host/service still referenced by a link (or a host still parenting another row)
    returns 409 with the reference count, mirroring the project-delete guard. 404 if nothing matched."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="only managers may edit the infra model"), 403
    b = request.get_json(silent=True) or {}
    entity = (b.get("entity") or "").lower()
    conn = db(); cur = conn.cursor()
    try:
        if entity in ("host", "service"):
            name = (b.get("name") or "").strip()
            if not name:
                raise ValueError("name required")
            cur.execute("SELECT count(*) FROM infra_link WHERE src=%s OR dst=%s", (name, name))
            refs = cur.fetchone()[0]
            if entity == "host":
                cur.execute("SELECT count(*) FROM infra_service WHERE host=%s", (name,))
                refs += cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM infra_host WHERE parent_host=%s", (name,))
                refs += cur.fetchone()[0]
            if refs:
                conn.rollback(); cur.close(); conn.close()
                return jsonify(error="'%s' is still referenced by %d infra row(s) — remove those first" % (name, refs)), 409
            cur.execute("DELETE FROM %s WHERE name=%%s" % ("infra_host" if entity == "host" else "infra_service"), (name,))
        elif entity == "link":
            if not b.get("src") or not b.get("dst"):
                raise ValueError("link requires src and dst")
            cur.execute("DELETE FROM infra_link WHERE src=%s AND dst=%s AND rel=%s",
                        (b["src"], b["dst"], b.get("rel") or "depends_on"))
        else:
            cur.close(); conn.close()
            return jsonify(error="entity must be host|service|link"), 400
        if cur.rowcount == 0:
            conn.rollback(); cur.close(); conn.close()
            return jsonify(error="no matching %s row to delete" % entity), 404
    except ValueError as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error=str(e)), 400
    except Exception:
        conn.rollback(); cur.close(); conn.close()
        app.logger.exception("infra_delete failed")
        return jsonify(error="infra delete failed"), 400
    tid = b.get("name") or ("%s->%s" % (b.get("src"), b.get("dst")))
    log(cur, a["name"], "infra_delete", entity, tid, None)
    conn.commit(); cur.close(); conn.close()
    return jsonify(ok=True, entity=entity, id=tid, deleted=True)


# ---------------------------------------------------------------------------
# — edge-type REVIEW queue. classify_edges.py proposes the infra relation types
# (accessed_via/runs_on/depends_on/uses) into memory_relation.proposed_type when they
# pass grounding+citation+verify; the edge stays rel_type='relates_to' until a manager
# approves. Approve -> apply the type (with the same unique-key weight-merge as auto-promotion);
# reject -> clear the proposal, leave relates_to (classified_at stays set so it isn't re-proposed).
@app.get("/graph/edge-proposals")
def edge_proposals():
    """Manager review: the queued infra edge-type proposals awaiting approve/reject. Each carries the
    source note, target note, the proposed relation, and the grounding quote that earned it."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT r.id, r.src_id, r.dst_id, r.proposed_type, r.proposed_quote, r.proposed_at, "
                "s.name AS src_name, d.name AS dst_name "
                "FROM memory_relation r JOIN memory s ON s.id=r.src_id JOIN memory d ON d.id=r.dst_id "
                "WHERE r.proposed_type IS NOT NULL AND r.rel_type='relates_to' "
                "ORDER BY r.proposed_at")
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        d["id"] = str(d["id"]); d["src_id"] = str(d["src_id"]); d["dst_id"] = str(d["dst_id"])
        d["proposed_at"] = _iso(d.get("proposed_at"))
        rows.append(d)
    cur.close(); conn.close()
    return jsonify(count=len(rows), proposals=rows)


@app.post("/graph/edge-proposals/decide")
def edge_proposal_decide():
    """Approve or reject a queued edge-type proposal (manager/approver only). Body: {edge_id,
    decision:'approve'|'reject'}. approve -> set rel_type=proposed_type (merge on the (src,dst,type)
    unique key, folding weight); reject -> clear the proposal, keep relates_to."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    b = request.get_json(silent=True) or {}
    eid = b.get("edge_id"); decision = b.get("decision")
    if decision not in ("approve", "reject"):
        return jsonify(error="decision must be 'approve' or 'reject'"), 400
    if not eid:
        return jsonify(error="edge_id required"), 400
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("SELECT src_id, dst_id, weight, proposed_type FROM memory_relation "
                    "WHERE id=%s AND proposed_type IS NOT NULL AND rel_type='relates_to'", [eid])
        row = cur.fetchone()
    except Exception:
        conn.rollback(); cur.close(); conn.close()
        return jsonify(error="bad edge_id"), 400
    if not row:
        cur.close(); conn.close()
        return jsonify(error="not found or not a pending proposal"), 404
    src_id, dst_id, weight, ptype = row
    try:
        if decision == "reject":
            cur.execute("UPDATE memory_relation SET proposed_type=NULL, proposed_quote=NULL, "
                        "proposed_at=NULL, updated_at=now() WHERE id=%s", [eid])
            outcome = "rejected"
        else:                                          # approve -> apply the type (unique-key merge)
            try:
                cur.execute("SAVEPOINT ap")
                cur.execute("UPDATE memory_relation SET rel_type=%s, proposed_type=NULL, "
                            "proposed_quote=NULL, proposed_at=NULL, updated_at=now() WHERE id=%s",
                            (ptype, eid))
                cur.execute("RELEASE SAVEPOINT ap")
                outcome = "approved"
            except psycopg2.errors.UniqueViolation:     # an edge of this type already exists -> merge
                cur.execute("ROLLBACK TO SAVEPOINT ap")
                cur.execute("UPDATE memory_relation SET weight=weight+%s, updated_at=now() "
                            "WHERE src_id=%s AND dst_id=%s AND rel_type=%s", (weight, src_id, dst_id, ptype))
                cur.execute("DELETE FROM memory_relation WHERE id=%s", [eid])
                outcome = "approved+merged"
        log(cur, a["name"], "edge_proposal_%s" % decision, "memory_relation", str(eid),
            {"type": ptype, "src": str(src_id), "dst": str(dst_id)})
        conn.commit()
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return _internal_err("decide failed", 500, e)
    cur.close(); conn.close()
    return jsonify(ok=True, edge_id=eid, decision=decision, outcome=outcome, type=ptype)


@app.get("/tags")
def list_tags():
    """List all tags in use with counts, over the memories the caller may read."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    where, params = mem_read_where(a)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT t, count(*) AS n FROM (SELECT unnest(tags) AS t FROM memory WHERE " + where +
                ") s GROUP BY t ORDER BY n DESC, t", params)
    out = [{"tag": r[0], "count": r[1]} for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(count=len(out), tags=out)


@app.get("/consolidation/candidates")
def consolidation_candidates():
    """ brain-health: near-DUPLICATE memory pairs (embedding cosine >= CONSOLIDATE_COSINE) over the
    LIVE trusted brain, most-similar first — SURFACED for a manager to merge/supersede by hand. Read-only:
    it never merges (never lose data). Exact pairwise scan (fine at this store size; off the hot path)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    limit = _clip_int(request.args.get("limit"), 50, 1, 500)
    thr = float(cfg("CONSOLIDATE_COSINE"))
    dist_max = 1.0 - thr                        # pgvector <=> is cosine DISTANCE (1 - similarity)
    conn = db(); cur = conn.cursor()
    cur.execute(
        "SELECT a.id, a.name, a.description, b.id, b.name, b.description, "
        "1 - (a.embedding <=> b.embedding) AS sim "
        "FROM memory a JOIN memory b ON a.id < b.id "
        "WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL "
        "AND a.deleted_at IS NULL AND b.deleted_at IS NULL "
        "AND a.share_status='trusted' AND b.share_status='trusted' "
        "AND (a.embedding <=> b.embedding) <= %s "
        "ORDER BY sim DESC LIMIT %s", [dist_max, limit])
    pairs = [{"a": {"id": str(r[0]), "name": r[1], "description": r[2]},
              "b": {"id": str(r[3]), "name": r[4], "description": r[5]},
              "sim": round(float(r[6]), 4)} for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(threshold=thr, count=len(pairs), pairs=pairs)


# ---- runtime config surface for the manager dashboard ----
@app.get("/config")
def config_get():
    """Manager-only. Every live-tunable knob with its effective value + where it came from
    (db override > brain.env > code default). Reads the config table DIRECTLY (not the cfg() cache)
    so it always reflects ground truth immediately after a PATCH."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):   # approver = the mTLS-gated dashboard; matches 0028 RLS
        return jsonify(error="manager/approver role required"), 403
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT key, value, updated_at, updated_by FROM config")
    dbrows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    cur.close(); conn.close()
    knobs = []
    for key in sorted(CONFIG_KNOBS):
        caster, default = CONFIG_KNOBS[key]
        dbv = dbrows.get(key)
        envv = os.environ.get(key)
        if dbv is not None:
            eff, src = dbv[0], "db"
        elif envv is not None:
            eff, src = envv, "env"
        else:
            eff, src = str(default), "default"
        knobs.append({
            "key": key, "type": caster.__name__, "effective": eff, "source": src,
            "db_value": dbv[0] if dbv else None, "env_value": envv, "default": str(default),
            "updated_at": dbv[1].isoformat() if dbv else None,
            "updated_by": dbv[2] if dbv else None,
        })
    return jsonify(knobs=knobs)


@app.patch("/config")
def config_patch():
    """Manager-only. Set or reset live knobs. Body: {"key": "...", "value": ...} or
    {"updates": {"KEY": value, ...}}; value=null RESETS a key (deletes the override -> falls back to
    env/default). Values are validated against the registry caster, the change is audit-logged, and
    the local cfg() cache is busted (other gunicorn workers pick it up within _CFG_TTL)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):   # approver = the mTLS-gated dashboard; matches 0028 RLS
        return jsonify(error="manager/approver role required"), 403
    body = request.get_json(force=True, silent=True) or {}
    updates = body.get("updates")
    if updates is None and "key" in body:
        updates = {body["key"]: body.get("value")}
    if not isinstance(updates, dict) or not updates:
        return jsonify(error='body must be {"key","value"} or {"updates":{...}}'), 400
    for key, val in updates.items():
        if key not in CONFIG_KNOBS:
            return jsonify(error="unknown config key: %s" % key), 400
        if val is not None:
            try:
                CONFIG_KNOBS[key][0](val)                  # validate castability
            except Exception:
                return jsonify(error="value for %s is not a valid %s: %r"
                               % (key, CONFIG_KNOBS[key][0].__name__, val)), 400
    conn = db(); cur = conn.cursor()
    applied = {}
    try:
        for key, val in updates.items():
            if val is None:
                cur.execute("DELETE FROM config WHERE key=%s", (key,))
                applied[key] = None
            else:
                cur.execute("INSERT INTO config(key,value,updated_by) VALUES (%s,%s,%s) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
                            "updated_at=now(), updated_by=EXCLUDED.updated_by",
                            (key, str(val), a["name"]))
                applied[key] = str(val)
        log(cur, a["name"], "config_patch", "config", None, {"updates": applied})
        conn.commit()
    except Exception:
        conn.rollback(); cur.close(); conn.close()
        app.logger.exception("config patch failed")
        return jsonify(error="config write failed"), 500
    cur.close(); conn.close()
    _cfg_cache["at"] = -1.0                                # bust this worker's cache; others within _CFG_TTL
    return jsonify(ok=True, applied=applied)


# ---------------------------------------------------------------------------
# — brain batch-job visibility. The 5 scheduled maintenance jobs run as systemd
# timers on THIS host (the brain host); the dashboard (the dashboard host) can't reach systemd, so expose
# read-only status here. Reading `systemctl show`/`is-enabled` needs NO privilege (any
# user can query system-unit state). CONTROL (start/enable/disable) is a SEPARATE, gated
# endpoint that needs a scoped sudoers grant — added only after the operator approves the grant.
JOB_UNITS = ["brain-classify-edges", "brain-retention-prune", "brain-reembed",
             "brain-memory-verify", "brain-golden-eval", "brain-derive-infra-edges",
             "brain-repair-orphans", "brain-vet-links"]


def _sysctl_show(unit, props):
    import subprocess
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", ",".join(props)],
                           capture_output=True, text=True, timeout=5)
        d = {}
        for ln in r.stdout.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1); d[k] = v
        return d
    except Exception:
        return {}


def _sysctl_enabled(unit):
    import subprocess
    try:
        r = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr or "").strip() or "?"
    except Exception:
        return "?"


@app.get("/sessions")
def sessions_list():
    """list ALL conversation sessions (the session table, not just memory-producing ones) so the
    dashboard can show every session as a graph node. approver/manager; access_where gates rows."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    where, params = access_where(a, temporal=False)      # session table has deleted_at, no invalid_at
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT source_session, agent_body, turn_count, started_at, title, project "
                "FROM session WHERE " + where + " ORDER BY started_at DESC NULLS LAST", params)
    rows = [{"sid": ss, "agent": ab, "turns": tc, "started": _iso(st), "title": ti, "project": pr}
            for ss, ab, tc, st, ti, pr in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(count=len(rows), sessions=rows)


@app.get("/jobs")
def jobs():
    """Read-only status of the brain's scheduled maintenance jobs (systemd timers on the brain host)."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    out = []
    for name in JOB_UNITS:
        s = _sysctl_show(name + ".service",
                         ["ActiveState", "SubState", "Result", "ExecMainStatus",
                          "ExecMainExitTimestamp", "Description"])
        t = _sysctl_show(name + ".timer",
                         ["ActiveState", "LastTriggerUSec", "NextElapseUSecRealtime"])
        active = s.get("ActiveState", "")
        result = s.get("Result", "")
        out.append({
            "name": name,
            "description": s.get("Description", ""),
            "active": active,                       # inactive = idle (good for a oneshot); activating = running
            "sub": s.get("SubState", ""),
            "result": result,                       # success / signal / exit-code
            "exit_status": s.get("ExecMainStatus", ""),
            "last_run": s.get("ExecMainExitTimestamp", ""),
            "timer_enabled": _sysctl_enabled(name + ".timer"),
            "timer_active": t.get("ActiveState", ""),
            "last_trigger": t.get("LastTriggerUSec", ""),
            "next_run": t.get("NextElapseUSecRealtime", ""),
            "failed": active == "failed" or (result not in ("success", "")),
        })
    return jsonify(jobs=out)


@app.post("/jobs/<name>/<action>")
def job_control(name, action):
    """GATED job control. run -> start the oneshot service; enable/disable -> its timer.
    Whitelisted three ways: role manager/approver, name in JOB_UNITS, action in {run,enable,disable};
    the actual privilege is a scoped /etc/sudoers.d/brain-jobs (only these exact systemctl+unit pairs,
    no stop/wildcard). Every call is audit-logged to action_log."""
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] not in ("manager", "approver"):
        return jsonify(error="manager/approver role required"), 403
    if name not in JOB_UNITS:
        return jsonify(error="unknown job"), 400
    if action not in ("run", "enable", "disable"):
        return jsonify(error="action must be run|enable|disable"), 400
    import subprocess
    unit = name + (".service" if action == "run" else ".timer")
    sub = "start" if action == "run" else action
    cmd = ["sudo", "-n", "systemctl", sub, unit]
    triggered = False; rc = None; errout = ""
    try:
        if action == "run":
            # `systemctl start` on a oneshot BLOCKS until the job finishes (minutes) — don't hang the
            # gunicorn worker. Probe briefly for an immediate auth/dispatch failure; if it's still
            # running after the probe, return 'triggered' and leave it (do NOT kill the job).
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                _, errout = p.communicate(timeout=4); rc = p.returncode
            except subprocess.TimeoutExpired:
                triggered = True; rc = 0
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            rc = r.returncode; errout = r.stderr or ""
    except Exception as e:
        app.logger.warning("job control failed: %s", e)
        return jsonify(ok=False, error="job control failed"), 500
    ok = rc == 0
    conn = db(); cur = conn.cursor()
    try:
        log(cur, a["name"], "job_" + action, "job", name,
            {"unit": unit, "rc": rc, "triggered": triggered, "stderr": (errout or "")[:300]})
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not ok:
        return jsonify(ok=False, error=(errout or "systemctl failed").strip()[:300]), 500
    return jsonify(ok=True, job=name, action=action, unit=unit, triggered=triggered)


@app.post("/session/<sid>/rebuild")
def session_rebuild(sid):
    """MANUAL, manager-only source-grounded rebuild of ONE session's memories.

    Runs `rebuild_session.py <sid> --apply` — the LLM re-extracts the session's durable memories +
    typed relations from its transcript and writes the NEW ones as trust='quarantined',
    share_status='personal' (the untrusted/needs-you pool; NOT auto-trusted) after dedup, plus the
    grounded typed edges. Nothing trusted is touched and it is fully reversible (tag
    'session-rebuild-t454'). Synchronous — one LLM extract fits inside the gunicorn 120s timeout.
    Manager-only (it WRITES); every call is audit-logged. Not a scheduled job (no timer)."""
    import re, subprocess
    a, err = authenticate()
    if err:
        return jsonify(error=err[0]), err[1]
    if a["role"] != "manager":
        return jsonify(error="manager role required"), 403
    rl = _rate_limit(a["name"], "rebuild", 6)   # heavy synchronous LLM rebuild
    if rl:
        return rl
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid or ""):     # sid is a uuid -> injection-safe charset
        return jsonify(error="bad session id"), 400
    # The brain-api service has no EnvironmentFile, so source brain.env for OLLAMA_GEN_URL/RERANK_MODEL
    # (embedding uses the search.py defaults). sid is charset-validated above so interpolation is safe.
    cmd = ["bash", "-c",
           "set -a; source /opt/brain-db/db/brain.env; set +a; "
           "exec python3 /opt/brain-db/db/rebuild_session.py " + sid + " --apply"]
    try:
        r = subprocess.run(cmd, cwd="/opt/brain-db/db", capture_output=True, text=True, timeout=115)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="rebuild timed out (>115s) — Ollama slow/wedged?"), 504
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    m = re.search(r"APPLIED:\s*(\d+) written,\s*(\d+) skipped\(dup\),\s*(\d+) edges", out)
    summary = ({"written": int(m.group(1)), "skipped": int(m.group(2)), "edges": int(m.group(3))}
               if m else None)
    ok = (r.returncode == 0)
    conn = db(); cur = conn.cursor()
    try:
        log(cur, a["name"], "session_rebuild", "session", sid, {"rc": r.returncode, "summary": summary})
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not ok:
        return jsonify(ok=False, sid=sid, error=out[-800:]), 500
    return jsonify(ok=True, sid=sid, summary=summary, output=out[-4000:])


@app.get("/metrics")
def metrics():
    """Prometheus exposition for the brain API. Additive + read-only, unauthenticated
    like /healthz; reachable only through the nginx mTLS gate."""
    from flask import Response
    out = []
    def emit(name, help_, value, labels=""):
        out.append("# HELP %s %s" % (name, help_))
        out.append("# TYPE %s gauge" % name)
        out.append("%s%s %s" % (name, labels, value))
    up = 1
    try:
        conn = db(); cur = conn.cursor()
        try:
            cur.execute("SELECT GREATEST(reltuples::bigint, 0) FROM pg_class WHERE relname = 'memory'")
            emit("brain_memory_total", "Total memories in the brain", cur.fetchone()[0])
        except Exception as e:
            app.logger.warning("metrics memory count failed: %s", e)
        try:
            cur.execute("SELECT count(*) FROM action_log")
            emit("brain_action_log_total", "Total action_log rows", cur.fetchone()[0])
        except Exception as e:
            app.logger.warning("metrics action_log count failed: %s", e)
        cur.close(); conn.close()
    except Exception as e:
        up = 0
        app.logger.warning("metrics db unavailable: %s", e)
    emit("brain_up", "Brain API up and DB reachable", up)
    emit("brain_build_info", "Brain API build info", 1, '{version="%s"}' % FLEETMEM_VERSION)
    return Response("\n".join(out) + "\n", mimetype="text/plain; version=0.0.4")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
