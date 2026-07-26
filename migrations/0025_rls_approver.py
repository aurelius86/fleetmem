"""0025 RLS approver arm — let the `approver` role (the mTLS-gated brain-dashboard, the operator's
review console) do what the app already routes it to do: SELECT the full review surface, INSERT the
materialized trusted memory on Keep (/proposal/<pid>/decide -> apply_proposal), and UPDATE (re-sign
after apply, soft-delete on provisional 'delete', graduate-amend).

Root cause (audit B2-3 / A2-1): migration 0020's memory policies name only `manager`; approver had no
arm, so under live RLS (BRAIN_DB_USER=brain_app) an approver Keep failed the mem_ins WITH CHECK ->
apply_proposal INSERT 500'd -> rolled back to pending. Rejects worked (proposal table has no RLS),
which masked the bug during the queue drain.

This is additive: it GRANTS the approver role and denies nothing to any other role. approver's
access_config ceiling is already 'secret' (= manager), and the app layer already treats approver as
see-all (api.py caller-visibility), so manager-equivalence at the DB layer matches the intended model.
mem_del (hard delete) stays manager-only — the app has no DELETE grant and soft-delete goes via UPDATE.

Reversible: down() restores the exact 0020 manager-only policy bodies.
"""
VERSION = "0025"
NAME = "rls_approver"

_SELECT = "mem_sel"; _INSERT = "mem_ins"; _UPDATE = "mem_upd"


def _create_sel(cur, roles):
    cur.execute("DROP POLICY IF EXISTS %s ON memory" % _SELECT)
    cur.execute("""
        CREATE POLICY %s ON memory FOR SELECT USING (
          current_setting('app.role', true) %s
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
        )""" % (_SELECT, roles))


def _create_ins(cur, roles):
    cur.execute("DROP POLICY IF EXISTS %s ON memory" % _INSERT)
    cur.execute("""
        CREATE POLICY %s ON memory FOR INSERT WITH CHECK (
          current_setting('app.role', true) %s
          OR (author_body = current_setting('app.agent', true) AND share_status = 'personal')
          OR (current_setting('app.role', true) = 'organizer'
              AND author_body = current_setting('app.agent', true) AND share_status = 'ready_to_share')
        )""" % (_INSERT, roles))


def _create_upd(cur, roles):
    cur.execute("DROP POLICY IF EXISTS %s ON memory" % _UPDATE)
    cur.execute("""
        CREATE POLICY %s ON memory FOR UPDATE
        USING (
          current_setting('app.role', true) %s
          OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
        )
        WITH CHECK (
          current_setting('app.role', true) %s
          OR (author_body = current_setting('app.agent', true) AND share_status IN ('personal','ready_to_share'))
        )""" % (_UPDATE, roles, roles))


def up(cur):
    _create_sel(cur, "IN ('manager','approver')")
    _create_ins(cur, "IN ('manager','approver')")
    _create_upd(cur, "IN ('manager','approver')")


def down(cur):
    _create_sel(cur, "= 'manager'")
    _create_ins(cur, "= 'manager'")
    _create_upd(cur, "= 'manager'")
