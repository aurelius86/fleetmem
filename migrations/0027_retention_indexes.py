"""0027 retention/hot-query indexes + session_recall dedup (audit M5/B1-10).

Three changes, all low-risk:
  1. action_log (action, created_at) — GET /autolearn/last does `action LIKE 'autolearn%'` + a
     max(created_at)/created_at filter over a table that grows ~146k rows/yr; without this it's a seq scan.
  2. session_recall (memory_id) — the co-recall usage join (link_usage: "a new memory in this session,
     which memories were co-recalled?") filters by memory_id, previously unindexed.
  3. UNIQUE (session_id, memory_id) on session_recall — the recall path appended a row per recall, so a
     memory recalled 3x in a session made 3 identical rows (75 pairs logged >3x). The co-recall graph only
     needs "did this session recall this memory", so collapse to one row. Existing dups are removed
     (keep the earliest id) BEFORE adding the constraint; the app INSERT gains ON CONFLICT DO NOTHING.

down() drops the constraint + both indexes (the deduped rows are not restored — they were redundant).
"""
VERSION = "0027"
NAME = "retention_indexes"


def up(cur):
    cur.execute("CREATE INDEX IF NOT EXISTS action_log_action_created_idx ON action_log (action, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS session_recall_memory_idx ON session_recall (memory_id)")
    # collapse existing duplicate (session_id, memory_id) pairs, keeping the earliest row, so the
    # UNIQUE constraint can be added without violation.
    cur.execute("DELETE FROM session_recall a USING session_recall b "
                "WHERE a.session_id = b.session_id AND a.memory_id = b.memory_id AND a.id > b.id")
    cur.execute("ALTER TABLE session_recall ADD CONSTRAINT session_recall_session_memory_uniq "
                "UNIQUE (session_id, memory_id)")


def down(cur):
    cur.execute("ALTER TABLE session_recall DROP CONSTRAINT IF EXISTS session_recall_session_memory_uniq")
    cur.execute("DROP INDEX IF EXISTS session_recall_memory_idx")
    cur.execute("DROP INDEX IF EXISTS action_log_action_created_idx")
