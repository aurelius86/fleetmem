# Contributing to fleetmem

Thanks for wanting to improve fleetmem. A couple of ground rules keep the project healthy.

## License of contributions (inbound = outbound)
fleetmem is licensed under the **GNU AGPL-3.0** (see `LICENSE`). By submitting a
contribution (a pull request, patch, or any code/docs), you agree that your
contribution is provided to the project and its users under the **same AGPL-3.0**
terms as the rest of fleetmem. You keep the copyright to your own contribution.

You confirm you have the right to submit the work (it is yours, or you are
authorized to submit it). Opening a pull request constitutes acceptance.

## Migrations are append-only
Never edit a migration file (`migrations/NNNN_*.py`) once it has shipped or been applied anywhere —
its `sha256` is recorded in `schema_migrations`, so **any** change (even a comment) trips the drift
guard on every existing install and blocks `migrate.py up`. To change the schema, add a **new**
numbered migration. To genericize wording for the public mirror, do it in `export-brain.py`, not in
the tracked file. If a shipped migration truly was edited intentionally and is schema-neutral,
`migrate.py reconcile` re-hashes it to the current file (review the diff first).

## How to contribute
- Open an issue first for anything non-trivial so we can agree on the approach.
- Keep changes focused and match the surrounding code style.
- Because fleetmem is AGPL: if you run a modified version as a network service,
  you must make your modified source available to its users — and please send
  improvements upstream so everyone (and the maintainer) benefits.
