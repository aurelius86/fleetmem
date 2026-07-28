#!/usr/bin/env python3
"""staleness_check.py — flag active projects whose living plan has fallen behind task activity.

Sibling to drift_check.py. Where drift_check verifies a plan's `invariant` sections against the live
CODE, this checks the plan's freshness against the plan's own WORK: a project that is actively moving
tasks but whose `resume-here` section hasn't been touched is a plan going stale — the exact gap that
breaks a cold cross-agent handoff.

Signal (per active project that has >=1 in-progress task):
    STALE if the `resume-here` section is MISSING, or the newest task activity in the project is more
    than STALENESS_DAYS newer than when `resume-here` was last updated (the plan is behind the work).
    A quiet project (no recent task movement) is NOT flagged — its plan matches its (lack of) activity.
    Paused/done/archived projects are excluded (their plan is intentionally frozen).

Safe by construction: the check runs on a READ ONLY DB session. Reads project_doc/task straight from
the local DB (peer auth), so it must run ON the brain host (the brain host). The nudge is INFORMATIONAL — the
scheduled path always exits 0 (a stale plan is a reminder, not a unit failure); the signal is an
idempotent manager inbox alert (like drift_check / golden_regression). Use --strict to exit 1 on any
stale project (for manual/CI use).

Usage (as the brain service user, from the db dir):
    python3 staleness_check.py                 # roll-up + idempotent alert on any stale project
    python3 staleness_check.py --verbose       # also list projects that are fresh
    python3 staleness_check.py --days 14       # override the threshold (default env STALENESS_DAYS or 7)
    python3 staleness_check.py --strict         # exit 1 when any project is stale
"""
import argparse
import os
import sys

import psycopg2

# Threshold: how many days the newest task activity may lead `resume-here` before the plan is "stale".
DEFAULT_DAYS = int(os.environ.get("STALENESS_DAYS", "7"))

# One read-only query: every active project with an in-progress task whose resume-here is missing or
# lags the newest task activity by more than :days. `days_behind` is how far the plan trails the work.
STALE_SQL = """
WITH inprog AS (
    SELECT project_id, MAX(updated_at) AS last_task
    FROM task
    GROUP BY project_id
    HAVING count(*) FILTER (WHERE status = 'in-progress') > 0
),
rh AS (
    SELECT project_id, updated_at AS resume_at
    FROM project_doc
    WHERE section_key = 'resume-here'
)
SELECT p.slug,
       ip.last_task,
       rh.resume_at,
       round(EXTRACT(EPOCH FROM (ip.last_task - rh.resume_at)) / 86400.0, 1) AS days_behind
FROM inprog ip
JOIN project p ON p.id = ip.project_id
LEFT JOIN rh ON rh.project_id = ip.project_id
WHERE p.status IN ('active', 'ongoing')
  AND ( rh.resume_at IS NULL
        OR ip.last_task - rh.resume_at > make_interval(days => %(days)s) )
ORDER BY (rh.resume_at IS NULL) DESC, days_behind DESC NULLS FIRST
"""


def find_stale(cur, days):
    """Return [(slug, last_task, resume_at, days_behind)] for every stale active project."""
    cur.execute(STALE_SQL, {"days": days})
    return cur.fetchall()


def post_alert(stale_rows, days):
    """Idempotent inbox alert to every active manager — mirrors drift_check.py / golden_regression.py:
    mark any prior unread brain-staleness alert read first, so each inbox holds exactly ONE live
    staleness alert (the latest), never a daily stack. Uses a SEPARATE writable connection so the
    check pass itself stays strictly read-only. Returns the manager list."""
    lines = []
    for slug, last_task, resume_at, days_behind in stale_rows:
        if resume_at is None:
            lines.append("%s: no resume-here section (plan never seeded/written)" % slug)
        else:
            lines.append("%s: plan %.1f days behind task activity "
                         "(resume-here %s, newest task %s)"
                         % (slug, days_behind, resume_at.date(), last_task.date()))
    w = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    wc = w.cursor()
    wc.execute("UPDATE message SET read_at=now(), updated_at=now() "
               "WHERE from_agent='brain-staleness' AND kind='alert' AND read_at IS NULL")
    body = ("Project-plan staleness: %d active project(s) have a resume-here that trails their task "
            "activity by more than %d day(s).\n\n%s\n\n"
            "Refresh the plan: brain_project_doc_set(<slug>, 'resume-here', ...) with the current "
            "state + exact next step. Inspect: `python3 staleness_check.py --verbose` on the brain host."
            % (len(stale_rows), days, "\n".join(lines[:40])))
    wc.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
    managers = [m for (m,) in wc.fetchall()]
    for m in managers:
        wc.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) "
                   "VALUES (%s,%s,%s,%s,'alert')",
                   ("brain-staleness", m, "ALERT: project plans stale vs task activity", body))
    w.commit()
    wc.close()
    w.close()
    return managers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="staleness threshold in days (default env STALENESS_DAYS or 7)")
    ap.add_argument("--verbose", action="store_true", help="also list projects that are fresh")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any project is stale")
    args = ap.parse_args()

    # The check pass is ALWAYS read-only. Alerts go out on a separate writable connection in post_alert().
    conn = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    conn.set_session(readonly=True)
    cur = conn.cursor()

    stale = find_stale(cur, args.days)

    # Context: how many active projects were even in scope (have an in-progress task)?
    cur.execute(
        "SELECT count(*) FROM ("
        "  SELECT t.project_id FROM task t JOIN project p ON p.id = t.project_id "
        "  WHERE p.status IN ('active','ongoing') GROUP BY t.project_id "
        "  HAVING count(*) FILTER (WHERE t.status='in-progress') > 0) s")
    in_scope = cur.fetchone()[0]
    cur.close()
    conn.close()

    print("staleness-check: %d active project(s) with in-progress work; threshold %d day(s)\n"
          % (in_scope, args.days))
    for slug, last_task, resume_at, days_behind in stale:
        if resume_at is None:
            print("[STALE] %-34s no resume-here section" % slug)
        else:
            print("[STALE] %-34s %.1f days behind (plan %s, task %s)"
                  % (slug, days_behind, resume_at.date(), last_task.date()))
    if args.verbose and not stale:
        print("(all %d in-scope project(s) have a current plan)" % in_scope)

    print("\nTOTAL: %d stale / %d in-scope project(s)" % (len(stale), in_scope))
    if stale:
        managers = post_alert(stale, args.days)
        print("alerted managers: %s" % (", ".join(managers) if managers else "(no active managers)"))

    # Scheduled path is a NUDGE, not a failure: exit 0 unless --strict was asked.
    sys.exit(1 if (stale and args.strict) else 0)


if __name__ == "__main__":
    main()
