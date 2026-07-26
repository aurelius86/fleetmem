"""0016 bootstrap-MOC guard — alert on ANY content change to a bootstrap-injected memory.

`always_on_rules_moc` is injected RAW as instructions into every session (api.py /bootstrap), so a
content change to it is the highest-value poisoning event in the brain. This AFTER trigger fires on
any INSERT or body-changing UPDATE to a protected-name memory — via ANY write path, including direct
SQL that bypasses the API — and (a) writes an action_log alert with old/new body md5s and (b) drops
an inbox 'alert' message to every (non-revoked) manager. DETECTIVE control: it does not block
(prevention stays at the API — trusted-name writes go through the manager-only /approve path). The
protected-name set is an array in the function; extend it there as more MOCs become bootstrap-injected.
Reversible.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VERSION = "0016"
NAME = "bootstrap_guard"

_FN = r"""
CREATE OR REPLACE FUNCTION brain_bootstrap_moc_guard() RETURNS trigger AS $BODY$
DECLARE
  protected text[] := ARRAY['always_on_rules_moc'];   -- bootstrap-injectable names; extend here
  m record;
  oldh text; newh text;
BEGIN
  IF NEW.name IS NULL OR NOT (NEW.name = ANY(protected)) THEN
    RETURN NEW;
  END IF;
  IF NEW.deleted_at IS NOT NULL THEN
    RETURN NEW;   -- soft-deleted rows are never bootstrap-injected; ignore
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.body IS NOT DISTINCT FROM OLD.body THEN
    RETURN NEW;   -- not a content change
  END IF;
  newh := substr(md5(coalesce(NEW.body, '')), 1, 12);
  oldh := CASE WHEN TG_OP = 'UPDATE' THEN substr(md5(coalesce(OLD.body, '')), 1, 12) ELSE 'none' END;
  INSERT INTO action_log(actor, action, target_kind, target_id, detail)
    VALUES ('brain-guard', 'bootstrap_moc_changed', 'memory', NEW.id::text,
            jsonb_build_object('name', NEW.name, 'op', TG_OP, 'old_hash', oldh, 'new_hash', newh));
  FOR m IN SELECT name FROM agent WHERE role = 'manager' AND revoked_at IS NULL LOOP
    INSERT INTO message(from_agent, to_agent, subject, body, kind)
      VALUES ('brain-guard', m.name, 'ALERT: bootstrap MOC changed',
              format('The bootstrap-injected memory "%s" changed (%s): old=%s new=%s. It is injected as '
                     'instructions into EVERY session — verify this change was intended.',
                     NEW.name, TG_OP, oldh, newh),
              'alert');
  END LOOP;
  RETURN NEW;
END;
$BODY$ LANGUAGE plpgsql;
"""


def up(cur):
    cur.execute(_FN)
    cur.execute("DROP TRIGGER IF EXISTS bootstrap_moc_guard ON memory")
    cur.execute("CREATE TRIGGER bootstrap_moc_guard AFTER INSERT OR UPDATE ON memory "
                "FOR EACH ROW EXECUTE FUNCTION brain_bootstrap_moc_guard()")


def down(cur):
    cur.execute("DROP TRIGGER IF EXISTS bootstrap_moc_guard ON memory")
    cur.execute("DROP FUNCTION IF EXISTS brain_bootstrap_moc_guard()")
