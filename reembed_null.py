#!/usr/bin/env python3
"""re-embed rows whose embedding is NULL (the brain host, run on a a daily systemd timer).

A write-time embed failure (Ollama hiccup) stores embedding=NULL forever and no backfill job
existed (audit M1) — those rows are then invisible to dense recall permanently. This finds live
`memory` rows and `session_turn` rows with a NULL embedding and re-embeds them via the SAME bge-m3
path the app uses (search.embed). Bounded per run (REEMBED_BATCH, default 200) so a backlog can't
pin Ollama; Ollama-down leaves the row NULL for the next run (no crash). Logs a liveness row.
"""
import os
import psycopg2
import psycopg2.extras
from search import embed, vec_literal, MODEL

DB = os.environ.get("PGDATABASE", "brain")
LIMIT = int(os.environ.get("REEMBED_BATCH", "200"))


def main():
    conn = psycopg2.connect(dbname=DB, client_encoding="UTF8")
    cur = conn.cursor()

    cur.execute("SELECT id, body FROM memory WHERE embedding IS NULL AND deleted_at IS NULL "
                "AND body IS NOT NULL AND body <> '' LIMIT %s", (LIMIT,))
    mn = 0
    for rid, body in cur.fetchall():
        try:
            v = vec_literal(embed(body))
        except Exception:
            continue                     # Ollama down -> leave NULL, retry next run
        with conn.cursor() as u:
            u.execute("UPDATE memory SET embedding=%s::vector, embed_model=%s WHERE id=%s", (v, MODEL, rid))
        mn += 1

    cur.execute("SELECT id, text FROM session_turn WHERE embedding IS NULL AND text IS NOT NULL "
                "AND text <> '' LIMIT %s", (LIMIT,))
    tn = 0
    for rid, txt in cur.fetchall():
        try:
            v = vec_literal(embed(txt))
        except Exception:
            continue
        with conn.cursor() as u:
            u.execute("UPDATE session_turn SET embedding=%s::vector WHERE id=%s", (v, rid))
        tn += 1

    cur.execute("INSERT INTO action_log(actor,action,target_kind,detail) VALUES (%s,%s,%s,%s)",
                ("reembed", "reembed_null", "system",
                 psycopg2.extras.Json({"memory": mn, "session_turn": tn})))
    conn.commit(); cur.close(); conn.close()
    print("reembed_null: memory +%d, session_turn +%d" % (mn, tn))


if __name__ == "__main__":
    main()
