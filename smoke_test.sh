#!/usr/bin/env bash
# smoke_test.sh — fast post-install / CI sanity check for an fleetmem box. Catches the class of bugs an
# external tester hit on v0.1.0 (missing migration, malformed CA) BEFORE a user ever does. The schema
# and PKI checks are deterministic (need only Postgres + this repo); the endpoint check runs only if
# BRAIN_URL is set and the API is up.
#
# Usage:  PGDATABASE=brain PKI_DIR=/opt/brain-db/pki [BRAIN_URL=https://host:8443] ./smoke_test.sh
set -uo pipefail
DB="${PGDATABASE:-brain}"
PKI_DIR="${PKI_DIR:-/opt/brain-db/pki}"
# Client creds for the live-API steps. A server box's PKI_DIR holds only ca/server material —
# an agent's client cert lives in ~/.fleetmem/pki — so BRAIN_CERT/BRAIN_KEY/BRAIN_CA env win.
CERT="${BRAIN_CERT:-$PKI_DIR/client.crt}"; KEY="${BRAIN_KEY:-$PKI_DIR/client.key}"; CAB="${BRAIN_CA:-$PKI_DIR/ca.crt}"
# fleetmem deps live in a venv ($PREFIX/venv) — system python3 has no psycopg2. Prefer a co-located
# venv python (this script sits in the install dir); override with PYTHON=... if yours is elsewhere.
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-$([ -x "$HERE/venv/bin/python" ] && echo "$HERE/venv/bin/python" || echo python3)}"
fail=0
ok(){  echo "  ok   - $1"; }
bad(){ echo "  FAIL - $1"; fail=1; }

echo "[1/7] apply migrations (idempotent)  [python: $PYTHON]"
"$PYTHON" migrate.py up || bad "migrate up failed"

echo "[2/7] schema: agent.autoapprove_own exists (else /autolearn/extract 500s)"
if psql -d "$DB" -tAc \
  "SELECT 1 FROM information_schema.columns WHERE table_name='agent' AND column_name='autoapprove_own'" \
  2>/dev/null | grep -q 1; then
  ok "agent.autoapprove_own present"
else
  bad "agent.autoapprove_own MISSING — apply migration 0031"
fi

echo "[3/7] PKI: CA cert is a usable signing CA"
if [ -f "$PKI_DIR/ca.crt" ]; then
  ext="$(openssl x509 -in "$PKI_DIR/ca.crt" -noout -text 2>/dev/null || true)"
  echo "$ext" | grep -q "CA:TRUE"          && ok "CA basicConstraints CA:TRUE"    || bad "CA lacks basicConstraints CA:TRUE"
  echo "$ext" | grep -qi "Certificate Sign" && ok "CA keyUsage keyCertSign"        || bad "CA lacks keyUsage keyCertSign"
else
  echo "  skip - no CA at $PKI_DIR/ca.crt (run fleetmem-init-pki.sh first)"
fi

echo "[4/7] API /healthz (only if BRAIN_URL set)"
# Client creds may live outside PKI_DIR (a server box's PKI_DIR has ca/server certs only, an
# agent's live in ~/.fleetmem/pki) — honour BRAIN_CERT/BRAIN_KEY/BRAIN_CA env first. Without a
# client cert, nginx answers 403 on every proxied route (mTLS is required); a completed TLS
# handshake + HTTP 403 still proves the front door is up AND auth is enforced — count it ok.
if [ -n "${BRAIN_URL:-}" ]; then
  if [ -f "$CERT" ]; then
    curl -fsS --max-time 10 --cert "$CERT" --key "$KEY" --cacert "$CAB" \
         "$BRAIN_URL/healthz" >/dev/null 2>&1 && ok "/healthz reachable (mTLS)" || bad "/healthz unreachable with client cert"
  else
    code=$(curl -sS --max-time 10 --cacert "$CAB" -o /dev/null -w '%{http_code}' "$BRAIN_URL/healthz" 2>/dev/null || true)
    case "$code" in
      200) ok "/healthz reachable (open)";;
      403) ok "/healthz answered 403 without a client cert (front door up, mTLS enforced)";;
      *)   bad "/healthz unreachable (got '${code:-no response}')";;
    esac
  fi
