"""0018 memory signature ( tail) — server-side Ed25519 tamper-evidence for memory rows.

The brain signs a canonical payload (name | author_body | source_session | sha256(body)) with its
Ed25519 PRIVATE key on write, storing base64(sig) + a short key id. GET /memory/verify re-verifies
every signed row with the PUBLIC key and flags any whose signature no longer matches its content —
detecting a direct-Postgres edit that bypassed the API (the one tamper path the trust/provenance gate
can't see). Detective, fail-soft: a NULL signature = unsigned/legacy (grandfathered, NOT a tamper
flag); no key on disk = signing disabled. sig_key_id supports key rotation. Reversible.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VERSION = "0018"
NAME = "memory_signature"


def up(cur):
    cur.execute("ALTER TABLE memory ADD COLUMN IF NOT EXISTS signature text")
    cur.execute("ALTER TABLE memory ADD COLUMN IF NOT EXISTS sig_key_id text")


def down(cur):
    cur.execute("ALTER TABLE memory DROP COLUMN IF EXISTS signature")
    cur.execute("ALTER TABLE memory DROP COLUMN IF EXISTS sig_key_id")
