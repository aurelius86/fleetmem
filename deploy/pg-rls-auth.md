# Postgres auth for the RLS non-owner role (`brain_app`) — the brain host

Part of the RLS cutover. `brain-api` connects as `brain_app` (see `brain-api.service.d-rls.conf`)
so RLS policies apply. `brain_app` authenticates over the **local unix socket** with **peer** auth
mapped from the OS user `brain` — no password, no secret on disk (the point: the app never holds a DB
credential; identity is the OS user + the ident map).

## `/etc/postgresql/16/main/pg_hba.conf`
Add (kept ABOVE the generic `local all all` line so it matches first):
```
local   all             brain_app                               peer            map=brainmap
```

## `/etc/postgresql/16/main/pg_ident.conf`
```
# MAPNAME   SYSTEM-USERNAME   PG-USERNAME
brainmap    brain             brain_app
brainmap    postgres          brain_app
```
`brain` → the `brain-api` service user (User=brain). `postgres` → lets the superuser test as `brain_app`
(`sudo -u postgres psql "user=brain_app dbname=brain"`) for the RLS harness.

## Apply
```
sudo systemctl reload postgresql   # pg_hba/pg_ident are re-read on reload (no restart needed)
```

## Rollback
Remove the two blocks + reload; then remove the `BRAIN_DB_USER=brain_app` drop-in so brain-api
reconnects as the owner (RLS inert). The GRANTs to `brain_app` are in `grants-brain_app.sql`.

See `RLS-CUTOVER.md` for the full staged cutover and `../design--rls-access-model.md` for the model.
