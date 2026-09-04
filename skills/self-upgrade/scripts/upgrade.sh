#!/bin/bash
# self-upgrade — the mechanical half of a safe Sutando self-upgrade.
#
# Pulls the repo to latest and restarts background services WITHOUT bricking
# the running core session. The restart belongs to a detached tmux session:
# unlike a plain `nohup ... &`, tmux survives teardown of the Codex executor
# that launched this script, and the parked service pane keeps restarted
# background bridges alive.
#
# What this script does NOT do (agent-side, handled by SKILL.md):
#   - run the post-upgrade health check / report to the owner
#
# Usage:
#   bash skills/self-upgrade/scripts/upgrade.sh [--remote <name>] [--branch <name>] [--no-restart] [--canary owner/repo#N]
# Exit codes: 0 = upgraded (or already latest); 2 = aborted (dirty tree / not FF-able);
#             4 = refused by the witness-owed gate (or the gate could not run)

set -uo pipefail

REMOTE="origin"
BRANCH="main"
DO_RESTART=1
CANARY=""
SERVICE_SESSION="sutando-services"
DONE_MARKER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --remote) REMOTE="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --no-restart) DO_RESTART=0; shift ;;
    --canary) CANARY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO" || { echo "self-upgrade: cannot cd to repo root" >&2; exit 2; }

echo "self-upgrade: repo=$REPO  remote=$REMOTE  branch=$BRANCH"

# 1. Clean working tree required — never clobber uncommitted work.
if [ -n "$(git status --porcelain)" ]; then
  echo "self-upgrade: ABORT — working tree is dirty ($(git status --porcelain | wc -l | tr -d ' ') files). Commit or stash first." >&2
  exit 2
fi

# A restart is only safe if its owner outlives this executor. Preflight the
# durable handoff before pulling so a host without tmux is left untouched.
TMUX_BIN=""
TMUX_SOCKET=""
if [ "$DO_RESTART" = "1" ]; then
  TMUX_BIN="$(command -v tmux 2>/dev/null || true)"
  if [ -z "$TMUX_BIN" ]; then
    echo "self-upgrade: ABORT — tmux is required for a restart that survives the task executor. Install tmux or use --no-restart." >&2
    exit 2
  fi
  TMUX_SOCKET="$(bash "$REPO/scripts/sutando-config.sh" tmux-socket 2>/dev/null || true)"
  if [ -z "$TMUX_SOCKET" ]; then
    echo "self-upgrade: ABORT — could not resolve the Sutando tmux socket." >&2
    exit 2
  fi
  SOCKET_TAG="$(printf '%s' "$TMUX_SOCKET" | cksum | awk '{print $1}')"
  DONE_MARKER="/tmp/sutando-self-upgrade-$SOCKET_TAG.done"
  if "$TMUX_BIN" -S "$TMUX_SOCKET" has-session -t "=$SERVICE_SESSION" 2>/dev/null; then
    EXISTING_COMMAND="$("$TMUX_BIN" -S "$TMUX_SOCKET" list-panes -t "=$SERVICE_SESSION" -F '#{pane_current_command}' 2>/dev/null | head -1 || true)"
    EXISTING_PID="$("$TMUX_BIN" -S "$TMUX_SOCKET" list-panes -t "=$SERVICE_SESSION" -F '#{pane_pid}' 2>/dev/null | head -1 || true)"
    COMPLETED_PID="$(cat "$DONE_MARKER" 2>/dev/null || true)"
    if [ -z "$EXISTING_PID" ] || [ "$COMPLETED_PID" != "$EXISTING_PID" ]; then
      echo "self-upgrade: ABORT — $SERVICE_SESSION is still active ($EXISTING_COMMAND). Wait for that restart to finish." >&2
      exit 2
    fi
  fi
fi

