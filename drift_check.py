#!/usr/bin/env python3
"""drift_check.py — verify a project's project_doc `invariant` sections against the LIVE system.

The project plan (project_doc) records, per project, the invariants each flow must uphold. This tool
turns the checkable ones into a pass/fail report so the plan can't silently go stale vs the code.

Each `invariant` section MAY embed a fenced ```check block (a YAML list). Each check:
    - id:     short name (shown in the report)
      kind:   sql | grep_present | grep_absent
      # sql:        query: <SELECT ...>   expect: <scalar>   (compared to the 1st column of the 1st row)
      # grep_*:     path: <file|dir>      pattern: <ERE>     [include: <glob, e.g. *.py>]
      #             grep_present => at least one match; grep_absent => zero matches

Safe by construction: the DB session is READ ONLY (a stored `sql` check can only read); grep runs via
argv (no shell), binary files ignored (-I). Reads project_doc straight from the local DB (peer auth),
so it must run ON the brain host. Exit 0 = all pass; 1 = any fail or errored check.

Usage (as the brain service user, from the db dir):
    python3 drift_check.py <project-slug> [--verbose]   # one project
    python3 drift_check.py --all [--verbose]            # every project with checks; on failure,
                                                        # posts an idempotent inbox alert to managers
"""
import argparse
import re
import subprocess
import sys

import psycopg2
import yaml

CHECK_RE = re.compile(r"```check\s*\n(.*?)```", re.S)


def load_checks(cur, slug):
    """Return [(section_key, check_dict, preverdict_or_None)] for every check in the plan's
    invariant sections. preverdict is set only when the ```check block itself failed to parse."""
    cur.execute(
        "SELECT section_key, body FROM project_doc "
        "WHERE project_id = (SELECT id FROM project WHERE slug = %s) AND kind = 'invariant' "
        "ORDER BY position", (slug,))
    out = []
    for section_key, body in cur.fetchall():
        m = CHECK_RE.search(body or "")
        if not m:
            continue
        try:
            checks = yaml.safe_load(m.group(1)) or []
        except yaml.YAMLError as e:
            out.append((section_key, {"id": "<parse>"}, ("ERROR", "bad check YAML: %s" % e)))
            continue
        for chk in checks:
            out.append((section_key, chk, None))
    return out


def run_sql(cur, chk):
    cur.execute("SAVEPOINT _c")
    try:
        cur.execute(chk["query"])
        row = cur.fetchone()
        got = row[0] if row else None
        cur.execute("RELEASE SAVEPOINT _c")
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT _c")
        return ("ERROR", str(e).strip().splitlines()[0])
    ok = str(got) == str(chk.get("expect"))
    return ("PASS" if ok else "FAIL", "got=%r expect=%r" % (got, chk.get("expect")))


def run_grep(chk, want_present):
    argv = ["grep", "-rEcI"]
    if chk.get("include"):
        argv += ["--include", chk["include"]]
    argv += [chk["pattern"], chk["path"]]
    p = subprocess.run(argv, capture_output=True, text=True)
    total = 0
    for line in (p.stdout or "").splitlines():
        n = line.rsplit(":", 1)[-1] if ":" in line else line   # "path:count" (dir) or "count" (file)
        try:
            total += int(n)
        except ValueError:
            pass
    ok = (total > 0) == want_present
    return ("PASS" if ok else "FAIL",
            "matches=%d want=%s" % (total, "present" if want_present else "absent"))


def run_slug(cur, slug, verbose=False, emit=True):
    """Run every machine-checkable invariant for `slug` on the (read-only) cursor `cur`.
    Returns (n_checks, n_fail, fail_lines). `emit` prints the per-check report; --all silences it
    and prints its own one-line-per-project roll-up instead."""
    checks = load_checks(cur, slug)
    if not checks:
        if emit:
            print("no machine-checkable invariants found for '%s'" % slug)
        return (0, 0, [])

    n_fail = 0
    fails = []
    if emit:
        print("drift-check: %s\n" % slug)
    for section_key, chk, pre in checks:
        if pre:
            status, detail = pre
        else:
            kind = chk.get("kind")
            if kind == "sql":
                status, detail = run_sql(cur, chk)
            elif kind == "grep_present":
                status, detail = run_grep(chk, True)
            elif kind == "grep_absent":
                status, detail = run_grep(chk, False)
            else:
                status, detail = ("ERROR", "unknown kind %r" % kind)
        if status != "PASS":
            n_fail += 1
            fails.append("%s / %s [%s]: %s" % (slug, chk.get("id", "?"), status, detail))
        if emit:
            mark = {"PASS": "[ ok ]", "FAIL": "[FAIL]", "ERROR": "[ERR ]"}.get(status, "[ ?  ]")
            show = detail if (verbose or status != "PASS") else ""
            print("%s %-20s %-22s %s" % (mark, section_key, chk.get("id", "?"), show))
    if emit:
        print("\n%d check(s), %d failing" % (len(checks), n_fail))
    return (len(checks), n_fail, fails)


