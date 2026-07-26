#!/usr/bin/env python3
"""scheduled Ed25519 tamper check (the brain host, run on a a daily systemd timer).

GET /memory/verify only ever fired if someone manually curled it (audit M5). This runs the SAME
verification server-side, reusing api.verify_memory_row (the PURE crypto helper — one implementation,
no drift) but connecting to Postgres DIRECTLY (api.db()/api.log() need a Flask request context, which
a CLI script has no business creating). On any mismatch it logs an action_log row + alerts every
manager, exactly like the endpoint's detective path; a clean run logs a liveness row too.
"""
import hashlib
import os
import psycopg2
import psycopg2.extras
import api   # reuse ONLY verify_memory_row (pure: loads the local key, verifies a row dict)

DB = os.environ.get("PGDATABASE", "brain")


def _log(cur, actor, action, tkind, tid, detail):
    cur.execute("INSERT INTO action_log(actor,action,target_kind,target_id,detail) VALUES (%s,%s,%s,%s,%s)",
                (actor, action, tkind, tid, psycopg2.extras.Json(detail) if detail else None))


def main():
    conn = psycopg2.connect(dbname=DB, client_encoding="UTF8")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, body, author_body, source_session, signature "
                "FROM memory WHERE deleted_at IS NULL")
    signed = unsigned = 0
    tampered = []
    for r in cur.fetchall():
        v = api.verify_memory_row(r)
        if v is None:
            unsigned += 1
        elif v:
            signed += 1
        else:
            tampered.append({"id": str(r["id"]), "name": r.get("name")})
    if tampered:
        _log(cur, "brain-guard", "memory_sig_tamper", "memory", None,
             {"count": len(tampered), "ids": [t["id"] for t in tampered[:20]]})
        cur.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
        for m in [row["name"] for row in cur.fetchall()]:
            cur.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) VALUES (%s,%s,%s,%s,%s)",
                        ("brain-guard", m, "ALERT: memory signature mismatch",
                         "%d memory row(s) failed Ed25519 verification (possible direct-DB tamper). "
                         "Names: %s." % (len(tampered),
                                         ", ".join(t["name"] or t["id"] for t in tampered[:10])), "alert"))
    else:
        _log(cur, "brain-guard", "memory_verify_ok", "system", None,
             {"signed": signed, "unsigned_legacy": unsigned})

    # integrity-check user-uploaded attachment blobs. The Ed25519 signature above covers memory
    # rows only; attachments instead carry a stored sha256 + byte_size (migration 0019). Recompute both
    # from the bytea and compare — a mismatch means a direct-DB blob tamper. Same detective path as the
    # memory check: log an action_log row + alert every manager.
    cur.execute("SELECT id, filename, sha256, byte_size, content "
                "FROM memory_attachment WHERE deleted_at IS NULL")
    att_ok = 0
    att_bad = []
    for r in cur.fetchall():
        blob = bytes(r["content"]) if r["content"] is not None else b""
        if hashlib.sha256(blob).hexdigest() == (r["sha256"] or "") and len(blob) == (r["byte_size"] or -1):
            att_ok += 1
        else:
            att_bad.append({"id": str(r["id"]), "name": r.get("filename")})
    if att_bad:
        _log(cur, "brain-guard", "attachment_integrity_tamper", "memory_attachment", None,
             {"count": len(att_bad), "ids": [t["id"] for t in att_bad[:20]]})
        cur.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
        for m in [row["name"] for row in cur.fetchall()]:
            cur.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) VALUES (%s,%s,%s,%s,%s)",
                        ("brain-guard", m, "ALERT: attachment integrity mismatch",
                         "%d attachment blob(s) failed sha256/byte_size verification (possible direct-DB "
                         "tamper). Files: %s." % (len(att_bad),
                         ", ".join(t["name"] or t["id"] for t in att_bad[:10])), "alert"))
    else:
        _log(cur, "brain-guard", "attachment_verify_ok", "system", None, {"verified": att_ok})

    conn.commit(); cur.close(); conn.close()
    print("memory_verify: signed=%d unsigned=%d tampered=%d | attachments: ok=%d tampered=%d"
          % (signed, unsigned, len(tampered), att_ok, len(att_bad)))


if __name__ == "__main__":
    main()
