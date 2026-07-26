"""0019 memory attachments — attach a file / image / blob to a memory.

the operator: "let an agent attach related info to a memory — a text file, a table, or a
picture — so finding the memory leads to the attached information." Structured TABLES already
anchor to a memory (0007 provisional_artifact); this migration adds the BLOB side (files,
images, arbitrary binary) as `memory_attachment`.

The blob lives in a bytea column IN Postgres (not on disk) so it rides the existing pg_dump
backup automatically — durability-first, no separate blob store to back up (a restore brings
attachments back with the DB). Per-attachment size is capped in api.py (ATTACH_MAX_BYTES).

Access is DERIVED, never copied: an attachment is reachable iff its anchor memory is visible to
the caller (api.py resolves anchor_memory_id -> _visible_memory -> mem_read_where), so a memory's
sensitivity / reader-group / share_status changes carry to its attachments with zero drift.
Soft-delete (deleted_at) mirrors memory. The FK is ON DELETE CASCADE for the rare hard-delete;
the normal memory soft-delete just hides the anchor (and thus its attachments). Reversible.
"""
VERSION = "0019"
NAME = "memory_attachments"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory_attachment (
          id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          anchor_memory_id uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
          kind             text NOT NULL DEFAULT 'file'
                             CHECK (kind IN ('file','image','blob')),
          filename         text NOT NULL,
          content_type     text NOT NULL DEFAULT 'application/octet-stream',
          byte_size        bigint NOT NULL,
          sha256           text NOT NULL,          -- integrity + dedup signal
          caption          text,                   -- so recall can say what it is without a fetch
          content          bytea NOT NULL,         -- the blob; text files = their utf-8 bytes
          author_body      text,                   -- who attached it (audit)
          created_at       timestamptz NOT NULL DEFAULT now(),
          deleted_at       timestamptz             -- soft-delete; NULL = live
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS memory_attachment_anchor_idx "
                "ON memory_attachment (anchor_memory_id) WHERE deleted_at IS NULL")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS memory_attachment")
