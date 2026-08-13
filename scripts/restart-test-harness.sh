#!/usr/bin/env bash
# Produce real post-restart round-trip evidence for live-path PRs: probe a service
# before and after a controlled restart; exit 0 only if every stage passed. -h for usage.
set -uo pipefail

usage() {
  cat >&2 <<'EOF'
restart-test-harness — real post-restart round-trip evidence for live-path PRs.

  scripts/restart-test-harness.sh \
    --label   "<pr# or description>" \
    --start   "<cmd to start the service>" \
    --restart "<cmd to restart it>" \
    --probe   "<behavior check; exit 0 = healthy>" \
    --identity "<prints a value that MUST change across a real restart,
                 e.g. the pid or process start time>" \
    [--target local|ssh:user@host]   (default: local) \
    [--known-hosts <file>]           (ssh: pin to a disposable known_hosts and
                                      accept a new key only on first connect;
                                      without it ssh uses your normal host
                                      verification — never disabled) \
    [--ready  "<cmd polled until exit 0 before probing>"] \
    [--settle <seconds>]             (fixed wait after start/restart; default 3) \
    [--ready-timeout <seconds>]      (default 60)

Run the service commands against a disposable TARGET (a test VM, not the live
core). Exit 0 only if start, restart, and BOTH probes succeeded AND the identity
value changed — a restart command can return 0 while the old process keeps
answering, and then the probes pass without a restart having occurred.
EOF
}

LABEL="" START="" RESTART="" PROBE="" IDENTITY="" READY="" TARGET="local" KNOWN_HOSTS=""
SETTLE=3 READY_TIMEOUT=60

while [ $# -gt 0 ]; do
  case "$1" in
    --label)         LABEL="$2"; shift 2 ;;
    --start)         START="$2"; shift 2 ;;
    --restart)       RESTART="$2"; shift 2 ;;
    --probe)         PROBE="$2"; shift 2 ;;
    --identity)      IDENTITY="$2"; shift 2 ;;
    --ready)         READY="$2"; shift 2 ;;
    --target)        TARGET="$2"; shift 2 ;;
    --known-hosts)   KNOWN_HOSTS="$2"; shift 2 ;;
    --settle)        SETTLE="$2"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

for req in START RESTART PROBE IDENTITY; do
  if [ -z "${!req}" ]; then echo "missing required --$(echo "$req" | tr '[:upper:]' '[:lower:]')" >&2; exit 2; fi
done

# An unvalidated bound is worse than no bound: `[ n -ge nope ]` errors every
# iteration, so the timeout branch never fires and the wait runs forever.
for num in READY_TIMEOUT:1 SETTLE:0; do
  var="${num%%:*}"; min="${num##*:}"; val="${!var}"
  if ! printf '%s' "$val" | grep -Eq '^[0-9]+$' || [ "$val" -lt "$min" ]; then
    echo "--$(echo "$var" | tr '[:upper:]_' '[:lower:]-') must be an integer >= $min (got: $val)" >&2
    usage; exit 2
  fi
done

# run <cmd> on the configured target; forwards exit code + output.
run() {
  case "$TARGET" in
    local)     bash -c "$1" ;;
    ssh:*)
      # Host verification stays ON. --known-hosts pins a disposable file and
      # accepts a new key only on first connect (still detects later changes).
      if [ -n "$KNOWN_HOSTS" ]; then
        ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS" \
            -o LogLevel=ERROR -o ConnectTimeout=15 "${TARGET#ssh:}" "$1"
      else
        ssh -o LogLevel=ERROR -o ConnectTimeout=15 "${TARGET#ssh:}" "$1"
      fi ;;
    *) echo "bad --target: $TARGET (want local | ssh:user@host)" >&2; return 2 ;;
  esac
}

# poll --ready (if given) until exit 0 or timeout; else just sleep --settle.
wait_ready() {
  if [ -n "$READY" ]; then
    local waited=0
    until run "$READY" >/dev/null 2>&1; do
      waited=$((waited + 2)); [ "$waited" -ge "$READY_TIMEOUT" ] && return 1
      sleep 2
    done
  fi
  # `sleep` failing must not read as ready — the settle is part of the contract.
  sleep "$SETTLE" || return 1
  return 0
}

utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
line() { printf '%.0s─' $(seq 1 60); echo; }

echo "# restart-test-harness — ${LABEL:-<unlabeled>}"
echo "target: $TARGET   started: $(utc)"
line

echo "## 1. start service"
run "$START"; SRC=$?; echo "(start rc=$SRC)"
if [ "$SRC" -ne 0 ]; then echo "FAIL ❌ — start command failed (rc=$SRC); no round trip attempted" >&2; exit 1; fi
if ! wait_ready; then echo "FAIL ❌ — service not ready within ${READY_TIMEOUT}s after start" >&2; exit 1; fi

echo "## 2. BEFORE — probe pre-restart"
BEFORE="$(run "$PROBE" 2>&1)"; BRC=$?
echo "$BEFORE"; echo "(probe rc=$BRC)"
if [ "$BRC" -ne 0 ]; then echo "FAIL ❌ — baseline probe failed pre-restart (rc=$BRC); no valid 'before' state to compare against" >&2; exit 1; fi
ID_BEFORE="$(run "$IDENTITY" 2>/dev/null)"; IBRC=$?
echo "(identity before: ${ID_BEFORE:-<empty>} rc=$IBRC)"
if [ "$IBRC" -ne 0 ] || [ -z "$ID_BEFORE" ]; then
  echo "FAIL ❌ — identity probe gave no pre-restart value (rc=$IBRC); a restart cannot be witnessed" >&2; exit 1
fi
line

echo "## 3. restart service"
run "$RESTART"; RRC=$?; echo "(restart rc=$RRC)"
if [ "$RRC" -ne 0 ]; then echo "FAIL ❌ — restart command failed (rc=$RRC)" >&2; exit 1; fi
if ! wait_ready; then echo "FAIL ❌ — service not ready within ${READY_TIMEOUT}s after restart" >&2; exit 1; fi

echo "## 4. AFTER — probe post-restart"
AFTER="$(run "$PROBE" 2>&1)"; ARC=$?
echo "$AFTER"; echo "(probe rc=$ARC)"
ID_AFTER="$(run "$IDENTITY" 2>/dev/null)"; IARC=$?
echo "(identity after: ${ID_AFTER:-<empty>} rc=$IARC)"
line

echo "## verdict"
echo "start rc=$SRC   before-probe rc=$BRC   restart rc=$RRC   after-probe rc=$ARC   finished: $(utc)"
echo "identity: ${ID_BEFORE} -> ${ID_AFTER:-<empty>}"
if [ "$ARC" -ne 0 ]; then
  echo "FAIL ❌ — behavior probe did NOT pass after restart"
  exit 1
fi
if [ "$IARC" -ne 0 ] || [ -z "$ID_AFTER" ]; then
  echo "FAIL ❌ — identity probe gave no post-restart value (rc=$IARC); restart unwitnessed" >&2
  exit 1
fi
if [ "$ID_BEFORE" = "$ID_AFTER" ]; then
  echo "FAIL ❌ — identity unchanged ($ID_BEFORE); the probes passed but NO restart occurred" >&2
  exit 1
fi
echo "PASS ✅ — start, restart, and both probes succeeded, and identity changed; behavior round trip verified"
exit 0
