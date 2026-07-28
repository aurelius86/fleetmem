"""0046 drop_redundant_name_index — remove the duplicate memory_name_live_uniq (from 0045).

0045 added `memory_name_live_uniq`, but `memory_name_uniq` (migration 0001) already enforces
the identical constraint: a partial unique index on `name WHERE deleted_at IS NULL AND name IS NOT
NULL`. 0045's only addition was `AND name <> ''`, a no-op in practice — blank names are stored as
NULL (already excluded), and no empty-string names exist. So 0045 was a redundant duplicate that
doubled the unique-check + index-write cost on every memory write. Drop it; the original
`memory_name_uniq` stays and does the job.

Forward-only + idempotent: on a box that applied 0045 this drops the dup; on a fresh install 0045
creates it and 0046 drops it (net-zero). The real fix for the nameless-rows problem is the write-path
guard (api._derive_name in /provisional/memory + autolearn), not another index.
"""
VERSION = "0046"
NAME = "drop_redundant_name_index"


def up(cur):
    cur.execute("DROP INDEX IF EXISTS memory_name_live_uniq")


def down(cur):
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS memory_name_live_uniq "
        "ON memory (name) WHERE deleted_at IS NULL AND name IS NOT NULL AND name <> ''"
    )
