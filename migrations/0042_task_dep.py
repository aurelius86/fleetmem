"""0042 task_dep — task->task dependency edges: "this task waits on that one".

Before this, the store had a `blocked` STATUS but nowhere to record WHAT a task is blocked ON — so
`blocked` degraded into a "parked" flag (3 tasks marked blocked, none actually waiting on anything)
while real dependencies lived as unqueryable prose in 439 task notes. Both to-tickets and wayfinder wanted the same primitive so the store can answer the one question it couldn't: "what can I
pick up right now?" — the FRONTIER (open tasks whose every blocker is done).

WHY A NEW TABLE, not the existing edge store: `memory_relation` already carries `depends_on` edges,
but it is FK-locked to `memory` on both ends (memory_relation_src_id_fkey / _dst_id_fkey) — a task
uuid is not a memory row, so tasks cannot live there. This mirrors memory_relation's proven shape,
scoped to tasks.

EDGE DIRECTION: blocked_id waits on blocker_id. Read a row as "blocked_id is blocked by blocker_id".

ON DELETE CASCADE on both FKs = deleting a task takes its edges with it, so a blocker can never dangle.
UNIQUE(blocked_id, blocker_id) makes a repeated edge a no-op; CHECK(blocked_id <> blocker_id) rejects
the trivial self-edge. Longer cycles (A->B->A) are NOT prevented here — they simply keep those tasks
off the frontier (a visible symptom, not corruption); recursive cycle-detection is a later slice.

New TABLE => needs its own GRANT to brain_app (unlike a column-add on an already-granted table);
mirrors 0039_skill.py. Reversible: down() drops the table (indexes + sequence go with it).
"""
VERSION = "0042"
NAME = "task_dep"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_dep (
          id          bigserial PRIMARY KEY,
          blocked_id  uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
          blocker_id  uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
          created_by  text,
          created_at  timestamptz DEFAULT now(),
          UNIQUE (blocked_id, blocker_id),
          CHECK  (blocked_id <> blocker_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS task_dep_blocked_idx ON task_dep (blocked_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS task_dep_blocker_idx ON task_dep (blocker_id)")
    cur.execute("GRANT SELECT, INSERT, DELETE ON task_dep TO brain_app")
    cur.execute("GRANT USAGE, SELECT ON SEQUENCE task_dep_id_seq TO brain_app")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS task_dep")