# 2. Fetch + measure the gap.
git fetch "$REMOTE" --quiet || { echo "self-upgrade: git fetch failed" >&2; exit 2; }
LOCAL="$(git rev-parse --short HEAD)"
BEHIND="$(git rev-list --count "HEAD..$REMOTE/$BRANCH" 2>/dev/null || echo 0)"
AHEAD="$(git rev-list --count "$REMOTE/$BRANCH..HEAD" 2>/dev/null || echo 0)"
echo "self-upgrade: local=$LOCAL  behind=$BEHIND  ahead=$AHEAD"

if [ "$BEHIND" = "0" ]; then
  echo "self-upgrade: already at latest ($LOCAL). Nothing to pull."
  exit 0
fi
if [ "$AHEAD" != "0" ]; then
  echo "self-upgrade: ABORT — local is $AHEAD commit(s) ahead of $REMOTE/$BRANCH; not a fast-forward. Resolve manually." >&2
  exit 2
fi

# 3. Heads-up if a rebuild is likely needed (dependency/build files changed).
REBUILD="$(git diff --name-only "HEAD..$REMOTE/$BRANCH" | grep -iE 'package.*\.json|package-lock|tsconfig|\.swift$|requirements' || true)"
if [ -n "$REBUILD" ]; then
  echo "self-upgrade: NOTE — dependency/build files changed; a rebuild (npm ci / tsc) may be needed after restart:"
  echo "$REBUILD" | sed 's/^/    /'
fi

# 3.5 Witness-owed gate: a merged live-path PR that still owes its post-restart
#     round trip (REVIEW.md lesson 15) is not activated here, except as a declared
#     canary on the host that owes it. Runs BEFORE the pull so a refusal leaves HEAD
#     alone, and FAILS CLOSED: a gate it cannot run is a gate it cannot pass.
GATE_WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
GATE_HOST="$(bash "$REPO/scripts/sutando-config.sh" host-label 2>/dev/null || true)"
GATE_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || true)"
GATE_HELPER="$REPO/src/witness_owed.py"
[ -n "$GATE_WS" ] || { echo "self-upgrade: ABORT — cannot resolve the workspace, so the witness-owed gate cannot run" >&2; exit 4; }
# An unresolved host label cannot release anything (no canary matches ""), so
# `check` still runs; only a canary declaration needs the label and fails closed.
[ -n "$GATE_PY" ] || { echo "self-upgrade: ABORT — no usable python (sutando-config.sh python-bin), so the witness-owed gate cannot run" >&2; exit 4; }
[ -f "$GATE_HELPER" ] || { echo "self-upgrade: ABORT — $GATE_HELPER is missing, so the witness-owed gate cannot run" >&2; exit 4; }
# The target repository scopes the gate: another project's (#N) is not ours.
GATE_URL="$(git remote get-url "$REMOTE" 2>/dev/null || true)"
GATE_REPO="$(printf '%s' "$GATE_URL" | sed -E 's#/+$##; s#\.git$##' | sed -nE 's#^.*[/:]([^/:]+)/([^/:]+)$#\1/\2#p')"
[ -n "$GATE_REPO" ] || { echo "self-upgrade: ABORT — cannot derive owner/name from remote '$REMOTE' ($GATE_URL), so the witness-owed gate cannot scope its records" >&2; exit 4; }
# Fleet view: with the vault on, refresh it first and fail closed if that fails;
# with it off, foreign host subtrees are a fleet this host cannot refresh.
GATE_VAULT="$(bash "$REPO/scripts/sutando-config.sh" vault-enabled 2>/dev/null || true)"
GATE_MAX_AGE="${SUTANDO_WITNESS_MAX_AGE:-3600}"
# nan/inf parse as floats and defeat expiry entirely, so a stale fleet view
# would read as fresh forever. Manifest declaration is still owed.
case "$GATE_MAX_AGE" in
  *[!0-9.]*|""|".") echo "self-upgrade: ABORT — SUTANDO_WITNESS_MAX_AGE must be a finite positive number of seconds, got '$GATE_MAX_AGE'" >&2; exit 4;;
