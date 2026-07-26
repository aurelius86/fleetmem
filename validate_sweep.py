#!/usr/bin/env python3
"""async validation backstop sweep (Approval 2.0 build 3/5) — the background half of the
validate-on-recall model (project_doc design-recall-validation). Runs on a daily systemd timer,
connecting to Postgres DIRECTLY (like run_memory_verify.py — a CLI job has no Flask request context).

Two jobs over the UNTRUSTED personal notes (share_status='personal', trust='quarantined'):

  1. CENSUS (always, read-only): classify each note by the state of its SOURCE transcript and log
     one memory_sweep_census action_log row. Buckets:
       - has_live_turns        : source_session -> a session row with >=1 live turn (validatable).
       - session_row_zero_turns: source_session -> a session row with ZERO live turns (transcript
                                  PRUNED away, tombstone kept) = the ONLY provable "source gone".
       - no_session_row        : source_session set but NO session row (NEVER ingested — NOT proof
                                  of pruning; left alone).
       - null_source           : no source_session at all (legacy/pre-; cannot be self-validated).

  2. SOURCE-GONE-DELETE (decision D4): soft-delete ONLY the `session_row_zero_turns` notes — the one
     class where the source demonstrably existed and is now gone. NEVER deletes no_session_row /
     null_source (can't prove pruned; a missing session row = never-ingested, not pruned). Gated by
     the VALIDATE_SWEEP_DELETE config knob (default 1). Each delete is audited
     (memory_source_gone_delete). This deletes 0 notes until session-turn retention prunes a
     transcript while keeping its session row as a tombstone.

The sweep NEVER self-trusts a note — validation (quarantined->trusted) is the recalling AGENT's job,
source-grounded; a server-side auto-validate would reintroduce the author==validator risk.
"""
import os
import psycopg2
import psycopg2.extras

DB = os.environ.get("PGDATABASE", "brain")

# The four source-states, as SQL predicates over a memory row `m`. Kept as one dict so the census and
# the delete agree on exactly what each bucket means (single source of truth).
_HAS_SESSION = ("EXISTS (SELECT 1 FROM session s WHERE s.source_session = m.source_session)")
_HAS_LIVE_TURNS = (
    "EXISTS (SELECT 1 FROM session s JOIN session_turn t ON t.session_id = s.id "
    "WHERE s.source_session = m.source_session)")
BUCKETS = {
    # source present + at least one turn -> validatable, leave it
    "has_live_turns":         "m.source_session IS NOT NULL AND m.source_session <> '' AND " + _HAS_LIVE_TURNS,
    # session row exists but no live turn -> transcript pruned (tombstone kept) = provable source-gone
    "session_row_zero_turns": ("m.source_session IS NOT NULL AND m.source_session <> '' AND "
                               + _HAS_SESSION + " AND NOT " + _HAS_LIVE_TURNS),
    # source_session set but no session row -> NEVER ingested (NOT proof of pruning) -> leave it
    "no_session_row":         ("m.source_session IS NOT NULL AND m.source_session <> '' AND NOT " + _HAS_SESSION),
    # no source at all -> legacy, cannot self-validate -> leave it ( backlog policy)
    "null_source":            "m.source_session IS NULL OR m.source_session = ''",
}
_UNTRUSTED = "m.share_status = 'personal' AND m.trust = 'quarantined' AND m.deleted_at IS NULL"


def _log(cur, actor, action, tkind, tid, detail):
    cur.execute("INSERT INTO action_log(actor,action,target_kind,target_id,detail) VALUES (%s,%s,%s,%s,%s)",
                (actor, action, tkind, tid, psycopg2.extras.Json(detail) if detail else None))


def _cfg_int(cur, key, default):
    """Read a live knob from the config table (config > default), matching api.cfg()'s precedence
    for the subset a CLI job needs. Fail-safe: any error / missing row -> default."""
    try:
        cur.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = cur.fetchone()
        return int(row["value"]) if row and row.get("value") is not None else default
    except Exception:
        return default


def census(cur):
    """Return {bucket: count} over the untrusted personal notes."""
    out = {}
    for name, pred in BUCKETS.items():
        cur.execute("SELECT count(*) AS n FROM memory m WHERE " + _UNTRUSTED + " AND (" + pred + ")")
        out[name] = cur.fetchone()["n"]
    return out


def main():
    conn = psycopg2.connect(dbname=DB, client_encoding="UTF8")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    counts = census(cur)
    _log(cur, "brain-guard", "memory_sweep_census", "system", None, counts)

    deleted = []
    if _cfg_int(cur, "VALIDATE_SWEEP_DELETE", 1):
        cur.execute("SELECT m.id, m.name, m.source_session FROM memory m WHERE " + _UNTRUSTED +
                    " AND (" + BUCKETS["session_row_zero_turns"] + ")")
        gone = cur.fetchall()
        for r in gone:
            cur.execute("UPDATE memory SET deleted_at=now(), invalid_at=now(), updated_at=now() WHERE id=%s", [r["id"]])
            _log(cur, "brain-guard", "memory_source_gone_delete", "memory", str(r["id"]),
                 {"name": r.get("name"), "source_session": r.get("source_session")})
            deleted.append(r.get("name") or str(r["id"]))

    # (Approval 2.0 step 2) TIME-CAP: an untrusted personal note still unconfirmed RECALL_VALIDATE_TTL_DAYS
    # after creation is soft-deleted (autolearn's weak-spot notes don't linger forever). 0 = OFF (deploy-inert).
    # Manager-trusted-at-birth notes are trust='trusted' -> already excluded by _UNTRUSTED. Audited + reversible.
    ttl_days = _cfg_int(cur, "RECALL_VALIDATE_TTL_DAYS", 0)
    ttl_deleted = []
    if ttl_days > 0:
        cur.execute("SELECT m.id, m.name FROM memory m WHERE " + _UNTRUSTED +
                    " AND m.created_at < now() - make_interval(days => %s)", [ttl_days])
        for r in cur.fetchall():
            cur.execute("UPDATE memory SET deleted_at=now(), invalid_at=now(), updated_at=now() WHERE id=%s", [r["id"]])
            _log(cur, "brain-guard", "memory_ttl_unconfirmed_delete", "memory", str(r["id"]),
                 {"name": r.get("name"), "ttl_days": ttl_days})
            ttl_deleted.append(r.get("name") or str(r["id"]))

    conn.commit(); cur.close(); conn.close()
    print("validate_sweep: census=%s source_gone_deleted=%d ttl_unconfirmed_deleted=%d"
          % (counts, len(deleted), len(ttl_deleted)))


if __name__ == "__main__":
    main()
