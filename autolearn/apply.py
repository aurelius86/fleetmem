"""Apply an approved proposal into the `memory` table (Big Step 6, the "apply step").

This is the seam api.py:464 promised but never built: a proposal becomes a real
memory row ONLY here, and ONLY for proposals already decided (auto-kept by the
deterministic gate, or human-approved via /approve). It never decides trust — the
gate (orchestrate.py + the API) does; apply just materializes the verdict.

Split so the security/shape logic is unit-testable with no DB and no network:
  * build_memory_row()  — pure: proposal dict -> the memory column values (no vector)
  * insert_sql()        — pure: (row, vec literal) -> (sql, params)
  * apply_proposal()    — executes: embed the body, INSERT, return the new memory id
                          (embed_fn / vec_fn injectable; real wiring uses search.embed)

Embedding reuses the pinned bge-m3 path (search.embed / search.vec_literal) — never
re-implemented here. embed_model is stamped from model-pin.json so a model swap is
detectable on rebuild (locked decision).
"""
import hashlib
import json
import os
import re
import time

import yaml

# Columns we write explicitly; `tsv` is GENERATED, `embedding` is cast ::vector.
_COLS = ["name", "mtype", "mem_tier", "share_status", "description", "body", "embedding", "embed_model",
         "readers", "sensitivity", "origin_channel", "trust", "author_body",
         "source_session", "content_hash", "tags"]

# mirrors the memory_mtype_check CHECK constraint (and api.py's _MTYPES/_norm_mtype from
#). The local LLM freely emits mtypes outside the vocabulary it was asked for
# (gotcha/decision/task/...); before this coercion those reached INSERT and violated the
# constraint, so the personal-write path threw and the capture fell through to the review queue
# instead of landing as a personal note (387 diversions in 24h, measured against the DB.
# added the coercion to api.py's propose + queue paths but not here, and the
# personal-write path calls apply_proposal with the raw candidate — this is that missed call site.
_MTYPES = ("user", "feedback", "project", "reference", "memory")


def norm_mtype(m):
    """Coerce an mtype to the memory_mtype_check allowlist; unknown/empty -> 'reference'."""
    return m if m in _MTYPES else "reference"

_PIN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model-pin.json")

# --- knowledge-graph config (box-ready) -----------------------------------------------
# The relation ontology + Ollama target live in graph.yaml so a new deployment is a config edit,
# not a code change (config-driven, brain-in-a-box). Missing file => the
# safe defaults below, so the write path never breaks on a fresh/partial install.
_GRAPH_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "graph.yaml")
_DEFAULT_ONTOLOGY = ["relates_to", "supersedes", "conflicts_with",
                     "accessed_via", "runs_on", "depends_on", "uses"]


