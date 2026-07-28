# Changelog

All notable changes to fleetmem are recorded here. Versions follow semantic versioning.

## Unreleased
- **Fixed:** `vet_similarity_links.py` crash (`AttributeError: 'str' object has no attribute 'get'`) when the model returns non-dict entries in `links` — now guarded. Surfaced by `doctor.py` on the first live run.
- **Fixed:** `deploy/pull_deploy.py` GitOps deploy now supports an **empty `SUBDIR`** (the repo root *is* the code tree, e.g. `fleet/brain-codes`) — the archive prefix was `"<root>//"` and matched nothing. Defaults + `pull-deploy.env.example` centralized here and repointed off the retired legacy monorepo `db/` subtree.

## v0.1.8 — 2026-07-27
Upgrade/installer robustness — from an external v0.1.0→v0.1.6 upgrade field report (Waves 1–2 of 3;
Wave 3, the fleetmem→fleetmem upgrade note, ships with the rebrand).
- **Added:** **`doctor.py`** (`python3 doctor.py`) — one-command health check that surfaces the silent problems: non-UTF8 database, migration drift/pending, missing venv, **stale systemd unit paths, and failed `brain-*` timers**. Self-skips the systemd checks off a systemd host (CI). Exit 0 = healthy.
- **Added:** **`GET /healthz/detail`** — the machine-readable companion to `doctor.py` (version, db, encoding, applied/pending/drift migrations); 200 when healthy, 503 otherwise. Unauth like `/healthz` (behind the mTLS gate).
- **Changed:** **`update.sh` is now robust across the system-python → venv re-platform** — it builds `$PREFIX/venv` if missing, installs `requirements.txt`, **rewires the systemd `ExecStart`** to the venv interpreter (+ `daemon-reload`), and fails loudly on a missing `python3-venv`. `UPGRADING.md` names `cp -a` for copy-installs and documents the venv/unit steps + `doctor.py` verify.
- **Added:** **`migrate.py reconcile [--yes]`** — re-hashes already-applied migrations whose file was intentionally, schema-neutrally edited (a comment/genericization change), so `up` stops aborting on the drift guard. Shows the drift and requires `--yes` to apply; runs **no DDL**; logs a `reconcile_migration` `action_log` row. The `up` abort message now points at it. Root fix documented in `CONTRIBUTING.md`: **shipped migrations are append-only** (never edit a checksummed `NNNN_*.py`).
- **Added:** **non-UTF8 (`SQL_ASCII`) database guard.** `migrate.py up` now **refuses** (and `status` warns) when the database encoding isn't UTF8 — a `SQL_ASCII` DB silently rejects non-ASCII writes and `PGCLIENTENCODING` can't fix it (the server encoding is what rejects). `install.sh` warns on a pre-existing non-UTF8 DB; `/healthz` reports `encoding`; `smoke_test.sh` adds a non-ASCII round-trip check; `UPGRADING.md` documents a copy-paste re-encode recipe (incl. the PG15 `GRANT ALL ON SCHEMA public`).
- **Changed:** the API now returns **JSON on every error** (a blanket `@app.errorhandler`) — an MCP/HTTP client gets a machine-readable reason instead of Flask's opaque HTML 500. `HTTPException`s keep their status + description; unhandled errors log a full traceback and return `{"error": …}`, 500.

## v0.1.7 — 2026-07-26
Autolearn quality — a fragmentation guard, and the first change shipped via the new pull-based GitOps deploy.
- **Added:** **autolearn sibling-merge guard** — `merge_siblings()` folds same-session near-sibling candidates into one note after extraction, so one unit of work no longer lands as ~5 fragments (e.g. `brain_metrics_implementation_{pattern,location,safety_pattern,…}`). Deterministic + high-precision: two candidates merge only when they share a ≥3-token name prefix **or** have near-identical bodies (Jaccard ≥ 0.6), **and** come from the same session. Merging **concatenates** bodies (no information dropped), takes the most conservative trust, and unions cited channels. Complements the per-candidate judge/backstop, which judge each note in isolation and so can't see fragmentation. 6 new unit tests + full 105-test autolearn suite green.

