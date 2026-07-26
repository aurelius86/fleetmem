# fleetmem — agent onboarding kit

**If you are an LLM agent (Claude Code or similar) reading this: this file is your runbook.** Your
job is to explain fleetmem to the human in plain language, then set it up *with their input* — you drive
the deterministic scripts; they make the choices (their manager name, how many managers, optional
git-sync). Do not invent identities or pick policy for them.

---

## 1. Explain it to the user (in your own words, briefly)
> **fleetmem is a self-hosted long-term memory + knowledge-graph server for AI agents.** It stores facts,
> decisions, and lessons; finds them by meaning (hybrid vector + keyword search); links them into a
> typed knowledge graph (supersedes / conflicts_with / depends_on / …); and governs who sees what
> (per-agent mTLS + roles; a `personal → shared → trusted` lifecycle). Agents talk to it over MCP, so
> any LLM gains durable, shared, access-controlled memory. It runs entirely on the user's own hardware —
> no data leaves their box, and it ships with **no memory and no credentials of anyone else's**.

Then confirm they have the prerequisites: PostgreSQL 15+ with the `pgvector` extension, Python 3.11+,
`openssl`, and a local LLM endpoint (Ollama or compatible) for embeddings/extraction. The install
shape (Docker Compose or `install.sh`) can provide Postgres + the service for them.

## 2. Bring up the server (once)
Prefer the install shape (`install.sh`) — it does these for you, including a dedicated Python
**venv** at `$PREFIX/venv` holding the pinned deps from `requirements.txt` (Flask, gunicorn,
psycopg2-binary, PyYAML, requests, **and `mcp`**). Distro `apt` ships older versions and has no
`mcp`, so every fleetmem process (API, MCP server, `migrate.py`, hygiene timers) runs from that venv.
Manual equivalent:
```sh
# a. one-time DB prep AS A POSTGRES SUPERUSER: create the DB + the pgvector extension
#    (migrations use the `vector` type, so the extension must exist first)
#    Force UTF-8 so non-ASCII text (em-dash, accents, arrows) round-trips even on a C/POSIX-locale
#    host — a bare `createdb` there yields a SQL_ASCII DB that 500s on the first unicode write.
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PGCLIENTENCODING=UTF8
createdb --encoding=UTF8 --template=template0 brain \
  && psql -d brain -c 'CREATE EXTENSION IF NOT EXISTS vector'

# b. Python deps — into a venv so versions are predictable and `mcp` is present
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# c. schema — apply all migrations against the empty DB (run FROM the venv)
./venv/bin/python migrate.py up   # reads PGDATABASE (default: brain)

# d. config — copy the example and edit for your environment
cp brain.env.example brain.env    # set OLLAMA_* endpoints, models, and knobs

# e. PKI — the box mints its OWN local CA + server cert (no external PKI)
BRAIN_HOST=<your-brain-host> [BRAIN_IP=<ip>] ./fleetmem-init-pki.sh

# f. front the API with nginx (deploy/nginx-brain-api.conf) using the pki/ paths, start the API
```
Verified 2026-07-08: steps a–b bring up all 30 migrations (26 tables) on a fresh empty Postgres,
then `seed_agents.example.py` / the genesis bootstrap creates your first manager — brain starts with
zero memories.

## 3. Create the FIRST manager (ask the user for the choices)
Ask: **(1) the name they want for their first manager** (they choose — nothing is preset), and
**(2) how many managers must co-approve a new agent later** (`ENROLL_APPROVALS`; 1 for a solo setup,
more to require several). Then run the one-time genesis script *on the brain host*:
```sh
FLEETMEM_MANAGER=<their-chosen-name> ENROLL_APPROVALS=<their-choice> ./venv/bin/python fleetmem-bootstrap-manager.py
```
It mints that manager's mTLS cert (via the local CA) + a bearer token (shown **once**), inserts the
manager row, and writes `~/.fleetmem/{client.conf, <name>.token, pki/*}`. Record their
`ENROLL_APPROVALS` choice in `brain.env`. This script refuses to run once a manager exists.

## 4. Wire yourself (the LLM) to the brain
Register the fleetmem MCP server with the agent, using the env the genesis script printed
(`BRAIN_URL / BRAIN_CERT / BRAIN_KEY / BRAIN_CA / BRAIN_TOKEN_FILE`). Restart the agent, then
smoke-test — `/whoami` should return the manager's name + role, and `brain_recall` should work
(empty results at first, since the brain ships with no memory).

Also register the **SessionStart hook** (`fleetmem-session-start.sh`) so the brain's per-agent brief
(persona + always-on rules + this agent's inbox/tasks/reviews) is injected every session, and the
agent uses the brain without being told — see `INSTALL.md` §6. It falls back to a shipped static
core (`session-brief.fallback.md`) if the brain is briefly unreachable.

Then **ask the user for their house rules** — e.g. *"Any standing rules or context you want every
session to start with?"* — and write their answer as the **global overlay** with
`brain_session_overlay_set(text=…)`. From then on it's injected (raw) into every agent's session
brief. You can also give one agent its own persona/job with
`brain_session_overlay_set(text=…, scope="<agent-name>")`. This is the "push some context each
session" habit, made a one-time setup step.

### Which brain tool, when (per turn)
The brief teaches the *habits*; this is the quick map from a situation to the tool. Agents lean hard on
`brain_recall` and forget the rest — reach past it when the situation calls for it:

| When you… | Use |
|---|---|
| need a saved fact/decision/preference (routine lookup) | `brain_recall` |
| have a hard, multi-fact "dig up **everything** about X" question | `brain_deep_search` (wider pool + sourced synthesis) |
| need something from past **chat history** ("did we discuss / when did I say X") | `brain_search_transcripts` |
| confirm a durable fact/decision/gotcha | `brain_remember` (personal) or `brain_propose` (shared review) |
| have a future-work thought ("we should someday…") | `brain_add_idea` |
| need to see / add / update work | `brain_tasks` / `brain_add_task` / `brain_update_task` |
| start a session | read `brain_inbox`, then review your own drafts (`brain_provisional_pending`) |

## 5. Add more agents later (optional)
Any additional agent (another manager, or a scoped worker) joins via the enrollment flow in
`ENROLL.md`: they apply at `/enroll`, the required number of managers approve, a manager signs their
CSR with `sign-agent-cert.sh`, they install cert + token. You never need to re-run genesis.

## 6. Git backup / sync (optional — only if they want it)
fleetmem is fully functional without it. If the user wants an off-box markdown backup of their memories
in their own git repo, see `fleetmem-git-sync.example.sh` — copy it, point it at *their* remote, and run
it on a timer. This is opt-in and uses only the user's own repo; nothing is sent anywhere by default.
