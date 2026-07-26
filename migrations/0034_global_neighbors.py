"""0034 global_neighbors() — content-blind cross-agent nearest-neighbour search for autolearn v2.

Autolearn v2 needs to compare a new capture against EVERY agent's memories (to dedup across agents and
to link into the whole graph) WITHOUT reading any note body and WITHOUT the caller's RLS narrowing the
candidate set. This SECURITY DEFINER function runs as the table owner (who bypasses RLS — NO FORCE, same
as migrations/hygiene), so it sees all rows; it returns ONLY metadata + similarity, never `body`.

Content-blind by construction: the RETURNS TABLE list has no `body` column, so no other agent's note
content can ever leak through this path — only (id, name, author_body, share_status, sim). Callers use
the ids to create relates_to edges and to detect a cross-agent duplicate (then raise a manager-reviewed
share request); the drafting LLM never sees any of this.

Reversible: down() drops the function.
"""
VERSION = "0034"
NAME = "global_neighbors"

_FN = """
CREATE OR REPLACE FUNCTION global_neighbors(q vector, k integer)
RETURNS TABLE(id uuid, name text, author_body text, share_status text, sim double precision)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE
AS $fn$
  SELECT m.id, m.name, m.author_body, m.share_status,
         1 - (m.embedding <=> q) AS sim
  FROM memory m
  WHERE m.embedding IS NOT NULL AND m.deleted_at IS NULL
  ORDER BY m.embedding <=> q
  LIMIT GREATEST(k, 0)
$fn$;
"""


def up(cur):
    cur.execute(_FN)
    # Least privilege: no ambient EXECUTE; grant only to the app role. The function is content-blind
    # (no body in its result), so app callers get cross-agent metadata + similarity, never content.
    cur.execute("REVOKE ALL ON FUNCTION global_neighbors(vector, integer) FROM PUBLIC")
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "GRANT EXECUTE ON FUNCTION global_neighbors(vector, integer) TO brain_app; END IF; END $$")


def down(cur):
    cur.execute("DROP FUNCTION IF EXISTS global_neighbors(vector, integer)")
