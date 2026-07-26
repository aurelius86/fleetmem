"""0010 share_tier — universal personal|ready_to_share|trusted lifecycle.

the operator. One share_status per CONTENT row (memory/task/project/idea):
  * personal        = author-only, PERMANENT — the agent's private layer, fully agent-controlled.
  * ready_to_share  = author promoted it -> manager (managers) review queue ONLY (invisible to recall).
  * trusted         = shared; the existing per-agent reader tag (readers[]/sensitivity) STILL gates it,
                      and only managers see all.

Backfill: today's author-only `provisional` memory becomes PERMANENT personal (drop its TTL);
every other existing row defaults `trusted` (it IS today's shared brain). Additive + reversible.
Column added to task/project/idea too so the same lifecycle is ready there (wired under)."""
VERSION = "0010"
NAME = "share_tier"

_TABLES = ["memory", "task", "project", "idea"]


def up(cur):
    for t in _TABLES:
        cur.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS share_status text NOT NULL "
            "DEFAULT 'trusted' CHECK (share_status IN ('personal','ready_to_share','trusted'))" % t)
    cur.execute("CREATE INDEX IF NOT EXISTS memory_share_idx ON memory (share_status, author_body)")
    # today's author-only provisional notes -> permanent personal (no TTL)
    cur.execute("UPDATE memory SET share_status='personal', expires_at=NULL WHERE mem_tier='provisional'")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_share_idx")
    for t in _TABLES:
        cur.execute("ALTER TABLE %s DROP COLUMN IF EXISTS share_status" % t)
