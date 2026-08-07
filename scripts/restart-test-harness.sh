#!/usr/bin/env bash
# restart-test-harness — produce the "real post-restart round trip" evidence that
# reviewers require for live-path PRs (bridge / network / delivery loop / startup;
# e.g. #2406). Runs a behavior PROBE before and after a controlled RESTART and
# emits a before/after evidence block. The probe MUST pass after restart.
#
# Runs the service commands against a configurable TARGET so the live core's own
# production services are never touched — use a disposable service, or the test VM.
#
# Usage:
#   scripts/restart-test-harness.sh \
#     --label "<pr# or description>" \
#     --start   "<cmd to start the service>" \
#     --restart "<cmd to restart it>" \
#     --probe   "<behavior check; exit 0 = healthy>" \
#     [--target local|ssh:user@host]  (default: local) \
#     [--ready  "<cmd polled until exit 0 before probing>"] \
#     [--settle <seconds>]            (fixed wait after start/restart; default 3) \
#     [--ready-timeout <seconds>]     (default 60)
#
# Exit: 0 = round trip verified (probe passed after restart); non-zero otherwise.
set -uo pipefail

LABEL="" START="" RESTART="" PROBE="" READY="" TARGET="local"
SETTLE=3 READY_TIMEOUT=60

while [ $# -gt 0 ]; do
  case "$1" in
    --label)         LABEL="$2"; shift 2 ;;
    --start)         START="$2"; shift 2 ;;
    --restart)       RESTART="$2"; shift 2 ;;
    --probe)         PROBE="$2"; shift 2 ;;
    --ready)         READY="$2"; shift 2 ;;
    --target)        TARGET="$2"; shift 2 ;;
    --settle)        SETTLE="$2"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

for req in START RESTART PROBE; do
  if [ -z "${!req}" ]; then echo "missing required --$(echo "$req" | tr '[:upper:]' '[:lower:]')" >&2; exit 2; fi
done

# run <cmd> on the configured target; forwards exit code + output
run() {
  case "$TARGET" in
    local)     bash -c "$1" ;;
    ssh:*)     ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                   -o LogLevel=ERROR -o ConnectTimeout=15 "${TARGET#ssh:}" "$1" ;;
    *) echo "bad --target: $TARGET (want local | ssh:user@host)" >&2; return 2 ;;
  esac
}

# poll --ready (if given) until exit 0 or timeout; else just sleep --settle
wait_ready() {
  if [ -n "$READY" ]; then
    local waited=0
    until run "$READY" >/dev/null 2>&1; do
      waited=$((waited + 2)); [ "$waited" -ge "$READY_TIMEOUT" ] && return 1
      sleep 2
    done
  fi
  sleep "$SETTLE"; return 0
}

utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
line() { printf '%.0s─' $(seq 1 60); echo; }

echo "# restart-test-harness — ${LABEL:-<unlabeled>}"
echo "target: $TARGET   started: $(utc)"
line

# ── 1. start + baseline probe ────────────────────────────────────────────────
echo "## 1. start service"
run "$START"; echo "(start rc=$?)"
if ! wait_ready; then echo "FAIL: service not ready within ${READY_TIMEOUT}s after start" >&2; exit 1; fi

echo "## 2. BEFORE — probe pre-restart"
BEFORE="$(run "$PROBE" 2>&1)"; BRC=$?
echo "$BEFORE"; echo "(probe rc=$BRC)"
line

# ── 2. restart ───────────────────────────────────────────────────────────────
echo "## 3. restart service"
run "$RESTART"; echo "(restart rc=$?)"
if ! wait_ready; then echo "FAIL: service not ready within ${READY_TIMEOUT}s after restart" >&2; exit 1; fi

# ── 3. post-restart probe (the load-bearing check) ───────────────────────────
echo "## 4. AFTER — probe post-restart"
AFTER="$(run "$PROBE" 2>&1)"; ARC=$?
echo "$AFTER"; echo "(probe rc=$ARC)"
line

# ── verdict ──────────────────────────────────────────────────────────────────
echo "## verdict"
echo "before-probe rc=$BRC   after-probe rc=$ARC   finished: $(utc)"
if [ "$ARC" -eq 0 ]; then
  echo "PASS ✅ — service recovered and behavior probe passed after restart (round trip verified)"
  exit 0
else
  echo "FAIL ❌ — behavior probe did NOT pass after restart"
  exit 1
fi
