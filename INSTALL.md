# Connect a new agent to a running fleetmem

Wire a NEW agent/body so it can talk to an already-running fleetmem governance API
(`https://YOUR-BRAIN-HOST:8443`). This is per-agent client wiring only — to stand up the
fleetmem **server** itself (Postgres + migrations + PKI + your first manager), run `install.sh`
(Docker packaging is planned), which runs the migrations, calls `fleetmem-init-pki.sh`,
and creates your **genesis manager** (you pick its name — nothing ships with a name baked in).

**Auth is two factors, both required:** an mTLS client cert (CN == the agent name *you choose*,
signed by the box's local CA) **and** a bearer token. `/enroll` is the one cert-exempt route (a
brand-new agent has no cert yet); everything else needs both.

Do these five pieces in order.

---

## 1. Enroll the new agent (get an identity + role)
On the new host, generate a key + CSR (private key stays local, `0600`), then apply via the open
`/enroll` endpoint. **You choose the name** in the CSR CN — it becomes the agent's identity. Full
runbook: `ENROLL.md`.

```sh
mkdir -p ~/.fleetmem/pki && cd ~/.fleetmem/pki
openssl ecparam -name prime256v1 -genkey -noout -out client.key && chmod 600 client.key
openssl req -new -key client.key -out <name>.csr -subj "/CN=<name>/O=fleetmem/OU=agents"
```

POST the application (no creds needed) to `POST /enroll` with `proposed_name`, `purpose`,
`agent_host`, `requested_role` (`manager` | `worker` | `readonly`), and the public `csr`. You get
back an `enrollment_id` + one-time `enroll_secret` — this grants nothing yet. **The required number
of managers must approve** (`brain_enroll_pending` / `brain_enroll_approve`); how many is *your*
config, `ENROLL_APPROVALS` — set it to `1` for a single-manager setup, higher to require several.
Then poll `GET /enroll/status?id=&secret=` ONCE; on approval it returns your **token** (step 3).
Hand the public `<name>.csr` to a manager.

## 2. Sign the client cert (mTLS) — manager side
A manager runs `sign-agent-cert.sh` (the API deliberately holds no CA power). It signs the CSR with
the box's **local CA** (created by `fleetmem-init-pki.sh` at install — plain `openssl`, no external
PKI):

```sh
./sign-agent-cert.sh <name> /path/to/<name>.csr        # -> <name>-client.crt (leaf + local-CA chain)
```

Deliver `<name>-client.crt` back to the agent as `~/.fleetmem/pki/client.crt` (alongside `client.key`
from step 1). The **CA bundle is public** — copy the box's `ca.crt` (from the PKI dir, e.g.
`/opt/brain-db/pki/ca.crt`) to the new host as `~/.fleetmem/pki/ca.crt`. To renew before the leaf
expires, generate a fresh CSR and re-run `sign-agent-cert.sh` (script it on a timer if you like).

Also open the brain host's firewall to the new agent's source IP if needed (agents reach `:8443`).

## 3. Place the bearer token file
Write the token returned by `/enroll/status` to disk, `0600`:

```sh
mkdir -p ~/.fleetmem
# paste the token value into the file (never through chat), then:
chmod 600 ~/.fleetmem/<name>.token
```

The brain stores only the token's hash; this file is the only copy.

## 4. Write the client config — the ONE per-agent file
Create `~/.fleetmem/client.conf` as a plain `KEY=VALUE` file. All client scripts read this, so it is
the **only** per-agent file you edit — point every path at where you placed things above:

```sh
BRAIN_URL=https://YOUR-BRAIN-HOST:8443
BRAIN_CERT=~/.fleetmem/pki/client.crt
BRAIN_KEY=~/.fleetmem/pki/client.key
BRAIN_CA=~/.fleetmem/pki/ca.crt
BRAIN_TOKEN_FILE=~/.fleetmem/<name>.token
```

These keys match the `BRAIN_*` env vars the fleetmem MCP and the hook scripts honour.

## 5. Point the MCP / LLM at the brain, then smoke-test
Register the fleetmem MCP server (`mcp/server.py`) with your LLM agent (e.g. Claude Code) with env
pointing at the same `BRAIN_CERT / BRAIN_KEY / BRAIN_CA / BRAIN_TOKEN_FILE` values from
`client.conf` (add `BRAIN_MODE=worker` for a locked worker). Restart the agent.

> **Python deps:** the MCP server needs `mcp` + `requests`. On the brain host these already live in
> the box's venv (`$PREFIX/venv`, installed by `install.sh` from `requirements.txt` — `mcp` is not in
> distro `apt`). Running the MCP client on a *separate* host? Give it its own venv there:
> `python3 -m venv venv && ./venv/bin/pip install mcp requests`.

Smoke-test with the real `/whoami` curl (same mTLS + bearer form the tools use):

```sh
curl --cert ~/.fleetmem/pki/client.crt --key ~/.fleetmem/pki/client.key \
     --cacert ~/.fleetmem/pki/ca.crt \
     -H "Authorization: Bearer $(cat ~/.fleetmem/<name>.token)" \
     https://YOUR-BRAIN-HOST:8443/whoami
```

Expect your agent name + role. In your LLM agent, the brain tools (`brain_recall`, …) now work. Done.

## 6. (Recommended) Register the SessionStart hook — auto-inject the per-agent brief

So the agent *uses* the brain automatically each session (not only when it remembers to), register
`fleetmem-session-start.sh` as a **SessionStart** hook. It reads the same `~/.fleetmem/client.conf`, and
because the agent is identified by its mTLS cert, the brain assembles the brief for **that agent
only** — its persona + always-on rules, plus its own unread inbox, open tasks, and pending reviews.
If the brain is unreachable it prints the shipped static-core fallback (`session-brief.fallback.md`),
so a cold start is never blind. Requires `curl` + `jq`.

For **Claude Code**, add to the agent's `.claude/settings.json`:
```json
{ "hooks": { "SessionStart": [ { "hooks": [
    { "type": "command", "command": "/absolute/path/to/fleetmem-session-start.sh" } ] } ] } }
```
For other runtimes: run the script at session start and prepend its stdout to the system context.
The script never fails a session (always exits 0).

## 7. Dashboard exposure (only if you run the web UI)

The optional web dashboard (`graph-server.js`) is a **full-read console**: it authenticates to the
brain with its own cert + token and can see everything that identity is scoped to. It has **no
authentication of its own** — it trusts whatever fronts it.

- It binds **`127.0.0.1` by default** (`BIND_HOST` env to change). Left at the default it's reachable
  only from the same host — use an SSH tunnel, or run your mTLS proxy on that host.
- **Exposing it beyond loopback** (`BIND_HOST=0.0.0.0` or a LAN/public IP) means **anyone who can
  reach the port gets your whole brain**. If you do this, you **MUST** front it with the same mTLS
  proxy you use for the API (e.g. nginx, client-cert required) — never expose it raw. It logs a
  `[SECURITY]` warning at startup when bound non-loopback as a reminder.

> **Note (packaging):** the dashboard **is** part of the distributable — `export-brain.py` ships the
> brain-only build under `dashboard/` (mirroring `build-fleetmem.js`: infra views stripped, fleet
> identifiers scrubbed). The security contract above applies whenever you run it. (resolved 2026-07-21.)