else
  echo "  skip - set BRAIN_URL to also test the live API"
fi

echo "[5/7] AUTH: protected routes need cert+token; /enroll is the sole open route"
# Proves the inv-identity-pki seam at runtime. Needs BRAIN_URL + client cert (+ a real token to
# distinguish 401-no-token from 403-no-cert). code() = HTTP status only, empty on transport failure.
if [ -n "${BRAIN_URL:-}" ] && [ -f "$CERT" ]; then
  CC=(--cert "$CERT" --key "$KEY"); CA=(--cacert "$CAB")
  code(){ curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$@" 2>/dev/null; }
  # protected route, cert present but NO bearer -> 401 (authenticate: missing bearer token)
  [ "$(code "${CC[@]}" "${CA[@]}" "$BRAIN_URL/whoami")" = "401" ] \
    && ok "/whoami cert+no-token -> 401" || bad "/whoami without a token was NOT 401"
  # protected route, NO client cert -> refused (nginx/app reject: 403, or empty on TLS refusal). Never 200.
  nc="$(code "${CA[@]}" -H 'Authorization: Bearer x' "$BRAIN_URL/whoami")"
  { [ "$nc" = "403" ] || [ "$nc" = "401" ] || [ -z "$nc" ]; } \
    && ok "/whoami no-cert -> refused ($nc)" || bad "/whoami without a client cert returned $nc (expected 401/403/refused)"
  # /enroll is cert-exempt & reachable open: an app-level code (400/404/422), never a transport refusal
  ec="$(code "${CA[@]}" -X POST -H 'Content-Type: application/json' -d '{}' "$BRAIN_URL/enroll")"
  case "$ec" in 400|404|422|200) ok "/enroll no-cert reachable -> $ec (open)";; *) bad "/enroll not reachable cert-less (got '$ec')";; esac
  if [ -n "${BRAIN_TOKEN:-}" ]; then
    [ "$(code "${CC[@]}" "${CA[@]}" -H "Authorization: Bearer $BRAIN_TOKEN" "$BRAIN_URL/whoami")" = "200" ] \
      && ok "/whoami cert+token -> 200" || bad "/whoami with a valid cert+token was NOT 200"
  fi
else
  echo "  skip - set BRAIN_URL and provide a client cert (BRAIN_CERT or $PKI_DIR/client.crt) to test auth enforcement"
fi

echo "[6/7] non-ASCII round-trip: DB is UTF-8 and unicode survives a write/read (catches a SQL_ASCII DB)"
# A SQL_ASCII cluster (bare createdb on a C-locale host) 500s on the first non-ASCII write and mojibakes
# stored text. Deterministic half: server_encoding must be UTF8. Live half (needs a manager token):
# POST a unicode payload via /propose and read it back byte-identical, then clean up.
enc="$(psql -d "$DB" -tAc 'SHOW server_encoding' 2>/dev/null | tr -d '[:space:]')"
[ "$enc" = "UTF8" ] && ok "server_encoding=UTF8" || bad "server_encoding=$enc (must be UTF8 — recreate with --encoding=UTF8)"
if [ -n "${BRAIN_URL:-}" ] && [ -f "$CERT" ] && [ -n "${BRAIN_TOKEN:-}" ]; then
  probe='fleetmem-utf8-probe — «•» café → ✓ ±°'
  hc="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
        --cert "$CERT" --key "$KEY" --cacert "$CAB" \
        -H "Authorization: Bearer $BRAIN_TOKEN" -H 'Content-Type: application/json' \
        -X POST "$BRAIN_URL/propose" \
        --data "$(printf '{"name":"fleetmem_utf8_probe","body":%s}' "$(printf '%s' "$probe" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")")"
  if [ "$hc" = "200" ]; then
    got="$(psql -d "$DB" -tAc "SELECT proposed_body FROM proposal WHERE name='fleetmem_utf8_probe' ORDER BY created_at DESC LIMIT 1")"
    [ "$got" = "$probe" ] && ok "unicode round-trip byte-identical" || bad "unicode mangled on round-trip: got '$got'"
    psql -d "$DB" -c "DELETE FROM proposal WHERE name='fleetmem_utf8_probe'" >/dev/null 2>&1
  else
    bad "unicode POST /propose returned $hc (a SQL_ASCII DB 500s here)"
  fi
