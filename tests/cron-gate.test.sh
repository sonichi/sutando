#!/usr/bin/env bash
# Unit-of-the-logic tests for scripts/cron-gate.sh — the queue-defer wrapper
# for non-loop crons. Standalone shell test (no node), runs without any other
# Sutando service. Exits non-zero on first failure.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
GATE="$REPO/scripts/cron-gate.sh"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
mkdir -p "$TMPDIR/tasks"

fail() { echo "FAIL: $1" >&2; exit 1; }
ok()   { echo "  ok  $1"; }

# --- empty tasks/ → runs wrapped command --------------------------------------
out="$(SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-empty echo 'ran' 2>&1)"
[ "$out" = "ran" ] || fail "empty queue: expected 'ran', got '$out'"
ok "empty queue runs wrapped command"

# --- queued task → defers (prints message, exits 0, does NOT run command) ----
touch "$TMPDIR/tasks/task-1234567890123.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-queued echo 'should-not-run' 2>&1)"
case "$out" in
  *"deferring test-queued"*) : ;;
  *) fail "queued: expected 'deferring test-queued' in output, got '$out'" ;;
esac
case "$out" in
  *"should-not-run"*) fail "queued: wrapped command ran (output included 'should-not-run')" ;;
  *) : ;;
esac
ok "queued task defers and does not run wrapped command"

# --- tasks/ missing entirely → runs wrapped command --------------------------
rm -rf "$TMPDIR/tasks"
out="$(SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-missing echo 'ran-no-dir' 2>&1)"
[ "$out" = "ran-no-dir" ] || fail "missing tasks/: expected 'ran-no-dir', got '$out'"
ok "missing tasks/ directory runs wrapped command"

# --- archive/processed subdirs do NOT count as queued ------------------------
mkdir -p "$TMPDIR/tasks/archive" "$TMPDIR/tasks/processed"
touch "$TMPDIR/tasks/archive/task-old1.txt" "$TMPDIR/tasks/processed/task-old2.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-archived echo 'ran-archived-ok' 2>&1)"
[ "$out" = "ran-archived-ok" ] || fail "archive/processed: expected 'ran-archived-ok', got '$out'"
ok "archive/processed subdirs do not trigger deferral"

# --- non-task-* file in tasks/ does NOT count -------------------------------
touch "$TMPDIR/tasks/README.md"
out="$(SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-non-task echo 'ran-non-task-ok' 2>&1)"
[ "$out" = "ran-non-task-ok" ] || fail "non-task file: expected 'ran-non-task-ok', got '$out'"
ok "non-task-*.txt files do not trigger deferral"

# --- usage error: no command → exit 2 -----------------------------------------
set +e
SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-usage >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "usage error: expected exit 2, got $rc"
ok "missing command produces usage error (exit 2)"

# --- wrapped command's exit code propagates ----------------------------------
set +e
SUTANDO_WORKSPACE="$TMPDIR" bash "$GATE" test-rc bash -c 'exit 42' >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 42 ] || fail "exit propagation: expected 42, got $rc"
ok "wrapped command exit code propagates via exec"

echo
echo "OK — 7/7 cron-gate tests passed"