esac
awk -v v="$GATE_MAX_AGE" 'BEGIN{ exit !(v+0 > 0) }' ||
  { echo "self-upgrade: ABORT — SUTANDO_WITNESS_MAX_AGE must be > 0, got '$GATE_MAX_AGE'" >&2; exit 4; }
if [ "$GATE_VAULT" = "true" ]; then
  bash "$REPO/scripts/sync-workspace.sh" --pull-only || { echo "self-upgrade: ABORT — vault pull failed, so the fleet's witness-owed records cannot be called fresh" >&2; exit 4; }
elif [ -d "$GATE_WS/hosts" ] && [ -n "$GATE_HOST" ] && find "$GATE_WS/hosts" -mindepth 2 -maxdepth 2 -name witness-owed -not -path "$GATE_WS/hosts/$GATE_HOST/*" | grep -q .; then
  echo "self-upgrade: ABORT — foreign host witness-owed subtrees exist but the vault is disabled, so they cannot be refreshed" >&2
  echo "  Enable the vault, or re-stamp this host's view once its records are current:" >&2
  echo "    $GATE_PY $GATE_HELPER --workspace $GATE_WS publish --host ${GATE_HOST:-<this-host>}" >&2
  exit 4
fi
if [ -n "$CANARY" ]; then
  [ -n "$GATE_HOST" ] || { echo "self-upgrade: ABORT — cannot resolve this host's label, so it cannot be declared the canary for $CANARY" >&2; exit 4; }
  "$GATE_PY" "$GATE_HELPER" --workspace "$GATE_WS" canary "$CANARY" --host "$GATE_HOST" >/dev/null ||
    { echo "self-upgrade: ABORT — cannot declare $GATE_HOST the canary for $CANARY (no open record, or a different host owes it)" >&2; exit 4; }
  echo "self-upgrade: canary activation of $CANARY declared for $GATE_HOST — post the round trip and close the record"
fi
if ! "$GATE_PY" "$GATE_HELPER" --workspace "$GATE_WS" check --ref "$REMOTE/$BRANCH" --current HEAD --repo-root "$REPO" --repo "$GATE_REPO" --max-age "$GATE_MAX_AGE" ${GATE_HOST:+--host "$GATE_HOST"}; then
  echo "self-upgrade: ABORT — the target head newly contains a live-path PR that still owes its witness (listed above)." >&2
  echo "  Post the exact-head round trip to the PR thread, then close the record ON THE OWING HOST:" >&2
  echo "    $GATE_PY $GATE_HELPER --workspace $GATE_WS close owner/repo#N --witness <url> --host <owing-host>" >&2
  echo "  From any OTHER host, tombstone it instead (same arguments, --host is THIS host):" >&2
  echo "    $GATE_PY $GATE_HELPER --workspace $GATE_WS tombstone owner/repo#N --witness <url> --host ${GATE_HOST:-<this-host>}" >&2
  echo "  Or, on the host that owes it, activate as the declared canary: $0 --canary owner/repo#N" >&2
  exit 4
fi

# 4. Fast-forward pull — the actual code upgrade.
git pull --ff-only "$REMOTE" "$BRANCH" || { echo "self-upgrade: git pull --ff-only failed" >&2; exit 2; }
NOW="$(git rev-parse --short HEAD)"
# This host's view is now current: stamp it so peers can tell fresh from stale.
[ -n "$GATE_HOST" ] && "$GATE_PY" "$GATE_HELPER" --workspace "$GATE_WS" publish --host "$GATE_HOST" >/dev/null 2>&1 || true
echo "self-upgrade: pulled $LOCAL -> $NOW (0 behind)"

if [ "$DO_RESTART" = "0" ]; then
  echo "self-upgrade: --no-restart set; skipping service restart. New code applies on next restart."
  exit 0
fi

# Capture a timestamp before launching the restart. The always-on core heartbeat
# should advance past it even on hosts where no optional channel bridge is
# configured.
WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
[ -n "$WORKSPACE" ] || {
  echo "self-upgrade: ABORT — could not resolve the Sutando workspace" >&2
  exit 2
}
VERIFY_STAMP="$(mktemp -t sutando-upgrade-verify.XXXXXX 2>/dev/null || true)"

