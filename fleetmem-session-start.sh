#!/bin/bash
# fleetmem-session-start.sh — fleetmem SessionStart injection hook (ships with fleetmem).
#
# Purpose: at the start of every agent session, inject a PER-AGENT brief so the host LLM
# uses the brain automatically — its persona + always-on rules, plus its own live state
# (unread inbox, open tasks, pending reviews). The agent is identified by its mTLS cert,
# so the brief is assembled server-side for THAT agent only.
#
# Install (Claude Code): register this script's absolute path under SessionStart in the
# agent's .claude/settings.json, e.g.
#   { "hooks": { "SessionStart": [ { "hooks": [
#       { "type": "command", "command": "/absolute/path/to/fleetmem-session-start.sh" } ] } ] } }
# Other runtimes: run this at session start and prepend its stdout to the system context.
#
# Config: reads ~/.fleetmem/client.conf (the SAME one-file-per-agent config used by the MCP):
#   BRAIN_URL, BRAIN_CERT, BRAIN_KEY, BRAIN_CA, BRAIN_TOKEN_FILE
# Dependencies: curl, jq.
#
# stdout is injected as session context — keep it small. NEVER fail the session (always exit 0).
set +e

CONF="${BRAIN_CLIENT_CONF:-$HOME/.fleetmem/client.conf}"
# Static-core fallback (printed when the brain can't be reached), looked for next to this
# script first, then in ~/.fleetmem/.
_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null)"
FALLBACK=""
for _f in "$_self_dir/session-brief.fallback.md" "$HOME/.fleetmem/session-brief.fallback.md"; do
  [ -f "$_f" ] && { FALLBACK="$_f"; break; }
done

# Load client.conf (fills only unset vars; a real env var still wins). Expand a leading ~.
if [ -f "$CONF" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in ''|\#*) continue ;; esac
    _v="${_v/#\~/$HOME}"; eval ": \"\${$_k:=\$_v}\""
  done < "$CONF"
fi

echo "## session-start (auto) — fleetmem"

_print_fallback() {
  if [ -n "$FALLBACK" ]; then
    echo; cat "$FALLBACK"
  else
    echo "ℹ️ fleetmem brain unreachable and no local fallback found — starting without the injected brief."
  fi
}

# Need creds + a session id to talk to the brain; otherwise print the offline static core.
SID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "$BRAIN_URL" ] || [ ! -f "$BRAIN_CERT" ] || [ ! -f "$BRAIN_KEY" ] || [ ! -f "$BRAIN_CA" ] || [ ! -f "$BRAIN_TOKEN_FILE" ]; then
  echo "⚠️ fleetmem: missing BRAIN_URL or client creds (see ~/.fleetmem/client.conf) — injecting offline static core only."
  _print_fallback
  exit 0
fi

_curl() {
  curl -sS --max-time 8 --cert "$BRAIN_CERT" --key "$BRAIN_KEY" --cacert "$BRAIN_CA" \
    -H "Authorization: Bearer $(cat "$BRAIN_TOKEN_FILE")" "$@" 2>/dev/null
}

# --- Prefer a single server-assembled brief (/session-brief) when the brain offers it. ---
brief=$(_curl -X POST "$BRAIN_URL/session-brief" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SID\"}")
btext=$(printf '%s' "$brief" | jq -r '.brief // empty' 2>/dev/null)
if [ -n "$btext" ]; then
  echo "$btext"
  exit 0
fi

# --- Fallback path: stitch the existing per-agent endpoints (works on any fleetmem). ---
# 1. Persona + always-on rules (per-agent welcome + RLS-filtered rules), via /bootstrap.
boot=$(_curl -X POST "$BRAIN_URL/bootstrap" -H 'Content-Type: application/json' -d "{\"session_id\":\"$SID\"}")
welcome=$(printf '%s' "$boot" | jq -r '.welcome // empty' 2>/dev/null)
rules=$(printf '%s' "$boot" | jq -r '.rules // empty' 2>/dev/null)
overlay=$(printf '%s' "$boot" | jq -r '.overlay // empty' 2>/dev/null)   # global user house-rules overlay
if [ -n "$welcome" ] || [ -n "$rules" ] || [ -n "$overlay" ]; then
  echo "## persona + always-on rules (from your brain)"
  [ -n "$welcome" ] && echo "$welcome"
  [ -n "$rules" ] && { echo; echo "$rules"; }
  [ -n "$overlay" ] && { echo; echo "$overlay"; }
else
  echo "⚠️ fleetmem: /bootstrap returned no persona/rules — this session has no injected identity/rules."
  echo "   Check brain reachability and this agent's readers/groups vs the always-on rules note."
  _print_fallback
fi

# 2. This agent's UNREAD inbox.
binbox=$(_curl "$BRAIN_URL/inbox?unread=1" \
  | jq -r '.messages[]? | "  - [\(.from_agent)] \(.subject // "(no subject)"): \((.body // "") | gsub("\n";" ") | .[0:200])  (id \(.id[0:8]))"' 2>/dev/null)
[ -n "$binbox" ] && { echo; echo "🧠 Brain inbox — UNREAD (turn actionable items into tasks, then brain_mark_read):"; echo "$binbox"; }

# 3. This agent's open / in-progress tasks (compact; RLS-filtered by the brain).
btasks=$(_curl "$BRAIN_URL/tasks" \
  | jq -r '(.tasks // [])[] | select((.status // "") == "open" or (.status // "") == "in-progress")
           | "  \(if .status=="in-progress" then ">" else "." end) [\(.handle)] (\(.status)) \(.title)\(if .project then " — "+.project else "" end)"' 2>/dev/null)
[ -n "$btasks" ] && { echo; echo "🗂️ Open tasks (active first):"; echo "$btasks"; }

# 4. Pending reviews (manager/approver only; a worker simply sees nothing here).
prop=$(_curl "$BRAIN_URL/proposals" | jq -r '[(.proposals // [])[] | select((.status // "pending")=="pending")] | length' 2>/dev/null)
prov=$(_curl "$BRAIN_URL/provisional/pending" | jq -r '(.pending // []) | length' 2>/dev/null)
strc=$(_curl "$BRAIN_URL/structure/pending" | jq -r '.count // 0' 2>/dev/null)
tot=$(( ${prop:-0} + ${prov:-0} + ${strc:-0} ))
if [ "$tot" -gt 0 ] 2>/dev/null; then
  echo; echo "🧠 Awaiting your review: ${prop:-0} proposal(s) · ${prov:-0} ready-to-share · ${strc:-0} structure item(s)."
fi

exit 0