## v0.1.6 — 2026-07-20
Recall-quality release — on-demand ranked recall, span-level compression, a skill-corpus browser, a fix to the recall regression harness, plus a sanctioned audited memory-delete tool.
- **Added:** **`brain_curate_delete(memory_id, reason)`** — a sanctioned, audited manager delete path for any live memory (resolved by id **or** name). Manager/approver only; requires a non-empty `reason`; soft-deletes (sets `deleted_at`+`invalid_at`, reversible by nulling them) and writes an `action_log` `curate_delete` row. Fills the gap between `brain_validate_memory` (untrusted-personal only) and `brain_revoke` (the agent kill-switch), so retiring a stale trusted/semantic note no longer requires raw SQL. Completes the curation family (`curate_get` / `curate_edit` / `curate_delete`); its edges drop out automatically (relation queries already filter soft-deleted rows).
- **Added:** **on-demand ranked recall** — an opt-in `rank` flag on `brain_recall` (and `rank` in the `/recall` body). When set, the content hits are reranked by the local LLM before returning; it is deliberately **off the per-turn default** (base hybrid recall is already at ceiling for a well-curated brain, and ranking regresses it at that scale), and it **fail-safes to the original order** on any model error/timeout, so recall never stalls. Internally the `recall_core` parameter is `do_rank` (avoids a name clash with the RRF fusion loop counter).
- **Added:** **span-level recall compression** — `RECALL_SPAN_COMPRESS` (off by default) trims each recalled body to its most query-relevant spans (cheap lexical scoring, no embed calls; the first span is always kept for context), controlled by `RECALL_SPAN_COUNT` and `RECALL_SPAN_MIN_CHARS`. Roughly 85% body-size reduction on long notes with the answer span preserved; the full body is always fetchable via `brain_get`, and a `body_compressed` flag signals it. Serves thinner injected context without changing *which* notes are recalled.
- **Added:** **skill-corpus browser** — `GET /skill/list` returns all skills as light rows; a new **Skills** tab on the dashboard lists/filters them and loads a full body on click (`/skills.json` + `/skill-body.json` proxies). The recall knobs (incl. the new `RECALL_SPAN_*`) surface automatically in the Config tab.
- **Fixed:** **recall regression harness was blind.** The golden harnesses (`golden_pipeline.py`, `golden_regression.py`) ran `recall_core` under a synthetic `{"name":"manager"}` identity whose `readers` resolved empty, so `readers && '{}'` hid **every** trusted note and hit@k collapsed to noise — a benchmark-only regression, not a recall regression. They now run under a full-visibility `see_all` identity; `expect` may be a single name or a list of acceptable names; and the alert floor was recalibrated. Restores the eval to its 39/41 (95%) ceiling.
- **Added:** **recall resilience.** A transient embedder failure (HTTP 503 "server busy" when its queue is momentarily full, or a connection reset/timeout) no longer instantly collapses recall to keyword-only — `embed()` now does a small bounded backoff-retry (`EMBED_RETRIES`, `EMBED_RETRY_DELAY`) to ride out the blip. And when the dense arm *does* stay down, the degradation is surfaced instead of silent: `keyword_only` recall emits a distinct `recall_degraded` audit action and the `/recall` response carries a `degraded: "embedder_unavailable"` field — so a semantic-arm outage is visible immediately rather than quietly returning weaker results.

## v0.1.5 — 2026-07-15
Feature release — session rebuild, orphan repair, entity-linking recall, and per-agent config.
Ported from the live fleet (2026-07-14).
- **Security:** `requests` 2.32.3 → **2.32.4** — 2.32.3 and earlier can leak `.netrc` credentials to a
  third-party host on a maliciously-crafted URL (CVE-2024-47081).
- **Changed:** `Flask` 2.2.5 → **3.1.3** (with Werkzeug 3). fleetmem only uses Flask's stable core
  (`Flask`, `request`, `jsonify`, `g`) and none of the APIs 3.0 removed, so this is a drop-in: all 90
  routes register, the app imports with zero deprecation warnings, and CI passes on Python 3.11.
- **Fixed:** `psycopg2-binary` 2.9.10 → **2.9.12** — 2.9.10 ships no wheel for **Python 3.14**, so a
  fresh install on 3.14 tried to build from source and failed. 2.9.12 has cp314 wheels.