# 5. THE LOAD-BEARING STEP — give the restart to a persistent tmux service pane.
#    Plain `nohup ... &` is reaped by Codex executor teardown. The pane parks
#    after startup so its background services keep a durable parent.
mkdir -p "$WORKSPACE/logs" ||
  { echo "self-upgrade: cannot create workspace log directory" >&2; exit 2; }
LOG="$WORKSPACE/logs/self-upgrade-restart.log"
if "$TMUX_BIN" -S "$TMUX_SOCKET" has-session -t "=$SERVICE_SESSION" 2>/dev/null; then
  "$TMUX_BIN" -S "$TMUX_SOCKET" kill-session -t "=$SERVICE_SESSION" ||
    { echo "self-upgrade: could not clear completed $SERVICE_SESSION session" >&2; exit 2; }
fi
: > "$LOG" || { echo "self-upgrade: cannot write restart log: $LOG" >&2; exit 2; }
: > "$DONE_MARKER" || { echo "self-upgrade: cannot write completion marker: $DONE_MARKER" >&2; exit 2; }
CORE_SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
printf -v RESTART_COMMAND \
  'export SUTANDO_TMUX_SOCKET=%q SUTANDO_TMUX_SESSION=%q; cd %q && bash %q >> %q 2>&1; rc=$?; printf "%%s\n" "$$" > %q; printf "self-upgrade: restart exit=%%s\n" "$rc" >> %q; exec sleep 2147483647' \
  "$TMUX_SOCKET" "$CORE_SESSION" "$REPO" "$REPO/src/restart.sh" "$LOG" "$DONE_MARKER" "$LOG"
"$TMUX_BIN" -S "$TMUX_SOCKET" new-session -d -s "$SERVICE_SESSION" "$RESTART_COMMAND" ||
  { echo "self-upgrade: durable restart handoff failed" >&2; exit 2; }
"$TMUX_BIN" -S "$TMUX_SOCKET" has-session -t "=$SERVICE_SESSION" 2>/dev/null ||
  { echo "self-upgrade: durable restart session disappeared immediately; check $LOG" >&2; exit 2; }
echo "self-upgrade: restart.sh handed to durable tmux session $SERVICE_SESSION (log: $LOG)"

# 6. Verify the core heartbeat advances while services restart (best-effort,
#    bounded). Do not key this on a specific channel bridge: every bridge is
#    optional and may be intentionally unconfigured on this host.
#    SUTANDO_UPGRADE_VERIFY_TRIES caps the wait (each try = ~2s); default 45.
CORE_ALIVE_DIR="$WORKSPACE/state/cores"
heartbeat_advanced() {
  [ -n "$VERIFY_STAMP" ] && [ -d "$CORE_ALIVE_DIR" ] &&
    find "$CORE_ALIVE_DIR" -type f -name '*.alive' -newer "$VERIFY_STAMP" -print -quit 2>/dev/null | grep -q .
}
for _ in $(seq 1 "${SUTANDO_UPGRADE_VERIFY_TRIES:-45}"); do
  if heartbeat_advanced; then break; fi
  sleep 2
done
if heartbeat_advanced; then
  echo "self-upgrade: ✓ core heartbeat advancing while services restart"
else
  echo "self-upgrade: ⚠ core heartbeat has not advanced yet — startup.sh may still be building; check $LOG"
fi
[ -z "$VERIFY_STAMP" ] || rm -f "$VERIFY_STAMP"

cat <<'NEXT'
self-upgrade: mechanical steps done. AGENT MUST NOW:
  1. Run `python3 src/health-check.py` and confirm all-green.
  2. Confirm the managed task notifier session was recreated.
  3. Do NOT hand-kill the persistent service session; it owns the restarted
     background processes. Inspect the restart log before taking any action.
NEXT
exit 0
