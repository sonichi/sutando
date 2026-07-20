#!/usr/bin/env bash
# smoke-startup-gate.sh — CI guard for startup.sh's G1.5 bundled-mode gate.
#
# smoke-bundle.sh proves each dist artifact builds and loads under plain node.
# This script proves the OTHER half of the bundled contract: that startup.sh
# itself takes the bundled path and fails closed — the part that has only ever
# been exercised by hand (#2205). All three cases hit the gate at the top of
# startup.sh and resolve in seconds, before any service/tmux/CLI work:
#
#   1. SUTANDO_NODE set but invalid        → exit non-zero, "set but invalid"
#   2. valid SUTANDO_NODE, missing artifact → exit non-zero, "dist artifacts missing"
#   3. valid SUTANDO_NODE, full dist        → proceeds PAST the gate (neither
#      fail-closed message appears; startup output continues after the gate)
#
# Expects `npm run build:bundle` to have populated dist/ (the workflow runs
# smoke-bundle.sh first, which does that).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0

# A real node binary staged OUTSIDE PATH — what the desktop supervisor exports.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$(command -v node)" "$STAGE/node"

# The gate sits at the top of startup.sh; every case is capped anyway so a
# regression that pushes past the gate can't hang CI. Portable cap: `timeout`
# on CI/Linux, background+kill fallback on macOS (same shape as
# smoke-bundle.sh's run_capped — macOS ships no `timeout`).
CAP=30
run_startup() { # $1 = SUTANDO_NODE value; prints output, returns exit code
  local rc=0
  if command -v timeout >/dev/null 2>&1; then
    SUTANDO_NODE="$1" timeout "$CAP" bash src/startup.sh 2>&1 || rc=$?
    return $rc
  fi
  local out
  out="$(SUTANDO_NODE="$1" bash src/startup.sh 2>&1 & _p=$!; ( sleep "$CAP"; kill "$_p" 2>/dev/null ) & _w=$!; wait "$_p" 2>/dev/null; rc=$?; kill "$_w" 2>/dev/null; exit $rc)" || rc=$?
  printf '%s' "$out"
  [ "$rc" = 143 ] && return 124
  return $rc
}

echo "── case 1: SUTANDO_NODE invalid → fail-closed ──"
set +e
out="$(run_startup /nonexistent/node)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "SUTANDO_NODE is set but invalid"; then
  echo "  ✓ refused invalid SUTANDO_NODE (rc=$rc)"
else
  echo "  ✗ SMOKE FAIL: expected fail-closed on invalid SUTANDO_NODE (rc=$rc)"
  printf '%s\n' "$out" | tail -5 | sed 's/^/    /'
  fail=1
fi

echo "── case 2: valid SUTANDO_NODE, missing dist artifact → fail-closed ──"
if [ ! -f dist/voice-agent.js ]; then
  echo "  ✗ SMOKE FAIL: dist/voice-agent.js absent — run build:bundle before this script"
  exit 1
fi
mv dist/voice-agent.js "$STAGE/voice-agent.js.held"
set +e
out="$(run_startup "$STAGE/node")"; rc=$?
set -e
mv "$STAGE/voice-agent.js.held" dist/voice-agent.js
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "required dist artifacts missing"; then
  echo "  ✓ refused missing artifact (rc=$rc, named voice-agent.js: $(printf '%s' "$out" | grep -o 'voice-agent.js' | head -1))"
else
  echo "  ✗ SMOKE FAIL: expected fail-closed on missing dist artifact (rc=$rc)"
  printf '%s\n' "$out" | tail -5 | sed 's/^/    /'
  fail=1
fi

echo "── case 3: valid SUTANDO_NODE, full dist → gate passes ──"
# startup.sh will die later on CI (no claude CLI / tmux session), or hit the
# CAP — both fine. The assertion is only about the GATE: neither fail-closed
# message may appear, and output must continue past the gate region.
# CI-ONLY case: a passing gate means startup.sh goes on to launch real
# services from dist/. Never run this against a live install — it would fight
# the resident core over ports and state. The workflow sets SMOKE_GATE_CASE3=1.
if [ "${SMOKE_GATE_CASE3:-0}" != "1" ]; then
  echo "  ~ skipped (set SMOKE_GATE_CASE3=1 on a disposable runner only)"
  [ "$fail" -ne 0 ] && { echo "startup-gate smoke FAILED"; exit 1; }
  echo "startup-gate smoke OK (cases 1-2; case 3 is CI-only)"
  exit 0
fi
set +e
out="$(run_startup "$STAGE/node")"; rc=$?
set -e
# Reap any service the capped run left behind (timeout kills startup.sh, not
# its backgrounded children). Disposable-runner hygiene, keyed to THIS dist.
pkill -f "$PWD/dist/" 2>/dev/null || true
if printf '%s' "$out" | grep -qE "SUTANDO_NODE is set but invalid|required dist artifacts missing"; then
  echo "  ✗ SMOKE FAIL: gate fail-closed fired despite valid node + full dist (rc=$rc)"
  printf '%s\n' "$out" | grep -E "set but invalid|artifacts missing" | sed 's/^/    /'
  fail=1
elif [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
  echo "  ✗ SMOKE FAIL: no output at all — startup.sh did not run (rc=$rc)"
  fail=1
else
  echo "  ✓ gate passed with valid node + full dist (rc=$rc; later CI-environment failures are out of scope)"
fi

if [ "$fail" -ne 0 ]; then
  echo "startup-gate smoke FAILED — the G1.5 bundled gate does not behave as pinned."
  exit 1
fi
echo "startup-gate smoke OK — fail-closed both ways, gate passes when the contract is met."
