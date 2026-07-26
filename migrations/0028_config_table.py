"""0028 config table — runtime-tunable knobs the manager dashboard reads/writes.

Precedence in the app: config table > brain.env > code default (cfg() resolves at REQUEST time with a
~30s in-process cache, so a manager's PATCH takes effect with no service restart). The values are
non-secret behavioural knobs (recall depth, timeouts, TTLs, approval count), and the app resolves
behaviour on EVERY request as the caller's role — so SELECT is open to all app roles; only
manager/approver may write. brain_app gets table grants; the owner role bypasses RLS (NO FORCE),
so migrations/admin never lock out.

Reversible: down() drops the table (any live overrides are lost -> code falls back to env/default).
"""
VERSION = "0028"
NAME = "config_table"

_WRITE_ROLES = "current_setting('app.role', true) IN ('manager','approver')"


def up(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
          key        text PRIMARY KEY,
          value      text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          updated_by text
        )""")
    cur.execute("ALTER TABLE config ENABLE ROW LEVEL SECURITY")   # NO FORCE (owner bypasses)
    cur.execute("DROP POLICY IF EXISTS cfg_sel ON config")
    cur.execute("CREATE POLICY cfg_sel ON config FOR SELECT USING (true)")   # non-secret knobs; every role resolves behaviour
    cur.execute("DROP POLICY IF EXISTS cfg_ins ON config")
    cur.execute("CREATE POLICY cfg_ins ON config FOR INSERT WITH CHECK (%s)" % _WRITE_ROLES)
    cur.execute("DROP POLICY IF EXISTS cfg_upd ON config")
    cur.execute("CREATE POLICY cfg_upd ON config FOR UPDATE USING (%s) WITH CHECK (%s)" % (_WRITE_ROLES, _WRITE_ROLES))
    cur.execute("DROP POLICY IF EXISTS cfg_del ON config")
    cur.execute("CREATE POLICY cfg_del ON config FOR DELETE USING (%s)" % _WRITE_ROLES)
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON config TO brain_app; END IF; END $$")


def down(cur):
    cur.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brain_app') THEN "
                "REVOKE ALL ON config FROM brain_app; END IF; END $$")
    cur.execute("DROP TABLE IF EXISTS config")
