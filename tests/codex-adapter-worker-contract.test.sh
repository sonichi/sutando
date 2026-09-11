#!/usr/bin/env bash
# The Codex adapter's half of the worker contract: what the launcher forwards
# and what the notifier then resolves, read through the scripts' own probes so
# these are the shipped derivations. The workspace seam is the LAUNCHER's: an
# adapter still deriving it from `dirname "$TASKS_DIR"` is the duplication.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
NOTIFIER="$REPO/src/agent/codex/cli/task-notifier.sh"
LAUNCHER="$REPO/src/agent/codex/cli/start-cli.sh"
WS="$TMP/ws"; WID="w-codextest"; INBOX="$WS/deliveries/$WID"
mkdir -p "$INBOX" "$TMP/cwd" "$TMP/bin" "$TMP/home"
# The launcher refuses before it assembles anything when the Codex CLI cannot
# run; these probes are about the env contract, not about Codex being present.
printf '#!/bin/bash\nexit 0\n' > "$TMP/bin/codex"
chmod +x "$TMP"/bin/*
# Real tmux and fswatch on PATH: absent, the launcher tries to brew-install them
# before it assembles anything, and never reaches a probe.
DEPS="$(dirname "$(command -v tmux || echo /usr/bin/true)")"
STUB_PATH="$TMP/bin:$DEPS:/usr/bin:/bin:/usr/sbin:/sbin"

# --- the notifier's resolution, under the worker env the spawner sets ---
env SUTANDO_TASKS_DIR="$INBOX" SUTANDO_WORKSPACE_DIR="$WS" \
    bash "$NOTIFIER" --print-paths > "$TMP/worker-paths"
grep -qx "RESULTS_DIR=$WS/results" "$TMP/worker-paths"
check $? "the worker's results land in the shared workspace, not deliveries/"
grep -qx "TASK_HANDLER_CLAIMS_DIR=$WS/state/task-event-handler-claims" "$TMP/worker-paths"
check $? "claims resolve under the workspace's state/"
grep -qx "CORE_STATUS_FILE=$WS/state/core-status.json" "$TMP/worker-paths"
check $? "core-status resolves under the workspace's state/"
! grep -q "deliveries/results\|deliveries/state" "$TMP/worker-paths"
check $? "nothing the notifier derives lands under deliveries/"

# The control: this is exactly what the three lines above resolved to before
# the workspace was named — the reviewer's captured probe.
env SUTANDO_TASKS_DIR="$INBOX" bash "$NOTIFIER" --print-paths > "$TMP/unnamed"
grep -qx "RESULTS_DIR=$WS/deliveries/results" "$TMP/unnamed"
check $? "control: unnamed, the inbox's parent IS taken as the workspace"

# --- and the core's own resolution is untouched ---
env SUTANDO_TASKS_DIR="$WS/tasks" bash "$NOTIFIER" --print-paths > "$TMP/core-paths"
grep -qx "RESULTS_DIR=$WS/results" "$TMP/core-paths"
check $? "a core inbox still resolves results/ beside tasks/ (old behaviour intact)"

# --- what the launcher forwards ---
PLAN_ENV="$TMP/plan-env"
python3 - "$REPO" "$WS" "$WID" > "$PLAN_ENV" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/src")
import spawn_worker
p = spawn_worker.plan(sys.argv[2], sys.argv[1], runtime="codex",
                      socket="/tmp/sutando-test.sock", worker_id=sys.argv[3])
for k, v in sorted(p["env"].items()):
    print(f"{k}={v}")
PY
run_probe() {  # one launch, one assembled array, one token per line
  # shellcheck disable=SC2046
  env -i HOME="$TMP/home" PATH="$STUB_PATH" SUTANDO_CODEX_WORKING_DIR="$TMP/cwd" \
      $(cat "$PLAN_ENV") bash "$LAUNCHER" "$1"
}

# The notifier's env reads, intersected with what the spawner sets: neither file
# alone names the required set, so neither can drift out of it.
READS="$(grep -oE '\$\{SUTANDO_[A-Z_]+:' "$NOTIFIER" | tr -d '${:' | sort -u)"
run_probe --print-notifier-env > "$TMP/fwd-notifier"
missing=""
while IFS='=' read -r k v; do
  echo "$READS" | grep -qx "$k" || continue
  grep -qx -- "$k=$v" "$TMP/fwd-notifier" || missing="$missing $k"
done < "$PLAN_ENV"
[ -z "$missing" ]
check $? "the notifier is handed every var the spawner sets and it reads${missing:+ — MISSING:$missing}"

# The session's own scripts resolve the workspace the way the notifier does, so
# the core session needs the same four facts about which instance it is.
run_probe --print-core-env > "$TMP/fwd-core"
missing=""
for k in SUTANDO_INSTANCE_ID SUTANDO_TASKS_DIR SUTANDO_WORKSPACE_DIR SUTANDO_RESULTS_DIR; do
  v="$(grep -m1 "^$k=" "$PLAN_ENV" | cut -d= -f2-)"
  grep -qx -- "$k=$v" "$TMP/fwd-core" || missing="$missing $k"
done
[ -z "$missing" ]
check $? "the codex session is told which instance and workspace it is${missing:+ — MISSING:$missing}"

# Backward compatibility: an install that sets none forwards none.
env -i HOME="$TMP/home" PATH="$STUB_PATH" SUTANDO_CODEX_WORKING_DIR="$TMP/cwd" \
    bash "$LAUNCHER" --print-core-env > "$TMP/fwd-bare" 2>/dev/null
! grep -q "SUTANDO_WORKSPACE_DIR\|SUTANDO_INBOX_KIND" "$TMP/fwd-bare"
check $? "a core install forwards no worker vars (old behaviour intact)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
