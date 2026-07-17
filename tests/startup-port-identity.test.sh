#!/bin/bash
# tests/startup-port-identity.test.sh — locks in the port-identity guard logic
# from src/startup.sh (PR: fix/startup-port-identity-check).
#
# The correctness-critical part of that fix is telling "our own service holds
# this port" from "a foreign process squats it": a false negative (our service
# not matching its own pattern) re-breaks startup with the silent-misboot mode
# the PR fixes, and a false positive lets a squatter be reported as ✓.
#
# Rather than mirror the functions (which would not catch drift), this test
# EXTRACTS the real `verify_pattern_for` and `port_held_by` definitions from
# src/startup.sh and evals them, then exercises them against real listeners.
#
# Run: bash tests/startup-port-identity.test.sh
# Exit: 0 = all pass, 1 = failure

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STARTUP="$REPO_DIR/src/startup.sh"

pass=0; fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"; pass=$((pass+1))
  else
    echo "  FAIL: $2"; fail=$((fail+1))
  fi
}

# Extract a single shell function definition (`name() { ... }`) from a file by
# capturing from the `name() {` line to the first line that is a bare `}` at
# column 0. Both target functions are top-level and closed by a column-0 brace.
extract_fn() {
  local fn="$1" file="$2"
  awk -v fn="$fn" '
    $0 ~ "^"fn"\\(\\) \\{" { grabbing=1 }
    grabbing { print }
    grabbing && /^\}/ { exit }
  ' "$file"
}

VPF_SRC="$(extract_fn verify_pattern_for "$STARTUP")"
PHB_SRC="$(extract_fn port_held_by "$STARTUP")"

if [ -z "$VPF_SRC" ] || [ -z "$PHB_SRC" ]; then
  echo "  FAIL: could not extract verify_pattern_for / port_held_by from $STARTUP"
  exit 1
fi
eval "$VPF_SRC"
eval "$PHB_SRC"

# ---------------------------------------------------------------------------
# 1. verify_pattern_for is the single source of truth: every managed service
#    resolves to a non-empty pattern, and an unknown name resolves to "" (which
#    the reaper / final-verify loop treat as "skip identity check").
# ---------------------------------------------------------------------------
for svc in credential-proxy voice-agent web-client dashboard agent-api \
           screen-capture conversation-server collector; do
  p="$(verify_pattern_for "$svc")"
  [ -n "$p" ]; report $? "verify_pattern_for $svc → non-empty ('$p')"
done

# Regression for the specific change-request: credential-proxy was omitted, so
# the most security-sensitive port (:7846 → ANTHROPIC_BASE_URL) went
# identity-unchecked everywhere but the start guard.
[ -n "$(verify_pattern_for credential-proxy)" ]
report $? "credential-proxy has a pattern (was omitted before)"

# Unknown service → empty (skip semantics).
[ -z "$(verify_pattern_for definitely-not-a-service)" ]
report $? "unknown service → empty pattern (skip)"

# Patterns match the extension-less basename, so a .ts→.js bundle switch (#2128)
# still matches our own process.
case "$(verify_pattern_for voice-agent)" in
  voice-agent) report 0 "voice-agent pattern is extension-agnostic basename" ;;
  *)           report 1 "voice-agent pattern is extension-agnostic basename" ;;
esac

# ---------------------------------------------------------------------------
# 2. port_held_by against a real listener: our-holder matches its pattern,
#    a foreign holder does not, and an unheld port is not held.
# ---------------------------------------------------------------------------
TMP="$(mktemp -d)"
LISTENERS=()
cleanup() {
  for pid in "${LISTENERS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  rm -rf "$TMP"
}
trap cleanup EXIT

# Start a python listener whose argv carries $tag; it binds an OS-assigned port,
# writes the real port to $portfile, then sleeps. Returns once the port is known.
start_listener() {
  local tag="$1" portfile="$2"
  python3 -c "
import socket, time, sys
_service_tag = sys.argv[1]          # placed in argv so ps -o command= shows it
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('127.0.0.1', 0))
s.listen(1)
open(sys.argv[2], 'w').write(str(s.getsockname()[1]))
time.sleep(30)
" "$tag" "$portfile" &
  local pid=$!
  LISTENERS+=("$pid")
  disown "$pid" 2>/dev/null || true   # suppress the shell's async "Terminated" notice on cleanup
  # wait (≤3s) for the port to be reported
  for _ in $(seq 1 30); do [ -s "$portfile" ] && break; sleep 0.1; done
}

# our-holder: argv contains "voice-agent" → matches verify_pattern_for voice-agent
start_listener "voice-agent-marker" "$TMP/our.port"
OUR_PORT="$(cat "$TMP/our.port" 2>/dev/null)"
if [ -n "$OUR_PORT" ]; then
  port_held_by "$OUR_PORT" "$(verify_pattern_for voice-agent)"
  report $? "port_held_by matches our own listener (argv carries the pattern)"
else
  report 1 "port_held_by matches our own listener (listener failed to start)"
fi

# foreign-holder: argv has no service pattern → must NOT match.
start_listener "some-unrelated-squatter" "$TMP/foreign.port"
FOREIGN_PORT="$(cat "$TMP/foreign.port" 2>/dev/null)"
if [ -n "$FOREIGN_PORT" ]; then
  if port_held_by "$FOREIGN_PORT" "$(verify_pattern_for voice-agent)"; then
    report 1 "port_held_by rejects a foreign holder (matched — false positive)"
  else
    report 0 "port_held_by rejects a foreign holder"
  fi
else
  report 1 "port_held_by rejects a foreign holder (listener failed to start)"
fi

# unheld port: pick a very likely-free high port; nothing listening → not held.
if port_held_by 65432 "$(verify_pattern_for voice-agent)"; then
  report 1 "port_held_by returns false when nothing is listening (something on :65432?)"
else
  report 0 "port_held_by returns false when nothing is listening"
fi

# ---------------------------------------------------------------------------
echo ""
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
