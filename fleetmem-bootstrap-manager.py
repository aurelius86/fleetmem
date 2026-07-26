#!/usr/bin/env python3
"""fleetmem-bootstrap-manager.py — create the FIRST (genesis) manager. Run ONCE on the brain host.

Why this exists: every other agent joins via /enroll -> manager approval -> cert sign. At genesis
there are NO managers yet, so approval can't apply. This one-time CLI creates that first manager
directly on the box (which holds both the DB and the local CA). It is fail-closed: it refuses to
run once ANY manager already exists.

You PICK the name — nothing is baked in. You also pick how many managers must co-approve future
agents (ENROLL_APPROVALS); this only sets a default you can change later.

Steps it does: mint the manager's mTLS client cert (via the local CA), mint a bearer token (shown
ONCE), insert the manager agent row, write the token + client.conf, print the MCP wiring.

Usage:
  FLEETMEM_MANAGER=<name> [ENROLL_APPROVALS=1] [PKI_DIR=/opt/brain-db/pki] \
      python3 fleetmem-bootstrap-manager.py
  (omit FLEETMEM_MANAGER on an interactive terminal and it will prompt)
"""
import hashlib
import os
import re
import secrets
import subprocess
import sys

import psycopg2
import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
PKI_DIR = os.environ.get("PKI_DIR", "/opt/brain-db/pki")
OUT_DIR = os.path.expanduser(os.environ.get("FLEETMEM_HOME", "~/.fleetmem"))
BRAIN_URL = os.environ.get("BRAIN_URL", "https://YOUR-BRAIN-HOST:8443")


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def main():
    name = os.environ.get("FLEETMEM_MANAGER") or (
        input("Name for your first manager (you choose): ").strip() if sys.stdin.isatty() else "")
    if not name:
        die("set FLEETMEM_MANAGER=<name> (no default identity ships)")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,30}", name):
        die("name must be lowercase alnum / _ / - (2-31 chars), e.g. 'ada' or 'ops-lead'")

    approvals = os.environ.get("ENROLL_APPROVALS")
    if not approvals and sys.stdin.isatty():
        approvals = input("How many managers must approve a NEW agent later? [1]: ").strip() or "1"
    approvals = approvals or "1"
    if not approvals.isdigit() or int(approvals) < 1:
        die("ENROLL_APPROVALS must be a positive integer")

    if not (os.path.isfile(os.path.join(PKI_DIR, "ca.crt")) and os.path.isfile(os.path.join(PKI_DIR, "ca.key"))):
        die("local CA not found in %s — run fleetmem-init-pki.sh first" % PKI_DIR)

    conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "brain"), client_encoding="UTF8")
    conn.set_client_encoding("UTF8")
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT count(*) AS c FROM agent WHERE role='manager'")
    if cur.fetchone()["c"] > 0:
        die("a manager already exists — genesis is one-time; add more agents via the /enroll flow")

    # 1) client key + CSR + sign against the local CA -----------------------
    os.makedirs(os.path.join(OUT_DIR, "pki"), exist_ok=True)
    key = os.path.join(OUT_DIR, "pki", "client.key")
    csr = os.path.join(OUT_DIR, "pki", "%s.csr" % name)
    crt = os.path.join(OUT_DIR, "pki", "client.crt")
    if not os.path.isfile(key):
        sh(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", key])
        os.chmod(key, 0o600)
    sh(["openssl", "req", "-new", "-key", key, "-out", csr, "-subj", "/CN=%s/O=fleetmem/OU=agents" % name])
    env = dict(os.environ, PKI_DIR=PKI_DIR)
    sh(["bash", os.path.join(HERE, "sign-agent-cert.sh"), name, csr], env=env)
    signed = os.path.join(OUT_DIR, "pki", "%s-client.crt" % name)  # written beside the CSR
    sh(["cp", signed, crt])
    sh(["cp", os.path.join(PKI_DIR, "ca.crt"), os.path.join(OUT_DIR, "pki", "ca.crt")])

    # 2) mint token + insert the manager agent row (same shape as enroll provision) ---
    token = "brain_%s_%s" % (name, secrets.token_hex(24))
    th = hashlib.sha256(token.encode()).hexdigest()
    groups = ["common", "managers"]
    cur.execute(
        "INSERT INTO agent(name,cert_cn,role,welcome,lane,agent_tier,access_scope,readers,"
        "sensitivity,created_by,token_hash,token_prefix) "
        "VALUES (%s,%s,'manager',%s,'direct',3,%s,%s,'sensitive','genesis',%s,%s) "
        "ON CONFLICT (name) DO NOTHING RETURNING id",
        (name, name, "Genesis manager %s — full reach within the safety floor." % name,
         psycopg2.extras.Json({"groups": groups}), groups, th, token[:16]))
    row = cur.fetchone()
    if not row:
        conn.rollback(); die("an agent named '%s' already exists" % name)

    # 3) write token file + client.conf -------------------------------------
    tok_file = os.path.join(OUT_DIR, "%s.token" % name)
    with open(tok_file, "w") as f:
        f.write(token + "\n")
    os.chmod(tok_file, 0o600)
    conf = os.path.join(OUT_DIR, "client.conf")
    with open(conf, "w") as f:
        f.write("BRAIN_URL=%s\n" % BRAIN_URL)
        f.write("BRAIN_CERT=%s/pki/client.crt\n" % OUT_DIR)
        f.write("BRAIN_KEY=%s/pki/client.key\n" % OUT_DIR)
        f.write("BRAIN_CA=%s/pki/ca.crt\n" % OUT_DIR)
        f.write("BRAIN_TOKEN_FILE=%s\n" % tok_file)

    conn.commit(); cur.close(); conn.close()

    print("\n=== genesis manager '%s' created ===" % name)
    print("  role=manager  cert_cn=%s  groups=%s" % (name, ",".join(groups)))
    print("  client cert : %s" % crt)
    print("  token file  : %s  (0600, shown ONCE below)" % tok_file)
    print("  client.conf : %s" % conf)
    print("  ENROLL_APPROVALS you chose: %s  -> set this in brain.env" % approvals)
    print("\n  TOKEN (stored only as a hash on the server; keep this safe):\n    %s" % token)
    print("\nNext: register the fleetmem MCP with your LLM agent using these env values:")
    _prefix = os.path.dirname(os.path.abspath(__file__))   # the install dir this script was deployed to
    _py = os.path.join(_prefix, "venv", "bin", "python")
    if not os.path.exists(_py):
        _py = "python3"                                     # fallback: a host running from a bare checkout
    print('  { "mcpServers": { "fleetmem": { "command": "%s", "args": ["%s"],' % (_py, os.path.join(_prefix, "mcp", "server.py")))
    print('      "env": { "BRAIN_URL": "%s", "BRAIN_CERT": "%s/pki/client.crt",' % (BRAIN_URL, OUT_DIR))
    print('               "BRAIN_KEY": "%s/pki/client.key", "BRAIN_CA": "%s/pki/ca.crt",' % (OUT_DIR, OUT_DIR))
    print('               "BRAIN_TOKEN_FILE": "%s" } } } }' % tok_file)
    print("\nSmoke-test:  curl --cert %s/pki/client.crt --key %s/pki/client.key \\" % (OUT_DIR, OUT_DIR))
    print("               --cacert %s/pki/ca.crt -H \"Authorization: Bearer %s\" %s/whoami" % (OUT_DIR, "$(cat %s)" % tok_file, BRAIN_URL))


if __name__ == "__main__":
    main()
