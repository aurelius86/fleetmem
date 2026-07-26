"""0020 RLS access-control model — DB-enforced who-can-read/write on `memory`.

Design: design--rls-access-model.md. Four axes per row (author_body owner · share_status
personal|ready_to_share|trusted · readers text[] group+agent tokens · sensitivity with a per-role
ceiling in access_config). The app will connect as a NON-OWNER role `brain_app` and SET LOCAL
app.agent/app.role/app.groups per request; these policies enforce the model beneath the app. No
FORCE — the owner role bypasses so migrations/admin never lock out.

PRE-STEP (run once as a superuser, NOT in this migration because the owner role can't CREATE ROLE):
    DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app')
         THEN CREATE ROLE brain_app NOLOGIN; END IF; END $$;
The harness/live both create it as postgres first; then this migration (run by the owner) GRANTs to it.
Reversible: down() drops policies, disables RLS, revokes, drops config + function.
"""
VERSION = "0020"
NAME = "rls_access_model"

_SELECT = "mem_sel"; _INSERT = "mem_ins"; _UPDATE = "mem_upd"; _DELETE = "mem_del"


def up(cur):
    # sensitivity ordering (public<normal<sensitive<secret); unknown -> most restrictive
    cur.execute("""
        CREATE OR REPLACE FUNCTION sens_rank(s text) RETURNS int LANGUAGE sql IMMUTABLE AS $$
          SELECT CASE s WHEN 'public' THEN 0 WHEN 'normal' THEN 1
                        WHEN 'sensitive' THEN 2 WHEN 'secret' THEN 3 ELSE 3 END
        $$""")
    # per-role sensitivity ceiling (config-driven "which LLM sees what")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS access_config (
          role            text PRIMARY KEY,
          max_sensitivity text NOT NULL DEFAULT 'normal'
        )""")
    cur.execute("""INSERT INTO access_config(role,max_sensitivity) VALUES
                   ('manager','secret'),('approver','secret'),
                   ('organizer','sensitive'),
                   ('worker','normal'),('viewer','normal'),('readonly','normal')
                   ON CONFLICT (role) DO NOTHING""")

    cur.execute("ALTER TABLE memory ENABLE ROW LEVEL SECURITY")   # NO FORCE (owner bypasses)

    # READ: manager=all; own personal (any sens); trusted & shared-to-me within ceiling;
    #       organizer reads trusted+ready_to_share within ceiling (relate-wide, owner-fenced).
    cur.execute("""
        CREATE POLICY %s ON memory FOR SELECT USING (
          current_setting('app.role', true) = 'manager'
          OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
          OR (
               share_status = 'trusted'
               AND sens_rank(sensitivity) <= sens_rank(
                     (SELECT max_sensitivity FROM access_config WHERE role = current_setting('app.role', true)))
               AND ( current_setting('app.agent', true) = ANY(readers)
                     OR readers && (current_setting('app.groups', true))::text[] )
             )
          OR (
               current_setting('app.role', true) = 'organizer'
               AND share_status IN ('trusted','ready_to_share')
               AND sens_rank(sensitivity) <= sens_rank(
                     (SELECT max_sensitivity FROM access_config WHERE role = 'organizer'))
             )
        )""" % _SELECT)

    # INSERT: manager any; any agent own personal; organizer own ready_to_share.
    cur.execute("""
        CREATE POLICY %s ON memory FOR INSERT WITH CHECK (
          current_setting('app.role', true) = 'manager'
          OR (author_body = current_setting('app.agent', true) AND share_status = 'personal')
          OR (current_setting('app.role', true) = 'organizer'
              AND author_body = current_setting('app.agent', true) AND share_status = 'ready_to_share')
        )""" % _INSERT)

    # UPDATE / soft-delete: manager any; author on OWN personal|ready_to_share (can flip between them,
    # can soft-delete own; WITH CHECK forbids setting trusted or touching another author / trusted row).
    cur.execute("""
        CREATE POLICY %s ON memory FOR UPDATE
        USING (
          current_setting('app.role', true) = 'manager'
          OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
        )
        WITH CHECK (
          current_setting('app.role', true) = 'manager'
          OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
        )""" % _UPDATE)

    # hard DELETE: managers only (normal path is soft-delete via UPDATE; app isn't granted DELETE anyway)
    cur.execute("CREATE POLICY %s ON memory FOR DELETE USING (current_setting('app.role', true) = 'manager')" % _DELETE)

    # grants to the non-owner app role (RLS then narrows each). Guarded so the migration still
    # applies if the role isn't present yet (e.g. a fresh clone) — grants are re-runnable.
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "GRANT USAGE ON SCHEMA public TO brain_app; "
                "GRANT SELECT, INSERT, UPDATE ON memory TO brain_app; "
                "GRANT SELECT ON access_config TO brain_app; "
                "GRANT EXECUTE ON FUNCTION sens_rank(text) TO brain_app; "
                "END IF; END $$")


def down(cur):
    for p in (_SELECT, _INSERT, _UPDATE, _DELETE):
        cur.execute("DROP POLICY IF EXISTS %s ON memory" % p)
    cur.execute("ALTER TABLE memory DISABLE ROW LEVEL SECURITY")
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "REVOKE ALL ON memory FROM brain_app; REVOKE ALL ON access_config FROM brain_app; "
                "END IF; END $$")
    cur.execute("DROP TABLE IF EXISTS access_config")
    cur.execute("DROP FUNCTION IF EXISTS sens_rank(text)")