def slugs_with_checks(cur):
    """Every project slug whose plan has at least one embedded ```check block."""
    cur.execute(
        "SELECT DISTINCT p.slug FROM project p JOIN project_doc d ON d.project_id = p.id "
        "WHERE d.kind = 'invariant' AND d.body LIKE '%```check%' ORDER BY p.slug")
    return [r[0] for r in cur.fetchall()]


def post_alert(fail_lines, projects_failing):
    """Idempotent inbox alert to every active manager — mirrors golden_regression.py:
    mark any prior unread brain-drift alert read first, so each inbox holds exactly ONE live drift
    alert (the latest), never a daily stack. Uses a SEPARATE writable connection so the check pass
    itself stays strictly read-only (the safety-by-construction guarantee). Returns the manager list."""
    w = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    wc = w.cursor()
    wc.execute("UPDATE message SET read_at=now(), updated_at=now() "
               "WHERE from_agent='brain-drift' AND kind='alert' AND read_at IS NULL")
    body = ("Project-plan drift-check: %d project(s) have a failing invariant.\n\n%s\n\n"
            "Inspect: `python3 drift_check.py <slug> --verbose` on the brain host."
            % (projects_failing, "\n".join(fail_lines[:40])))
    wc.execute("SELECT name FROM agent WHERE role='manager' AND revoked_at IS NULL")
    managers = [m for (m,) in wc.fetchall()]
    for m in managers:
        wc.execute("INSERT INTO message(from_agent,to_agent,subject,body,kind) "
                   "VALUES (%s,%s,%s,%s,'alert')",
                   ("brain-drift", m, "ALERT: project-plan drift-check failing", body))
    w.commit()
    wc.close()
    w.close()
    return managers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", nargs="?", help="project slug (omit when using --all)")
    ap.add_argument("--all", action="store_true", dest="run_all",
                    help="check every project that has machine-checkable invariants; "
                         "alert managers on failure")
    ap.add_argument("--verbose", action="store_true", help="show detail for passing checks too")
    args = ap.parse_args()
    if not args.run_all and not args.slug:
        ap.error("give a project slug, or use --all")

    # The check pass is ALWAYS read-only — a stored `sql` check can therefore only read (safety
    # by construction). Alerts go out on a separate writable connection inside post_alert().
    conn = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    conn.set_session(readonly=True)
    cur = conn.cursor()

    if not args.run_all:
        n_checks, n_fail, _ = run_slug(cur, args.slug, verbose=args.verbose, emit=True)
        cur.close()
        conn.close()
        sys.exit(1 if n_fail else 0)

    slugs = slugs_with_checks(cur)
    print("drift-check --all: %d project(s) with machine-checkable invariants\n" % len(slugs))
    total_checks = total_fail = projects_failing = 0
    all_fails = []
    for slug in slugs:
        n_checks, n_fail, fails = run_slug(cur, slug, verbose=args.verbose, emit=False)
        total_checks += n_checks
        total_fail += n_fail
        if n_fail:
            projects_failing += 1
            all_fails.extend(fails)
        mark = "[FAIL]" if n_fail else "[ ok ]"
        print("%s %-34s %d/%d checks passing" % (mark, slug, n_checks - n_fail, n_checks))
    cur.close()
    conn.close()

    print("\nTOTAL: %d check(s) across %d project(s), %d failing (%d project(s) affected)"
          % (total_checks, len(slugs), total_fail, projects_failing))
    if total_fail:
        managers = post_alert(all_fails, projects_failing)
        print("alerted managers: %s" % ", ".join(managers) if managers else "no active managers to alert")
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
