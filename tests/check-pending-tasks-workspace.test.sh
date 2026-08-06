#!/bin/bash
# The Stop hook must watch the WORKSPACE queue, not <repo>/tasks/.
#
# THE DEFECT. src/check-pending-tasks.sh resolved its queue as
# `$(dirname "$0")/../tasks` — the repo root. Every producer (voice, Discord,
# Telegram, Slack, chat) and every consumer writes <workspace>/tasks/. On a
# default install those are different directories, so the hook read an empty
# one, emitted `{}` on every Stop, and never blocked on anything. It is the
# fail-silent shape: a broken hook and an empty queue emit the same `{}`, and
# the queue is empty most of the time anyone looks.
#
# ISOLATION (@john-the-dev's blocker on d1767d43). The first version of this
# test resolved the workspace from the host config and wrote its probe into the
# caller's REAL queue — the directory `watch-tasks-stream.sh` is concurrently
# watching. The watcher could claim and execute the probe as a live owner task
# before the EXIT trap removed it, and cleanup cannot retract a reply already
# delivered. A synthetic filename lowers collision odds; it does not isolate a
# live consumer.
#
# So this test builds its own workspace and pins it: `SUTANDO_TEST_MODE=1` plus
# `SUTANDO_WORKSPACE` is the repo's supported escape hatch (src/sutando_config.py
# ~line 336 — the env var alone is ignored post-v0.8/#1440). Passing a temp dir
# is NOT isolation on its own, so the resolved path is ASSERTED before anything
# is written: if the hatch ever regresses, this aborts instead of quietly
# writing to the live queue.
#
# WHY BOTH DIRECTIONS ARE PINNED. "Blocks when a task exists" is satisfied by a
# hook that watches EITHER directory, and "stays quiet when empty" is satisfied
# by the broken version. The case that separates them is the legacy one: a file
# in <repo>/tasks/ must NOT block, because reading that directory is the bug.
# Without it, a fix watching both paths passes — and reintroduces the defect in
# the other direction, since a stale legacy file would then block forever.
#
# Run: bash tests/check-pending-tasks-workspace.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

# --- Build and PIN an isolated workspace before resolving anything ----------
TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"

LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"

# Compare resolved paths, not the strings we passed in — mktemp may hand back a
# symlinked prefix (/tmp -> /private/tmp on macOS).
_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  echo "      wanted: $TMPWS"
  echo "      got:    $WS"
  echo "      The SUTANDO_TEST_MODE escape hatch (src/sutando_config.py) has changed."
  rm -rf "$TMPWS"
  exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  rm -rf "$TMPWS"
  exit 1
fi

# The legacy probe (case 4) must go where the BUG reads, so it is the one write
# outside the temp tree. It is safe: watch-tasks-stream.sh resolves its
# TASKS_DIR from `sutando-config.sh workspace` (line 38), so <repo>/tasks/ has
# no consumer — and the guard below refuses if the two are ever the same dir.
PROBE="task-zz-hooktest-$$.txt"
LEGACY_DIR="$REPO/tasks"

cleanup() { rm -rf "$TMPWS"; rm -f "$LEGACY_DIR/$PROBE"; }
trap cleanup EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

mkdir -p "$WS/tasks" "$WS/results"

# 1. A task in the workspace queue must block. FAILS against the old path.
printf 'id: probe\ntask: probe\n' > "$WS/tasks/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "workspace task blocks" ;;
  *) bad "workspace task blocks" "got: ${OUT:0:120}" ;;
esac

# 2. ...and the payload must name the task, not just block generically.
case "$OUT" in
  *"$PROBE"*) ok "block payload names the task" ;;
  *) bad "block payload names the task" "payload omits $PROBE" ;;
esac

# 3. A task with a matching result is already handled — must not block.
printf 'done\n' > "$WS/results/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "result present suppresses the block" ;;
  *) bad "result present suppresses the block" "got: ${OUT:0:120}" ;;
esac
rm -f "$WS/tasks/$PROBE" "$WS/results/$PROBE"

# 4. THE DIRECTION CASE. A file in the legacy <repo>/tasks/ must NOT block.
if [ "$(_real "$LEGACY_DIR")" = "$(_real "$WS/tasks")" ]; then
  printf '  skip legacy dir IS the resolved queue here; direction is not decidable\n'
else
  mkdir -p "$LEGACY_DIR"
  printf 'id: probe\ntask: legacy\n' > "$LEGACY_DIR/$PROBE"
  OUT="$(bash "$HOOK" 2>&1)"
  case "$OUT" in
    '{}') ok "legacy <repo>/tasks/ does not block" ;;
    *) bad "legacy <repo>/tasks/ does not block" "hook still reads the repo: ${OUT:0:120}" ;;
  esac
  rm -f "$LEGACY_DIR/$PROBE"
fi

# 5. Empty queue stays quiet — the control that proves case 1 measured something.
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "empty queue emits {}" ;;
  *) bad "empty queue emits {}" "got: ${OUT:0:120}" ;;
esac

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
