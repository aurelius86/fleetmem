# Security model & disclosures

fleetmem is a self-hosted, single-tenant memory server for AI agents. This document states its security
posture honestly — what is enforced where, and the deliberate trade-offs — so you can decide whether it
fits your threat model, and harden further if you need to.

## Authentication — two factors
Every request to the public listener (`nginx` on `:8443`) requires **both**:
1. an **mTLS client certificate** (CN = the agent name), signed by the box's own local CA; and
2. a **bearer token** (stored server-side only as a SHA-256 hash).

The single exception is `POST /enroll` + `GET /enroll/status` — a brand-new agent has no cert yet, so
these two routes are mTLS-exempt and authenticated by a one-time enrollment secret instead. Manager
approval (configurable N-of-M via `ENROLL_APPROVALS`) is required before an enrolling agent gets a
usable identity.

## PKI — the box mints its own
`fleetmem-init-pki.sh` creates a local CA + server cert at install with plain `openssl`. **No key
material ships**, and no external/cloud PKI is involved. Private keys never leave the host and are
`0600`. Distribute only the public `ca.crt` to agents. Rotate a leaf by re-running `sign-agent-cert.sh`.

## Authorization — Row-Level Security + the app layer
The API connects to Postgres as a **non-owner role `brain_app`**, so PostgreSQL Row-Level Security
(RLS) is genuinely enforced beneath the application (the table owner would bypass it). Per request the
app stamps `app.agent` / `app.role` / `app.groups`, and the policies read them.

**RLS is enforced on the content tables** — `memory`, `task`, `project`, `idea` — as a database
backstop for read isolation and manager-only hard-delete, independent of the app's own `WHERE` clauses.
The model per row: an owner (`author_body`/`created_by`), a share tier (`personal → ready_to_share →
trusted`), reader groups (`readers[]`), and a per-role sensitivity ceiling (`access_config`).

**Deliberate boundary:** the remaining tables — `agent`, `enrollment`, `enrollment_approval`,
`message`, `proposal`, `lesson`, `memory_relation`, `config`, `infra_*`, `session*` — are gated at the
**application layer**, not by RLS. Several are auth/enrollment-critical (the auth path reads `agent` on
every request) or system/flow tables where an over-eager RLS policy would risk locking the service out.
This is single-layer authorization for those tables by design; if your threat model needs a DB backstop
there too, extend the RLS pattern in `migrations/0020`/`0032` to them and test every write path.

**Least privilege for `brain_app`:** it is `LOGIN`, non-owner, no `BYPASSRLS`, and its `DELETE` grant is
scoped — revoked on tables the app never hard-deletes (`memory` is soft-deleted via `UPDATE`;
`agent`/`enrollment`/`enrollment_approval`/`lesson`/`message`/`proposal` are update-only/append-only).

## The loopback API hop (accepted exception)
`gunicorn` binds the API to `127.0.0.1:5000` and `nginx` fronts it on `:8443` with mTLS. Traffic on
the internal `:5000` hop is plain HTTP and carries only the bearer factor — **a process already on the
box that hits `127.0.0.1:5000` directly bypasses the mTLS factor.** This is acceptable for a
single-tenant box (reaching loopback already implies host access). To harden: firewall `:5000`,
put the API on a unix socket, or run it in its own namespace.

## Transport hardening
`nginx` serves TLS 1.2 + 1.3 with an AEAD-only cipher list, server cipher preference, a modern curve
list, and sends HSTS (`max-age=63072000; includeSubDomains`). Tune `deploy/nginx-brain-api.conf` for
your environment.

## What does NOT ship
No secrets, no key material, no private IPs/hostnames, no pre-made identities. The database starts
empty. The packaging tool (`export-brain.py`) fails closed if any of these leak into a build.

## Reporting a vulnerability
Please report privately via a GitHub Security Advisory on the repository (do not open a public issue for
security problems). Include a description, affected version/commit, and a reproduction if you have one.
