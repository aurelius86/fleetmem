# Using fleetmem

This is the day-to-day manual. It assumes fleetmem is installed and you've created your first
manager (see `AGENTS.md` / `INSTALL.md`). If you just want the pitch, see `README.md`.

fleetmem is a **memory server**. You don't "open" it — your **LLM agent** talks to it, either through
the **MCP tools** (`brain_*`, the normal way) or the **HTTP API** (curl, for scripts). Everything is
gated by your agent's mTLS cert + bearer token, so every read/write is attributed and access-checked.

---

## Where fleetmem fits (vs / with OpenClaw, Hermes, Claude Code, …)
fleetmem is **not** an agent framework — it's the **shared memory layer** those frameworks plug into.
Runtimes like **OpenClaw** and **Hermes Agent** *run* agents (connect them to chat channels, execute
tools, give one agent its own memory). fleetmem is the other half of the stack: a **standalone, shared,
governed memory** that any number of agents — across any framework — point at over MCP.

Reach for fleetmem when you want:
- **One brain shared by many agents**, not a per-agent silo — with per-agent identity, roles,
  reader-groups, and a `personal → shared → trusted` lifecycle.
- **A typed knowledge graph** — recall returns *how* facts relate (supersedes / conflicts_with /
  depends_on), not just similar text.
- **To keep your runtime** — stay on OpenClaw / Hermes / Claude Code / your own loop for *running*
  agents; add fleetmem so they all read and write one access-controlled long-term memory.
- **Full self-hosting** — your Postgres, your local models, your own CA. Nothing leaves your box.

In one line: **OpenClaw/Hermes run the agents; fleetmem is the shared brain you give them.**

---

## The mental model (learn this first)
- **Memory tiers (lifecycle):**
  - `personal` — author-only, private, permanent. Your agent's private scratch/notes.
  - `ready_to_share` — you asked to share it; it sits in a manager review queue.
  - `trusted` — approved & shared to the brain (still filtered by reader-groups + sensitivity).
- **Roles:** `manager` (broad read + propose + approve/enroll), `worker` (scoped, no sensitive tier),
  `readonly` (read only). Set at enrollment.
- **Access controls on every trusted note:** `readers` (which groups may see it) + `sensitivity`
  (`public` / `normal` / `sensitive` / `secret`, capped per role). A worker never sees `sensitive`.
- **Typed knowledge graph:** notes are linked by typed edges (`relates_to`, `supersedes`,
  `conflicts_with`, `depends_on`, `runs_on`, `accessed_via`, `uses`). Recall follows them, so it can
  hand back "this supersedes that" / "this conflicts with that," not just text matches.

---

## Everyday tools (MCP)
Your agent calls these; each returns JSON. Names and shapes below match the shipped MCP surface.

- **`brain_whoami()`** — who am I to the brain. → `{name, role, groups}`.
- **`brain_recall(query, k=5, tags=[])`** — the workhorse. Hybrid (meaning + keyword) search,
  role-filtered, with 1-hop related notes appended (each carrying a `relation` = how it connects).
- **`brain_remember(body, name=…, description=…, mtype=…, sensitivity=…, tags=[])`** — write a
  **personal** memory you can use immediately (author-only). Promote later with `brain_share`.
- **`brain_propose(...)`** — propose a memory straight to the **shared/trusted** review queue.
- **`brain_deep_search(query)`** — deeper, multi-source synthesis when a single recall isn't enough.
- **`brain_get(name)`** — exact lookup of a note you know the name of.
- **`brain_share(id)`** — promote one of your personal notes to `ready_to_share` (review queue).
- **`brain_validate_memory(id, verdict, source_session)`** — when `brain_recall` returns a note with
  `"trusted": false`, that is your own unvalidated capture (it carries `id` + `source_session`). Read
  the source with `brain_get_session_turns(source_session)`, confirm the note matches, then validate:
  `verdict="trusted"` self-trusts it, `"invalid"` deletes it. Do this before relying on an untrusted note.
- **`brain_get_session_turns(session_id)`** — read one past transcript's turns (to validate against).
- **`brain_my_provisional()`** — list your own pending/ready-to-share drafts.
- **Tasks / projects / ideas:** `brain_tasks`, `brain_add_task`, `brain_update_task`,
  `brain_projects`, `brain_add_project`, `brain_ideas`, `brain_add_idea`.
- **Messaging:** `brain_send(to, subject, body)` + `brain_inbox()` + `brain_mark_read(id)`.
- **Infra model:** `brain_infra()` (what runs where, dependencies), `brain_schema()` (what the brain
  holds + how to write/find it), `brain_tags()` (tag facets).

## Manager tools (role = manager)
- **`brain_provisional_pending()`** → review `ready_to_share` drafts; **`brain_provisional_decide(id,
  "graduate"|"delete", …)`** to approve (share) or drop.
- **`brain_proposals()`** / **`brain_proposal_decide(id, "approved"|"rejected")`** — the proposal queue.
- **`brain_edge_proposals()`** / **`brain_edge_proposal_decide(id, "approve"|"reject")`** — approve the
  graph edge-types the classifier proposes.