- **Added:** manual, source-grounded **session rebuild** — `rebuild_session.py` re-extracts a session's durable memories + typed relations from its transcript and (with `--apply`) writes the NEW ones as quarantined/personal notes (deduped by name/content-hash/cosine) plus grounded typed edges. Exposed as manager-only `POST /session/<sid>/rebuild` and a "Session rebuild" panel on the dashboard System tab (`/session-rebuild` proxy route + session picker).
- **Added:** `repair_orphans.py` — an LLM repair job that connects memories with no edges. For each orphan it shows the model its semantic + entity candidates and asks (strict, JSON-constrained, abstain-if-none) which are genuinely related, then creates `relates_to` edges — **edges only, note bodies are never modified**. Registered in `JOB_UNITS` so the dashboard exposes Run-now + schedule controls; `classify_edges` types the new edges on its next run.
- **Fixed:** the autolearn worthiness judge is now resilient to a transient model cold-load/eviction — `keep_alive:-1` (stay warm), a 45s timeout, and one retry (a cold-load times out the first call but warms it for the retry). Still fail-open after retries. Corrects an earlier over-reaction that swapped in a slower model on a transient failure.
- **Added:** entity-linking as a first-class recall signal (migration `0037` `memory_entity` junction + `populate_memory_entity.py`). `recall_core` gains a dedicated entity-expansion step: after graph 1-hop, it pulls in memories sharing a *rare* entity (task-ids, containers, hosts, `.py` files) with the query or the content hits, weighted by rarity (hub entities skipped) — knobs `ENTITY_EXPAND_WEIGHT`/`ENTITY_EXPAND_HUBCAP`. New memories self-index their entities on write (agent + autolearn paths); a batch re-populate refreshes curated-registry entities.
- **Added:** per-agent config surface — `GET /agents`, `GET /agent/<name>/injection-preview` (read-only: shows exactly what `/bootstrap` injects for an agent — welcome + role-filtered always-on rules + global overlay + pinned instruction-memory names), and `PATCH /agent/<name>` (role/lane/tier/sensitivity/autoapprove_own/welcome; manager-only, audited, with a self-demote lockout guard).
- **Added:** `/stats/tool-usage` now also returns a gap-filled daily `series` + `series_by_agent`, powering a usage-over-time view of per-agent brain-tool adoption.
- **Added (dashboard):** an **Agents** tab (per-agent config form + session-start injection preview); a glassmorphism usage-over-time chart with a per-agent filter; gzip on `/graph.json` (~10x smaller transfer); and scale-aware render simplification so large knowledge graphs stay smooth.
- **Changed (dashboard):** the approval tab now surfaces the genuine-needs `/needs-you` view (share-requests + human-vouch), with the full proposal queue moved to a secondary "Proposal queue" tab; removed a duplicated edge-type approval block from the System page.
- **Fixed:** the autolearn worthiness judge + extractor now pass `"think": false` to the generation model — a hybrid reasoning model's `<think>` preamble otherwise consumed the judge's tiny token budget and made it fail open (keep everything).
- **Fixed:** memory linking — the personal-write path now also creates co-use `relates_to` edges (previously autolearn-ingest only), so directly-written notes auto-link to their semantic neighbours instead of landing orphaned.
- **Changed:** the query-entity extractor's site/host IP-subnet pattern is now config-driven via `ENTITY_IP_PREFIXES` (comma-separated dotted prefixes in `brain.env`, e.g. `10.0.0.`) instead of any hardcoded subnet — no site-specific IP range lives in the code. Unset by default (a public install has no site subnet, so no IP entities).

## v0.1.4 — 2026-07-12
Launch polish (pre-public audit pass).
- **Added:** GitHub Actions CI (`.github/workflows/ci.yml`) — compiles every Python file, installs
  pinned dependencies, runs the sensitivity-routing unit test, applies all 36 migrations against a
  fresh `pgvector` Postgres, and scans for leaked key material or private endpoints on every PR.
- **Added:** `test_pick_backend.py` now ships — the standalone proof that sensitive/secret content
  never routes to a cloud extraction backend, runnable on any box (`python3 test_pick_backend.py`).
- **Added:** `/healthz` reports the running release version (`FLEETMEM_VERSION`, single source of truth
  in `api.py`), so an operator can confirm what a live box runs.
- **Changed:** README — architecture diagram (Mermaid), CI/license/version badges, and a note that
  Docker packaging is planned (install remains systemd + venv).
- **Fixed:** `install.sh` now creates the database with an explicit `UTF8` encoding
  (`template0` + `C.UTF-8`) — on a C-locale host the previous bare `createdb` minted a `SQL_ASCII`
  database that corrupted non-ASCII text (caught by our own smoke test during a fresh-install audit).
