"""0006 provisional tier — an agent's working memory before manager graduation.

the operator (provisional-tier-design.md). An agent may write its OWN memory and use
it for 2 WEEKS before a manager (managers) graduates it to trusted/shared — or it expires
and soft-deletes. This migration adds the storage; the security behaviour (author-only,
quarantined, TTL hide) is enforced in the read path (api.py), not in DDL.

  - memory.mem_tier gains 'provisional'.
  - expires_at (the TTL; NULL = permanent) added to the KNOWLEDGE tables (memory + lesson)
    so the access gate's temporal guard is uniform across knowledge-kind tables.
"""
VERSION = "0006"
NAME = "provisional_tier"

KNOWLEDGE_TABLES = ("memory", "lesson")


def up(cur):
    for t in KNOWLEDGE_TABLES:
        cur.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS expires_at timestamptz" % t)
    cur.execute("ALTER TABLE memory DROP CONSTRAINT IF EXISTS memory_mem_tier_check")
    cur.execute("ALTER TABLE memory ADD CONSTRAINT memory_mem_tier_check "
                "CHECK (mem_tier IN ('semantic','episodic','provisional'))")
    # supports both author recall of own provisional and the expiry sweep
    cur.execute("CREATE INDEX IF NOT EXISTS memory_provisional_idx ON memory (author_body, expires_at) "
                "WHERE mem_tier='provisional' AND deleted_at IS NULL")


def down(cur):
    # demote any provisional rows before tightening the constraint again
    cur.execute("UPDATE memory SET mem_tier='episodic' WHERE mem_tier='provisional'")
    cur.execute("DROP INDEX IF EXISTS memory_provisional_idx")
    cur.execute("ALTER TABLE memory DROP CONSTRAINT IF EXISTS memory_mem_tier_check")
    cur.execute("ALTER TABLE memory ADD CONSTRAINT memory_mem_tier_check "
                "CHECK (mem_tier IN ('semantic','episodic'))")
    for t in KNOWLEDGE_TABLES:
        cur.execute("ALTER TABLE %s DROP COLUMN IF EXISTS expires_at" % t)
