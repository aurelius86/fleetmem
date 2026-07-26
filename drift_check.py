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
    python3 drift_check.py <project-slug> [--verbose]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--verbose", action="store_true", help="show detail for passing checks too")
    args = ap.parse_args()

    conn = psycopg2.connect(dbname="brain", client_encoding="UTF8")
    conn.set_session(readonly=True)
    cur = conn.cursor()
    checks = load_checks(cur, args.slug)
    if not checks:
        print("no machine-checkable invariants found for '%s'" % args.slug)
        sys.exit(0)

    n_fail = 0
    print("drift-check: %s\n" % args.slug)
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
        mark = {"PASS": "[ ok ]", "FAIL": "[FAIL]", "ERROR": "[ERR ]"}.get(status, "[ ?  ]")
        show = detail if (args.verbose or status != "PASS") else ""
        print("%s %-20s %-22s %s" % (mark, section_key, chk.get("id", "?"), show))

    cur.close()
    conn.close()
    print("\n%d check(s), %d failing" % (len(checks), n_fail))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
