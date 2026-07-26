"""0022 memory tags — a lightweight tag facet on memories. was "build add_doc for structured docs (title/sections/tags)". Assessment: a
structured doc is already native (a markdown reference-type memory body + description + the [[link]]
graph), so the only genuinely net-new piece the operator chose to build is the TAGS facet: a `tags text[]`
on memory for tag-based organization + recall filtering, alongside (not replacing) the graph.

`proposal.tags` carries an author's tags through the propose -> approve -> apply path (the decide
endpoint does RETURNING * -> apply_proposal -> build_memory_row, which now persists tags). Reversible.
"""
VERSION = "0022"
NAME = "memory_tags"


def up(cur):
    cur.execute("ALTER TABLE memory ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}'")
    cur.execute("ALTER TABLE proposal ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}'")
    cur.execute("CREATE INDEX IF NOT EXISTS memory_tags_gin ON memory USING gin(tags)")


def down(cur):
    cur.execute("DROP INDEX IF EXISTS memory_tags_gin")
    cur.execute("ALTER TABLE memory DROP COLUMN IF EXISTS tags")
    cur.execute("ALTER TABLE proposal DROP COLUMN IF EXISTS tags")
