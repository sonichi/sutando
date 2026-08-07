#!/usr/bin/env bash
# Wiring test for the REAL launcher (#2701 review P1, bassil): the named
# secondary-gateway loop in src/startup.sh must thread REMOTE_TASK_CHANNEL_DIR
# per instance, or a dev bridge inherits prod's channels/ag2space/ config.
# Runs the actual loop body with a stub python that records its environment.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Stub interpreter: dump the env we care about, exit (no real bridge).
cat > "$TMP/py-stub" <<'STUB'
#!/usr/bin/env bash
env | grep -E '^(REMOTE_TASK_CHANNEL_DIR|GATEWAY_INSTANCE|REMOTE_TASK_TOKEN)=' \
  >> "$ENV_DUMP"
STUB
chmod +x "$TMP/py-stub"

# Extract the named-gateway loop from the real startup.sh (single source: the
# test fails if the loop moves or the marker comment is renamed).
LOOP="$(awk '/# Named secondary gateways \(multi-gateway\)/,/^  done$/' "$REPO/src/startup.sh")"
[ -n "$LOOP" ] || { echo "FAIL: named-gateway loop not found in startup.sh"; exit 1; }

run_loop() {  # $1 = extra env assignments (string), evaluated before the loop
  ENV_DUMP="$TMP/env-dump"; : > "$ENV_DUMP"; export ENV_DUMP
  ( eval "$1"
    PY="$TMP/py-stub"; REPO="$REPO"; LOGS_DIR="$TMP"
    eval "$LOOP"
    wait ) >/dev/null 2>&1
  cat "$ENV_DUMP"
}

# Case 1: default convention — instance "dev" gets channels/dev-ag2space/.
OUT="$(run_loop 'export AG2_REMOTE_TOKEN_DEV=tok-dev')"
echo "$OUT" | grep -q '^REMOTE_TASK_CHANNEL_DIR=dev-ag2space$' \
  || { echo "FAIL: dev instance did not get REMOTE_TASK_CHANNEL_DIR=dev-ag2space"; echo "$OUT"; exit 1; }
echo "  ok  dev instance defaults to dev-ag2space"

# Case 2: operator override wins.
OUT="$(run_loop 'export AG2_REMOTE_TOKEN_DEV=tok-dev REMOTE_TASK_CHANNEL_DIR_DEV=dev.ag2.space')"
echo "$OUT" | grep -q '^REMOTE_TASK_CHANNEL_DIR=dev.ag2.space$' \
  || { echo "FAIL: override REMOTE_TASK_CHANNEL_DIR_DEV not honored"; echo "$OUT"; exit 1; }
echo "  ok  per-instance override honored"

# Case 3: the instance never runs with prod's dir.
echo "$OUT" | grep -q '^REMOTE_TASK_CHANNEL_DIR=ag2space$' \
  && { echo "FAIL: instance leaked prod channel dir"; exit 1; }
echo "  ok  prod channel dir never inherited"
echo "PASS — launcher threads REMOTE_TASK_CHANNEL_DIR per instance"
