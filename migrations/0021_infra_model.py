"""0021 infra model — hosts / services / links as the brain's canonical infra MODEL.

Infrastructure STRUCTURE (what hosts exist, what services run on them, how they depend on each
other) is modelled in the brain as the source of truth. Live status and the physical device tree
can come from an external monitoring system that the dashboard overlays onto this model.

These are infra topology, NOT the sensitive memory tier, so they carry NO row-level security (only
`memory` has RLS). They just need grants for the non-owner app role `brain_app`; writes are
gated to managers at the app layer (api.py), reads are open to any authenticated agent. Reversible.
"""
VERSION = "0021"
NAME = "infra_model"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS infra_host (
          id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name           text UNIQUE NOT NULL,                 -- canonical key: the model host, the brain host, acme-router
          kind           text NOT NULL DEFAULT 'lxc'
                           CHECK (kind IN ('node','device','vm','lxc')),
          display        text,
          ip             text,
          mac            text,
          parent_host    text,                                 -- containment/topology: -> infra_host.name
          librenms_hostname text,                              -- match/overlay a LibreNMS device
          location       text,
          anchor_memory  text,                                 -- the reference_* note name
          notes          text,
          created_at     timestamptz NOT NULL DEFAULT now(),
          updated_at     timestamptz NOT NULL DEFAULT now()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS infra_service (
          id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name           text UNIQUE NOT NULL,                 -- e.g. service-a, service-b, api
          label          text,
          host           text NOT NULL,                        -- -> infra_host.name (where it runs)
          ip             text,
          port           integer,
          url            text,
          grp            text,                                 -- UI palette group: service/ai/security/...
          container_id   integer,                              -- Proxmox ct id (nullable)
          ha             boolean NOT NULL DEFAULT false,
          anchor_memory  text,
          description    text,
          created_at     timestamptz NOT NULL DEFAULT now(),
          updated_at     timestamptz NOT NULL DEFAULT now()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS infra_link (
          id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          src    text NOT NULL,                                -- host or service name
          dst    text NOT NULL,
          rel    text NOT NULL DEFAULT 'depends_on'
                   CHECK (rel IN ('depends_on','proxies','routes','runs_on','connects')),
          notes  text,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (src, dst, rel)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS infra_service_host_idx ON infra_service (host)")
    cur.execute("CREATE INDEX IF NOT EXISTS infra_host_parent_idx ON infra_host (parent_host)")
    # grants for the non-owner app role; guarded so a fresh clone without the role still applies
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON infra_host, infra_service, infra_link TO brain_app; "
                "END IF; END $$")


def down(cur):
    cur.execute("DROP TABLE IF EXISTS infra_link")
    cur.execute("DROP TABLE IF EXISTS infra_service")
    cur.execute("DROP TABLE IF EXISTS infra_host")