else
  echo "  skip - set BRAIN_URL + client cert + BRAIN_TOKEN to also test the live unicode round-trip"
fi

echo "[7/7] functional round-trip: propose -> approve -> recall (needs a manager cert+token)"
# The deepest live check: exercise the governance path end-to-end through the API only (no direct SQL
# writes) — a proposal is created, a manager Keep materializes it into a real memory, and the memory is
# read back by name. Proves apply_proposal + signing + recall-read all wire up on THIS install, not just
# that the schema exists. Runs only with a manager/approver token (a Keep is role-gated); a non-manager
# token SKIPs (not FAILs) so the test stays green for reader boxes. Artifacts are cleaned up either way.
if [ -n "${BRAIN_URL:-}" ] && [ -f "$CERT" ] && [ -n "${BRAIN_TOKEN:-}" ]; then
  RTN="fleetmem_roundtrip_probe"; RTB='fleetmem round-trip probe — do not keep'
  RTC=(--cert "$CERT" --key "$KEY" --cacert "$CAB" -H "Authorization: Bearer $BRAIN_TOKEN" \
       -H 'Content-Type: application/json' --max-time 15 -sS)
  jget(){ python3 -c 'import json,sys
try: print(json.load(sys.stdin).get(sys.argv[1],""))
except Exception: print("")' "$1"; }
  pj="$(curl "${RTC[@]}" -X POST "$BRAIN_URL/propose" \
        --data "$(printf '{"name":"%s","description":"smoke round-trip","body":%s}' "$RTN" \
                 "$(printf '%s' "$RTB" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")")"
  pid="$(printf '%s' "$pj" | jget proposal_id)"
  if [ -z "$pid" ]; then
    bad "round-trip: /propose returned no proposal_id ($pj)"
  else
    aj="$(curl "${RTC[@]}" -X POST "$BRAIN_URL/proposal/$pid/decide" --data '{"decision":"approved"}')"
    case "$aj" in
      *"role required"*) echo "  skip - token is not a manager/approver; can't test the Keep+recall step";;
      *)
        mid="$(printf '%s' "$aj" | jget memory_id)"
        if [ -z "$mid" ]; then
          bad "round-trip: approve did not materialize a memory ($aj)"
        else
          got="$(curl "${RTC[@]}" "$BRAIN_URL/memory/$RTN" | jget body)"
          [ "$got" = "$RTB" ] && ok "propose->approve->recall round-trip (memory $mid)" \
                              || bad "round-trip: recalled body mismatch (got '$got')"
        fi;;
    esac
  fi
  # cleanup — remove the probe memory + proposal rows regardless of outcome (reversible test artifact)
  psql -d "$DB" -c "DELETE FROM memory WHERE name='$RTN'; DELETE FROM proposal WHERE name='$RTN';" >/dev/null 2>&1
else
  echo "  skip - set BRAIN_URL + a manager client cert + BRAIN_TOKEN to test the propose->approve->recall round-trip"
fi

echo
if [ "$fail" -eq 0 ]; then echo "SMOKE TEST PASSED"; exit 0; else echo "SMOKE TEST FAILED"; exit 1; fi
