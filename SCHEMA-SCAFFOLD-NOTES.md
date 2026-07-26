# Schema scaffold notes — intentional dead columns / enums / indexes

> **Purpose (/A3, 2026-07-04):** the 2026-07-04 audit flagged a set of columns, enum values,
> and indexes that exist in the live schema but are never written by any code path. This file
> records which are **deliberate contract scaffold** (keep — the Table-Contract or a forward-planned
> feature owns them) vs **prunable** (safe to drop in a future migration). Written so the next audit
> doesn't re-flag them as bugs. Nothing here is a code change — it is the decision record.

## Intentional scaffold — KEEP

These come from `contract.py` (the KIND column-set scaffolder) or are forward-provisioned for a
planned feature. They are *supposed* to be present-but-unused on some tables.

- **`memory.valid_at`** — temporal-validity scaffold from the `knowledge` contract. Only `invalid_at`
  is written today (soft-invalidate); `valid_at` is the "effective from" half, reserved. Keep.
- **`proposal.valid_at` / `proposal.invalid_at` / `proposal.deleted_at`** — the proposal table mirrors
  the knowledge contract's temporal + soft-delete columns for uniformity; the proposal lifecycle uses
  `status` (pending/keep/reject) instead, so these stay NULL. Keep (contract uniformity).
- **`memory_relation.readers` / `memory_relation.sensitivity`** — access-column scaffold on the edge
  table. Edge visibility is derived from the two endpoint memories' own access at read time, so the
  edge's own copies are never set. Keep (contract), or prune if edge-level access is ruled out for good.
- **`session.project` / `session.title` / `session.deleted_at` / `session.readers`** — structure/
  knowledge contract columns on `session`; sessions are keyed by `source_session` and gated via the
  owning session's access, so these default and stay. Keep.
- **`enrollment.csr` / `enrollment.token_hash`** — enrollment PKI scaffold. The live enroll flow signs
  the CSR out-of-band and stores the token hash on the `agent` row, so these enrollment-side columns
  are unused. Keep (documents the intended contract) unless the enroll flow is finalized to not need them.
- **Boilerplate contract columns on `agent` / `enrollment_approval`** — timestamps/soft-delete added by
  the structure contract; harmless. Keep.
- **`agent.revoked_at`** — now WRITTEN as of (the `POST /agent/<name>/revoke` kill-switch); auth
  rejects a revoked agent. No longer dead. (Recorded here because the audit listed it as read-only.)

## Prunable — safe to drop in a future migration (low value, not scheduled)

- **`proposal.status = 'superseded'`** — a legal enum value that no code path ever sets. Prunable from
  the CHECK, or keep as a documented reserved state. Left in place (harmless; a drop is a CHECK change).
- **`memory_relation.rel_type = 'invalidated_by'`** — ontology value never written. Same call as above.
- **Duplicate unique index on `memory_relation`** — migration `0001` adds a UNIQUE **constraint** on the
  edge natural key and `0013` adds an equivalent unique **index**; they overlap. The `0013` index is the
  redundant one. Dropping it is safe but low-value (one extra index maintained on a small table); left
  in place. If pruned, do it via a numbered migration with a `down()` that recreates it.

## How to prune (if ever)

All of the above are DDL — changes go **only** through a numbered `migrations/NNNN_*.py` module with
`up()`/`down()`, applied via `python3 migrate.py up` on the brain host (never ad-hoc DDL). None are urgent;
they are recorded here precisely so they can stay un-pruned without being mistaken for defects.
