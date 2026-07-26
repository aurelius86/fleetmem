# fleetmem — new-agent onboarding runbook

How a brand-new agent/body joins a running fleetmem. The enrollment API issues identity + token;
the manager cert-signing step is `sign-agent-cert.sh`, signing against the box's local CA.

## The model (why it's two-phase)
Auth is **mTLS + bearer token, both factors**. The open `/enroll` endpoint is the ONLY mTLS-exempt
path (a new agent has no cert yet); every other endpoint needs both. On approval the API mints the
**token + identity**, but **NOT the cert** — CA signing stays manager-side (`sign-agent-cert.sh`),
so an API compromise can never mint certs. Provisioning is: API issues token+role → a **manager
signs the CSR** with the local CA → the agent installs both.

**The first manager (genesis).** When the brain is fresh there are no managers yet, so approvals
can't apply — the install/onboarding flow creates ONE genesis manager directly (you pick its name;
it signs its own cert against the freshly-minted local CA). Every agent after that goes through the
enroll → approve → sign flow below. **You decide how many managers must co-approve** a new agent via
`ENROLL_APPROVALS` (1 = single-manager; raise it to require several) — fleetmem does not assume one or
many.

## Applicant side (on the new agent's host)
```sh
# 1. generate a key (stays local, 0600) + a CSR; the CN is the agent name YOU choose
mkdir -p ~/.fleetmem/pki && cd ~/.fleetmem/pki
openssl ecparam -name prime256v1 -genkey -noout -out client.key && chmod 600 client.key
openssl req -new -key client.key -out <name>.csr -subj "/CN=<name>/O=fleetmem/OU=agents"

# 2. apply via the open enroll endpoint (no creds) -> returns an application id + one-time secret
#    (grants NOTHING yet).
```
Hand the **public** `<name>.csr` to a manager. The private key never leaves the host.

## Manager side
```sh
# 1. see pending applications + cast approval (via the fleetmem MCP):
#    brain_enroll_pending / brain_enroll_approve(enrollment_id, assign_role, assign_groups)
#    roles: manager (broad read+propose+approve) | worker (scoped, no sensitive) | readonly
#    Needs ENROLL_APPROVALS distinct manager approvals (your config).

# 2. once approved, the applicant pulls /enroll/status?id=&secret= ONCE -> receives its TOKEN
#    (written to the agent's disk 0600, e.g. ~/.fleetmem/<name>.token). Brain stores only the hash.

# 3. sign the applicant's CSR against the local CA (the cert half the API won't do):
./sign-agent-cert.sh <name> /path/to/<name>.csr            # -> <name>-client.crt (leaf + local CA)
#    deliver <name>-client.crt back to the agent as ~/.fleetmem/pki/client.crt
#    the CA bundle is public: copy the box's ca.crt (PKI dir, e.g. /opt/brain-db/pki/ca.crt)

# 4. open the brain host's firewall to the new agent's source IP(s) if needed (agents reach :8443).
```

## Revoking an agent (kill-switch)
Revocation is instant: `authenticate()` rejects a revoked agent, so its **very next** API call fails
`401`. Manager only; you cannot revoke your own agent. Restore with `unrevoke`.
```sh
# via the fleetmem MCP (manager):
#    brain_revoke("<name>")                 # cut off access now
#    brain_revoke("<name>", unrevoke=True)  # restore access

# or raw HTTP (mTLS + Bearer, from a manager's creds):
curl --cert client.crt --key client.key --cacert ca.crt \
     -H "Authorization: Bearer $(cat ~/.fleetmem/<manager>.token)" \
     -H "Content-Type: application/json" -d '{}' \
     -X POST https://<brain-host>:8443/agent/<name>/revoke
#    add -d '{"unrevoke":true}' to restore.
```
Revoking does NOT delete the agent's cert — for a compromised key, also stop re-signing its CSR (let
the 90-day leaf lapse) and drop its firewall allowance. The action is audit-logged (`agent_revoke`).

## Agent side (finish)
Install the fleetmem MCP server, register it with your LLM agent pointing env
`BRAIN_CERT/BRAIN_KEY/BRAIN_CA/BRAIN_TOKEN_FILE` at the agent's paths, then restart the agent.
Verify: `/whoami` returns the agent's name + role; a `brain_recall` returns hits. To renew the
90-day leaf, regenerate a CSR and re-run `sign-agent-cert.sh` (a timer can automate it).

## Notes
- **CA signing needs no external PKI / secrets** — `sign-agent-cert.sh` uses the box's local CA
  (`fleetmem-init-pki.sh`); `PKI_DIR` selects where the CA lives (default `/opt/brain-db/pki`).
- **Roles:** workers get scoped reader-groups + no sensitive tier; managers get broad read +
  propose + enroll-approve. Set the role at approval time.
- If you already run your own PKI, point `PKI_DIR` at it (drop in `ca.crt`/`ca.key`) instead of
  generating a local CA.
