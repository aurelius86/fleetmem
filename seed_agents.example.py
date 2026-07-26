#!/usr/bin/env python3
"""Seed the FIRST (genesis) manager agent row — idempotent by name.

EXAMPLE FILE. Two ways to create your first manager:
  1. Preferred: let the onboarding /bootstrap flow do it (it also mints the genesis
     mTLS cert + token). See AGENTS.md / INSTALL.md.
  2. Manual: copy this to `seed_agents.py`, set GENESIS_MANAGER / GENESIS_MANAGER_CN
     (the CN must equal the CN in that manager's client certificate), and run it.

NO secrets belong in this file. Tokens are generated on the agent's own host at
enrollment; only the token HASH is stored on the agent row. `cert_cn` is the mTLS
transport identity (matched against the client cert's CN on every request).
"""
import os
import psycopg2
import psycopg2.extras

# One genesis manager. You PICK the name — nothing ships as a default identity.
# Everything else (workers, more managers) is added later via /enroll; HOW MANY managers
# must co-approve a new agent is your choice (ENROLL_APPROVALS), not decided here.
_NAME = os.environ.get("GENESIS_MANAGER")
if not _NAME:
    raise SystemExit("set GENESIS_MANAGER=<the manager name you choose> "
                     "(optional GENESIS_MANAGER_CN=<client-cert CN>, defaults to the name)")
AGENTS = [
    {"name": _NAME,
     "cert_cn": os.environ.get("GENESIS_MANAGER_CN", _NAME),
     "role": "manager", "lane": "direct", "tier": 3,
     "welcome": "Primary manager — full reach within the safety floor.",
     "groups": ["common", "managers"]},
]


def main():
    conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")
    conn.set_client_encoding("UTF8")
    cur = conn.cursor()
    for a in AGENTS:
        cur.execute(
            "INSERT INTO agent(name,cert_cn,role,welcome,lane,agent_tier,access_scope,readers,sensitivity,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'sensitive','bootstrap') "
            "ON CONFLICT (name) DO UPDATE SET cert_cn=EXCLUDED.cert_cn, role=EXCLUDED.role, "
            "welcome=EXCLUDED.welcome, lane=EXCLUDED.lane, agent_tier=EXCLUDED.agent_tier, "
            "access_scope=EXCLUDED.access_scope, readers=EXCLUDED.readers, updated_at=now()",
            (a["name"], a["cert_cn"], a["role"], a["welcome"], a["lane"], a["tier"],
             psycopg2.extras.Json({"groups": a["groups"]}), a["groups"]))
    conn.commit()
    cur.execute("SELECT name, role, cert_cn, token_hash IS NOT NULL AS has_token FROM agent ORDER BY name")
    for r in cur.fetchall():
        print("  agent:", r)
    cur.close()
    conn.close()
    print("SEED-AGENTS-DONE")


if __name__ == "__main__":
    main()