- **Curation:** `brain_curate_get(name)` / `brain_curate_edit(...)` to rename/amend/retag a live note;
  **`brain_curate_delete(name, reason)`** to soft-delete one (manager/approver only, audited, reversible,
  reason required) — the sanctioned prune path for trusted/semantic notes.
- **Enrollment** is over HTTP (`/enroll`, `/enroll/pending`, `/enroll/<id>/approve`) — see `ENROLL.md`.

## Authoring typed graph links
Write `[[target|rel_type]]` in a memory body to create a typed edge at write time; a plain
`[[target]]` defaults to `relates_to`. Example inside a note body:
> "The API depends on Postgres. See [[postgres-setup|depends_on]] and [[old-design|supersedes]]."

---

## Worked walkthrough (copy-paste)
From your agent (MCP), or via curl (HTTP). HTTP form shown; MCP is the same verbs.

```sh
# 0. confirm identity
curl -s --cert client.crt --key client.key --cacert ca.crt \
     -H "Authorization: Bearer $(cat mytoken)" https://YOUR-BRAIN-HOST:8443/whoami

# 1. store a fact (personal, usable immediately) — via MCP: brain_remember(...)
#    (API-side, personal writes go through the provisional path; MCP brain_remember is the easy way)

# 2. recall it
curl -s --cert client.crt --key client.key --cacert ca.crt \
     -H "Authorization: Bearer $(cat mytoken)" -H "Content-Type: application/json" \
     -X POST https://YOUR-BRAIN-HOST:8443/recall -d '{"q":"what did I learn about X","k":5}'

# 3. propose something for the shared brain -> lands in the review queue
curl -s ... -X POST https://YOUR-BRAIN-HOST:8443/propose \
     -d '{"name":"deploy-runbook","body":"steps ...","mtype":"reference"}'

# 4. (manager) review + approve  ->  brain_provisional_pending / brain_proposal_decide
```

---

## HTTP API quick reference
Every route needs mTLS + `Authorization: Bearer <token>` except open enrollment.
- `GET /healthz` · `GET /whoami`
- `POST /recall` `{q,k,tags}` · `POST /deep-search`
- `POST /propose` · `GET /proposals` · `GET /memories` · `GET /memory/<name>` · `GET /graph`
- `GET/POST /tasks` · `GET/POST /projects` · `GET /ideas`
- `POST /messages` · `GET /inbox` · `POST /messages/<id>/read`
- `GET /enroll` (open) · `POST /enroll` (open) · `GET /enroll/pending` · `POST /enroll/<id>/approve`
- `GET /timeline` · `GET/POST /agent-config` · `GET/POST /graph-config`

The MCP tools wrap these with the same names/fields — point your agent's MCP client at the brain
(env: `BRAIN_URL`, `BRAIN_CERT`, `BRAIN_KEY`, `BRAIN_CA`, `BRAIN_TOKEN_FILE`) and call `brain_*`.

## Checking your invariants (`drift_check.py`)
Your project plan (`project_doc`) can record, per project, the **invariants** each flow must uphold.
Any `invariant` section may embed a fenced ` ```check ` block (a YAML list of checks — `kind: sql |
grep_present | grep_absent`). `drift_check.py` turns those into a pass/fail report so the plan can't
silently drift from the code:

```bash
# run ON the brain host, as the brain service user, from the db dir
python3 drift_check.py <project-slug> [--verbose]
```

It's safe by construction: the DB session is **read-only** and grep runs via argv (no shell). Exit
`0` = all invariants hold; `1` = any check failed or errored.

## Measuring recall quality (`golden_pipeline.py`)
fleetmem can score its own retrieval against a **golden set** — a list of `{q, expect}` cases where
`expect` names the memory that *should* come back top-1. `golden_pipeline.py` runs the set and reports
hit-rate / MRR so you can catch a recall regression before it bites.

**Bring your own golden set.** The shipped `golden.example.json` is a *template*, not a working set —
its entries are generic placeholders. Recall quality is only meaningful against **your own** memories,
so copy it to `golden.json` and replace the entries with real queries you care about and the memory
names you expect them to return:

```bash
cp golden.example.json golden.json          # then edit: your queries + expected memory names
python3 golden_pipeline.py golden.json       # run ON the brain host, from the db dir
```

An empty or placeholder golden set will score near-zero — that's the template talking, not your brain.

## Background maintenance (systemd timers)
`install.sh` installs a handful of `oneshot` timers (`systemd/brain-*.timer`) that keep the brain
healthy: edge classification, memory signature verification, re-embedding, retention pruning, and the
**validate-on-recall backstop sweep** (`brain-validate-sweep`, `validate_sweep.py`) — a daily census
of untrusted personal notes that soft-deletes only those whose source transcript was *provably* pruned
(set `VALIDATE_SWEEP_DELETE=0` for census-only). See each `*.service` for what it runs.
