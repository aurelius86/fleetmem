#!/usr/bin/env python3
"""retention prune for the fast-growing log tables (the brain host, run on a a daily
systemd timer). Deletes rows past their retention window and logs a liveness row every run.
Windows are env-configurable (feeds the config layer); defaults keep 1y of audit + 6mo of
recall usage. On a ~1-month-old store this deletes 0 rows today — it just establishes the policy.

  ACTION_LOG_RETENTION_DAYS      (default 365)  action_log ~146k rows/yr
  SESSION_RECALL_RETENTION_DAYS  (default 180)  session_recall ~490 rows/day
  SESSION_TURN_RETENTION_DAYS    (default 365)  session_turn = ingested transcripts, grows unbounded
                                                on a public box. 0 = keep forever.
  ATTACHMENT_RETENTION_DAYS      (default 30)   memory_attachment blobs whose anchor memory has been
                                                soft-deleted (or the attachment itself) longer than
                                                this restore window. 0 = keep forever.
"""
import os
import psycopg2
import psycopg2.extras

DB = os.environ.get("PGDATABASE", "brain")
ACTION_LOG_DAYS = int(os.environ.get("ACTION_LOG_RETENTION_DAYS", "365"))
SESSION_RECALL_DAYS = int(os.environ.get("SESSION_RECALL_RETENTION_DAYS", "180"))
SESSION_TURN_DAYS = int(os.environ.get("SESSION_TURN_RETENTION_DAYS", "365"))
ATTACHMENT_DAYS = int(os.environ.get("ATTACHMENT_RETENTION_DAYS", "30"))


def main():
    conn = psycopg2.connect(dbname=DB, client_encoding="UTF8")
    cur = conn.cursor()
    cur.execute("DELETE FROM action_log WHERE created_at < now() - make_interval(days => %s)", (ACTION_LOG_DAYS,))
    al = cur.rowcount
    cur.execute("DELETE FROM session_recall WHERE recalled_at < now() - make_interval(days => %s)", (SESSION_RECALL_DAYS,))
    sr = cur.rowcount
    # prune knowledge-graph edges dangling off a soft-deleted memory. classify_edges.py JOINs
    # deleted_at IS NULL on BOTH endpoints, so an edge to/from a retired note is never typed AND never
    # pruned — it sits forever as relates_to with classified_at NULL, dead weight in the graph. Delete
    # them here (recurring, so they never re-accumulate). A restored note re-derives its edges from its
    # live [[links]] on the next write, so this is safe.
    cur.execute("DELETE FROM memory_relation WHERE src_id IN (SELECT id FROM memory WHERE deleted_at IS NOT NULL) "
                "OR dst_id IN (SELECT id FROM memory WHERE deleted_at IS NOT NULL)")
    de = cur.rowcount
    # prune ingested transcripts (session_turn) past their window so a public box doesn't grow
    # without bound. TWO invariants make this safe:
    #  (a) TOMBSTONE — delete only the session_turn rows, NEVER the session row.'s validate-sweep
    #      treats "session row EXISTS + zero live turns" as the ONLY provable "source gone"; dropping
    #      the session row too would make a pruned transcript indistinguishable from never-ingested.
    #  (b) MEMORY-LINKED PROTECTION — skip any session a LIVE memory still points at (source_session),
    #      so a note's source is never pruned while the note lives (keeps it validatable; the safest
    #      outcome — D4 source-gone-delete then only ever fires on genuinely orphaned transcripts).
    # SESSION_TURN_DAYS=0 disables turn-pruning entirely (keep forever).
    st = 0
    if SESSION_TURN_DAYS > 0:
        cur.execute(
            "DELETE FROM session_turn t USING session s "
            "WHERE t.session_id = s.id "
            "AND COALESCE(s.started_at, s.created_at) < now() - make_interval(days => %s) "
            "AND NOT EXISTS (SELECT 1 FROM memory m "
            "                WHERE m.source_session = s.source_session AND m.deleted_at IS NULL)",
            (SESSION_TURN_DAYS,))
        st = cur.rowcount
    # reclaim memory_attachment blobs (user-uploaded bytea, NOT re-derivable — unlike graph edges
    # or transcripts) once provably past the restore window. HARD-delete (frees the blob) an attachment
    # whose ANCHOR memory has been soft-deleted longer than the grace window, OR whose own deleted_at is
    # that old. A memory restored WITHIN the window keeps its attachments (restore-safe); after it,
    # reclamation is documented retention behaviour. ATTACHMENT_DAYS=0 disables (keep forever).
    at = 0
    if ATTACHMENT_DAYS > 0:
        cur.execute(
            "DELETE FROM memory_attachment a USING memory m "
            "WHERE a.anchor_memory_id = m.id "
            "AND ((m.deleted_at IS NOT NULL AND m.deleted_at < now() - make_interval(days => %s)) "
            "     OR (a.deleted_at IS NOT NULL AND a.deleted_at < now() - make_interval(days => %s)))",
            (ATTACHMENT_DAYS, ATTACHMENT_DAYS))
        at = cur.rowcount
    # liveness row (inserted AFTER the deletes so it's never itself pruned in the same run)
    cur.execute("INSERT INTO action_log(actor,action,target_kind,detail) VALUES (%s,%s,%s,%s)",
                ("retention-prune", "retention_prune", "system",
                 psycopg2.extras.Json({"action_log_deleted": al, "session_recall_deleted": sr,
                                       "dangling_edges_deleted": de, "session_turn_deleted": st,
                                       "attachment_deleted": at,
                                       "action_log_days": ACTION_LOG_DAYS,
                                       "session_recall_days": SESSION_RECALL_DAYS,
                                       "session_turn_days": SESSION_TURN_DAYS,
                                       "attachment_days": ATTACHMENT_DAYS})))
    conn.commit(); cur.close(); conn.close()
    print("retention_prune: action_log -%d, session_recall -%d, dangling_edges -%d, session_turn -%d, attachment -%d"
          % (al, sr, de, st, at))


if __name__ == "__main__":
    main()