- **Fixed:** `smoke_test.sh` client-cert handling — the live-API steps now honour
  `BRAIN_CERT`/`BRAIN_KEY`/`BRAIN_CA` env (a server box's PKI dir holds no client cert), and a
  cert-less `/healthz` probe that completes TLS and gets nginx's 403 counts as "front door up, mTLS
  enforced" instead of a false FAIL.
- **Fixed:** `fleetmem-bootstrap-manager.py` printed a stale MCP registration (`python -m fleetmem_mcp`);
  it now prints the real venv-python + `mcp/server.py` command. `INSTALL.md` no longer implies a
  Docker Compose install ships.

## v0.1.3 — 2026-07-09
Security hardening (pre-public review).
- **Added:** Row-Level Security on the `task`, `project`, and `idea` tables (migration `0032`) — a
  database-level backstop for read isolation and manager-only hard-delete, matching what `memory`
  already had. Verified it does not affect the app's own read/write paths.
- **Changed:** scoped the app role's privileges — `brain_app`'s `DELETE` is revoked on tables the app
  never hard-deletes (`memory` is soft-deleted; `agent`/`enrollment`/`lesson`/`message`/`proposal` are
  update-only/append-only).
- **Changed:** nginx TLS hardening — AEAD-only cipher suite, server cipher preference, modern curves,
  and an HSTS header.
- **Added:** `SECURITY.md` documenting the auth model, PKI, the RLS/app-layer authorization boundary,
  the loopback API-hop trade-off, and how to report a vulnerability.
- **Changed:** `CONTRIBUTING.md` simplified to plain inbound=outbound AGPL-3.0 (dropped the
  dual-license contributor grant).

## v0.1.2 — 2026-07-09
Bug-fix release — the installer now actually produces a working, secure box (found by end-to-end
testing on clean VMs).
- **Fixed (critical):** on a default install the API returned HTTP 500 on *every* request
  (`peer authentication failed for user "brain_app"`). The Row-Level-Security setup makes the API
  connect as the non-owner role `brain_app`, but nothing let the service user authenticate as it over
  the local socket. `install.sh` now creates `brain_app` as a `LOGIN` role with a `pg_ident` map so
  Row-Level Security is genuinely enforced on a fresh install.
- **Fixed:** the pinned Python dependencies (and the `mcp` SDK) never actually installed — the
  installer used only distro `apt` packages, so the box ran older versions and the MCP server failed
  with `ModuleNotFoundError`. `install.sh` now builds a dedicated venv from `requirements.txt`; the
  API, MCP server, and all maintenance timers run from it. (Security: `gunicorn` 23.0.0, `requests`
  2.32.3.)
- **Fixed:** the optional remote MCP-over-HTTP service pointed at non-existent paths, auto-started
  before enrollment (crash-looping), and could not find its bearer token. It is now installed but
  opt-in (enable it after creating your first manager), runs from the venv, and reads your
  `~/.fleetmem/client.conf`.
- **Fixed:** the maintenance timer units had hardcoded paths that a non-default install prefix never
  reached; the installer now rewrites every unit's paths and interpreter.
- **Changed:** `requirements.txt` uses `psycopg2-binary` (prebuilt wheel) so the venv installs with no
  C toolchain on the target host.

## v0.1.1 — 2026-07-09
Bug-fix release — fresh-install robustness, from early-tester feedback.
- **Fixed:** a fresh install was missing the `agent.autoapprove_own` column, so `POST /autolearn/extract`
  returned HTTP 500 (`column does not exist`). Added migration `0031`.
- **Fixed:** the local CA was minted without `basicConstraints`/`keyUsage`, which modern OpenSSL (3.x)
  and Python 3.13 reject — agents could not complete the mTLS handshake. `fleetmem-init-pki.sh` now sets
  `CA:TRUE` + `keyCertSign,cRLSign`.
- **Fixed:** database/utility scripts assumed a UTF-8 locale and could raise `'ascii' codec` errors on a
  C/POSIX-locale host — every `psycopg2.connect()` now forces `client_encoding=UTF8`.
- **Fixed:** the optional `fleetmem-git-sync.example.sh` backup mangled any memory whose body spans
  multiple lines; it now exports via a multiline-safe emitter.
- **Fixed:** client scripts crashed with a raw traceback when run before enrollment; they now print a
  clear "complete enrollment (see INSTALL.md / ENROLL.md)" message and exit.
- **Added:** `.gitattributes` (LF line endings) so Windows clones can run `install.sh`, and
  `smoke_test.sh` for a fast post-install sanity check (migrations applied, CA valid, `/healthz`).

## v0.1.0 — 2026-07-08
Initial release.
- **Memory + knowledge-graph server:** hybrid retrieval (dense vector + keyword), a typed
  knowledge graph (supersedes / conflicts_with / depends_on / …), and recall that returns how
  notes connect.
- **Governance:** per-agent mTLS + bearer-token auth, roles (manager / worker / readonly),
  per-memory reader groups + sensitivity ceiling, and a `personal → shared → trusted` lifecycle.
- **MCP-native** tool surface for any MCP-capable LLM agent.
- **Self-contained security:** the box mints its own local CA + certs at install; no credentials
  or keys ship. Empty database — no data ships.
- **Install + onboarding:** bare-metal `install.sh` (Debian/Ubuntu), agent-driven `AGENTS.md`
  onboarding, one-time genesis-manager bootstrap, and `update.sh` for in-place upgrades.
- **License:** GNU AGPL-3.0.