def load_graph_cfg(path=_GRAPH_CFG_PATH):
    """Read graph.yaml (ontology types, default rel_type, Ollama endpoint/model). Returns {} on
    any failure — callers fall back to _DEFAULT_ONTOLOGY / 'relates_to' so nothing hard-depends
    on the file existing."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# resolve the ontology allowlist with a short TTL rather than once at import, so a graph.yaml
# edit (e.g. a domain type curated in from discover_ontology.py) becomes storable with NO service
# restart — mirrors the config-table cfg() live-reload. The allowlist lives ONLY in graph.yaml
# (single source); the DB keeps only a format guard (migration
# 0030). An unknown type falls back to DEFAULT_REL here — that IS the typo guard.
_ONT_TTL = 60.0
_ont_cache = {"types": None, "default": "relates_to", "at": 0.0}


def ontology():
    """Return (types_set, default_rel) from graph.yaml, re-read at most every _ONT_TTL seconds."""
    now = time.time()
    if _ont_cache["types"] is None or (now - _ont_cache["at"]) > _ONT_TTL:
        cfg = load_graph_cfg()
        ont = (cfg.get("ontology") or {})
        _ont_cache["types"] = set(ont.get("types") or _DEFAULT_ONTOLOGY)
        _ont_cache["default"] = ont.get("default") or "relates_to"
        _ont_cache["at"] = now
    return _ont_cache["types"], _ont_cache["default"]


def pinned_embed_model(path=_PIN_PATH):
    """Return the 'model@digest' tag for the pinned primary embedder, or None if the
    pin file is unreadable (caller may pass embed_model explicitly instead)."""
    try:
        with open(path) as f:
            pin = json.load(f)
        p = pin["primary"]
        return "%s@%s" % (p["model"].split(":")[0], p["ollama_digest_sha256"])
    except Exception:
        return None


def default_readers(sensitivity, share_status):
    """ + default reader-groups for a trusted memory that carries no explicit
    readers. A normal/public trusted note must reach the `common` group (else workers see
    nothing). A sensitive/secret note must NOT auto-widen to `common`/workers (safety floor +
    the governance rule) -> default to the two manager bodies
    (the "managers" group = secured). Non-trusted tiers keep [] (author/role-gated). Explicit readers win."""
    if share_status != "trusted":
        return []
    if (sensitivity or "normal") in ("sensitive", "secret"):
        return ["managers"]
    return ["common"]


def build_memory_row(proposal, *, trust=None, embed_model=None, share_status=None):
    """Pure: map a proposal (dict) to the memory column values. No embedding yet.

    `trust` override carries the decision's meaning:
      * human-approved (dashboard Keep / the operator) -> 'trusted' (a person validated it);
      * auto-keep                              -> the proposal's own (trusted) verdict.
    Falls back to the proposal's trust, else the table default 'quarantined'.
    `share_status` sets the lifecycle tier: the /approve path leaves it 'trusted';
    autolearn's personal-write passes 'personal'. Falls back to the proposal's, else 'trusted'.
    """
    body = (proposal.get("proposed_body") or proposal.get("body") or "").strip()
    name = proposal.get("name")
    chash = proposal.get("content_hash") or (
        hashlib.sha256(((name or "") + "\n" + body).encode("utf-8")).hexdigest() if body else None)
    _share = share_status or proposal.get("share_status") or "trusted"
    _readers = list(proposal.get("readers") or [])
    if not _readers:                                      # + default readers by sensitivity
        _readers = default_readers(proposal.get("sensitivity") or "normal", _share)
    return {
        "name": name,
        "mtype": norm_mtype(proposal.get("mtype")),          # allowlist, not just a default
        "mem_tier": proposal.get("mem_tier") or "semantic",
        "share_status": _share,
        "description": proposal.get("description") or "",
        "body": body,
        "embed_model": embed_model,
        "readers": _readers,
        "sensitivity": proposal.get("sensitivity") or "normal",
        "origin_channel": proposal.get("origin_channel") or "unknown",
        "trust": trust or proposal.get("trust") or "quarantined",
        "author_body": proposal.get("author_body"),
        "source_session": proposal.get("source_session"),
        "content_hash": chash,
        "tags": list(proposal.get("tags") or []),          # tag facet
    }


def insert_sql(row, vec_literal_str):
    """Pure: (memory row dict, pgvector literal string) -> (sql, params).
    The embedding placeholder is cast ::vector; tsv is computed by the column default."""
    placeholders = ", ".join("%s::vector" if c == "embedding" else "%s" for c in _COLS)
    sql = "INSERT INTO memory(%s) VALUES (%s) RETURNING id" % (", ".join(_COLS), placeholders)
    params = [vec_literal_str if c == "embedding" else row.get(c) for c in _COLS]
    return sql, params


def retire_prior(cur, name):
    """Find the LIVE memory currently named `name` and retire it WITH HISTORY:
    invalid_at=now() (it WAS valid until the correction) AND deleted_at=now() (recall filters
    on deleted_at, so it stops being recalled) — the row is kept, not erased, so the prior
    value stays auditable. Returns the retired memory id, or None if there was no prior.

    MUST run BEFORE inserting the replacement: memory_name_uniq is a partial unique index on
    name WHERE deleted_at IS NULL, so the old row's name has to be freed (soft-deleted) first
    or the new INSERT collides. Both happen in one transaction (proposal_decide rolls back on
    any error), so there is no window where the fact is gone."""
    if not name:
        return None
    cur.execute("SELECT id FROM memory WHERE name=%s AND deleted_at IS NULL "
                "ORDER BY created_at LIMIT 1", (name,))
    prior = cur.fetchone()
    if not prior:
        return None
    old_id = prior["id"] if isinstance(prior, dict) else prior[0]
    cur.execute("UPDATE memory SET invalid_at=now(), deleted_at=now(), updated_at=now() "
                "WHERE id=%s", (old_id,))
    return old_id


def record_supersedes(cur, new_id, old_id, *, sensitivity="normal", by="autolearn-apply"):
    """Record a `supersedes` edge new_id -> old_id (the graph + audit trail of what replaced
    what). No-op if either id is missing."""
    if not new_id or not old_id:
        return
    cur.execute("INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity) "
                "VALUES (%s,%s,'supersedes',%s,%s)", (new_id, old_id, by, sensitivity))


def log_session_recall(cur, session_id, memory_ids):
    """Append the memory ids a session RECALLED, keyed by the Claude Code per-chat session_id
    (migration 0008 /). Append-only; this is the source data for usage-based
    memory<->memory linking (link_usage). No-op without a session_id or ids — so non-MCP
    callers (session_id="") simply record nothing. Returns the count logged."""
    if not session_id or not memory_ids:
        return 0
    # ON CONFLICT DO NOTHING — one row per (session_id, memory_id) (migration 0027 unique).
    # A memory recalled repeatedly in a session no longer appends duplicate co-recall rows.
    cur.executemany("INSERT INTO session_recall(session_id, memory_id) VALUES (%s,%s) "
                    "ON CONFLICT (session_id, memory_id) DO NOTHING",
                    [(session_id, mid) for mid in memory_ids])
    return len(memory_ids)


def link_usage(cur, session_id, new_id, *, lookback=8, by="usage-link", sensitivity="normal"):
    """Link a freshly-created memory to the memories the SAME session most recently RECALLED
    ('relates_to') — the graph builds itself from real reasoning chains (recalled-then-created),
    not topical coincidence (migration 0008 /). Reads the most-recent distinct recalled
    ids for session_id, skips the self-link, and de-dups against existing relates_to edges.
    No-op if the session is unknown or recalled nothing. Returns the number of NEW edges."""
    if not session_id or not new_id:
        return 0
    cur.execute(
        "SELECT memory_id FROM ("
        " SELECT memory_id, max(recalled_at) AS mr FROM session_recall"
        " WHERE session_id=%s AND memory_id <> %s GROUP BY memory_id"
        ") t ORDER BY mr DESC LIMIT %s",
        (session_id, new_id, lookback))
    dst_ids = [(r["memory_id"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()]
    n = 0
    for rid in dst_ids:
        cur.execute(
            "INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity) "
            "SELECT %s,%s,'relates_to',%s,%s WHERE NOT EXISTS ("
            " SELECT 1 FROM memory_relation WHERE src_id=%s AND dst_id=%s AND rel_type='relates_to')",
            (new_id, rid, by, sensitivity, new_id, rid))
        if cur.rowcount and cur.rowcount > 0:
            n += cur.rowcount
    return n


# [[name]] reference matcher — identical to import_legacy.py's Obsidian-style parse so the
# on-write path and the one-time import agree on what counts as a link.
_LINK_RE = re.compile(r'\[\[([^\]|#]+)')

# Author-typed link matcher: [[name|rel_type]] captures the optional relation type after
# the pipe. Plain [[name]] leaves group(2) empty -> DEFAULT_REL, and the whole-note LLM pass
# (classify_edges.py) types it later. An unknown type falls back to DEFAULT_REL, never invents one.
_TYPED_LINK_RE = re.compile(r'\[\[([^\]|#]+)(?:\|([a-z][a-z0-9_]*))?')


def _norm_name(s):
    """Obsidian-style name normalisation (matches import_legacy.py): trim, lowercase,
    spaces/hyphens -> underscore. Lets hand-written [[hyphen-form]] links resolve to
    the underscored stem stored in memory.name."""
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def link_explicit_refs(cur, src_id, body, *, by="explicit-ref", sensitivity="normal"):
    """Parse [[name]] references out of a freshly-written memory body and create `relates_to`
    edges src_id -> referenced-memory id ( — grounds the wikilink graph in EXPLICIT
    citation, not authored 'vibes'). Resolves only to LIVE (deleted_at IS NULL) memories
    (memory_relation FK + gotcha #4), skips the self-link.

    TYPED + WEIGHTED: an author-typed [[name|rel_type]] creates that typed edge directly
    (captured at the source, no LLM); a plain [[name]] creates a DEFAULT_REL edge that the
    whole-note LLM pass types later. Unknown types fall back to DEFAULT_REL. Instead of a
    NOT-EXISTS skip, edges UPSERT on the (src,dst,rel_type) unique key: a repeat mention
    INCREMENTS weight rather than duplicating the row.

    SCOPE ('several brains'): the edge simply inherits its source — recall enforces
    privacy at read time by requiring the caller to read BOTH endpoints, so an edge FROM a
    personal note stays inside that author's personal brain. No-op on empty body / no matches.
    Returns the number of edges written/reinforced. Caller wraps this in a SAVEPOINT so a
    parse/insert error can never fail the parent memory write."""
    if not src_id or not body:
        return 0
    ONTOLOGY_TYPES, DEFAULT_REL = ontology()   # resolved per-call (TTL-cached) from graph.yaml
    # name -> rel_type. If the same link appears both plain and typed, the explicit type wins.
    want = {}
    for m in _TYPED_LINK_RE.finditer(body):
        nm = _norm_name(m.group(1))
        if not nm:
            continue
        rt = (m.group(2) or "").strip().lower()
        rt = rt if rt in ONTOLOGY_TYPES else DEFAULT_REL
        if nm not in want or want[nm] == DEFAULT_REL:
            want[nm] = rt
    if not want:
        return 0
    cur.execute(
        "SELECT id, lower(replace(replace(name,' ','_'),'-','_')) AS nn FROM memory "
        "WHERE deleted_at IS NULL AND id <> %s AND name IS NOT NULL "
        "AND lower(replace(replace(name,' ','_'),'-','_')) = ANY(%s)",
        (src_id, list(want.keys())))
    n = 0
    for r in cur.fetchall():
        rid = r["id"] if isinstance(r, dict) else r[0]
        nn = r["nn"] if isinstance(r, dict) else r[1]
        cur.execute(
            "INSERT INTO memory_relation(src_id,dst_id,rel_type,created_by,sensitivity,weight) "
            "VALUES (%s,%s,%s,%s,%s,1) "
            "ON CONFLICT (src_id,dst_id,rel_type) "
            "DO UPDATE SET weight = memory_relation.weight + 1, updated_at = now()",
            (src_id, rid, want.get(nn, DEFAULT_REL), by, sensitivity))
        if cur.rowcount:
            n += 1
    return n


def resync_explicit_refs(cur, src_id, body, *, by="explicit-ref"):
    """Amend-time: reconcile explicit-ref edges with an edited body — PRUNE edges whose
    [[name]] no longer appears, then (re)ADD edges for the current refs. Touches only
    created_by=by edges (any rel_type, since author links can now be typed —), so
    co-use/usage-link/supersedes and LLM-typed (created_by='graph-classifier') edges are left
    alone. Returns (pruned, added). Caller wraps in a SAVEPOINT (best-effort side-effect)."""
    names = {_norm_name(m) for m in _LINK_RE.findall(body or "")}
    names.discard("")
    cur.execute(
        "DELETE FROM memory_relation r USING memory d "
        "WHERE r.src_id=%s AND r.created_by=%s AND d.id=r.dst_id "
        "AND NOT (lower(replace(replace(d.name,' ','_'),'-','_')) = ANY(%s))",
        (src_id, by, list(names) or ['']))
    pruned = cur.rowcount or 0
    added = link_explicit_refs(cur, src_id, body, by=by)
    return pruned, added


def apply_proposal(cur, proposal, *, embed_fn, vec_fn, trust=None, embed_model=None):
    """Execute: embed the body via the pinned model, INSERT a memory row, return its id.
    `cur` is a live DB cursor; `embed_fn(text)->vec` and `vec_fn(vec)->literal` are
    injected (production = search.embed / search.vec_literal) so this stays testable.

    If the proposal's `name` already names a LIVE memory, the prior row is SUPERSEDED first
    (retired + a supersedes relation) so a correction REPLACES — never duplicates — the fact.
    A brand-new name just inserts (no prior to retire). This closes the edit gap: conflict.py
    escalates a same-name correction, and a human Keep lands here."""
    if embed_model is None:
        embed_model = pinned_embed_model()
    row = build_memory_row(proposal, trust=trust, embed_model=embed_model)
    if not row["body"]:
        raise ValueError("cannot apply a proposal with an empty body")
    # retire the prior FIRST so the partial-unique name index doesn't reject the new row
    old_id = retire_prior(cur, row["name"])
    vec_literal_str = vec_fn(embed_fn(row["description"] + "\n" + row["body"]))
    sql, params = insert_sql(row, vec_literal_str)
    cur.execute(sql, params)
    out = cur.fetchone()
    new_id = out["id"] if isinstance(out, dict) else out[0]
    record_supersedes(cur, new_id, old_id, sensitivity=row.get("sensitivity") or "normal",
                      by=row.get("author_body") or "autolearn-apply")
    return new_id

#/A2-14: insert_personal() removed — dead since (autolearn never auto-writes; it
#           only queues proposals). No caller remained; kept out to prevent accidental reuse.
