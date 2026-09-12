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

# SUTANDO_TEST_MODE=1 lets resolve_workspace() honor SUTANDO_WORKSPACE silently
# (without emitting the v0.8 deprecation warning that contaminates captured output).
export SUTANDO_TEST_MODE=1

fail() { echo "FAIL: $1" >&2; exit 1; }
# Counted, not hardcoded: a literal total silently under-reports added cases.
PASSED=0
ok()   { PASSED=$((PASSED + 1)); echo "  ok  $1"; }

# --- empty tasks/ → runs wrapped command --------------------------------------
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-empty echo 'ran' 2>&1)"
[ "$out" = "ran" ] || fail "empty queue: expected 'ran', got '$out'"
ok "empty queue runs wrapped command"

# --- queued task → defers (prints message, exits 0, does NOT run command) ----
touch "$TMPDIR/tasks/task-1234567890123.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-queued echo 'should-not-run' 2>&1)"
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
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-missing echo 'ran-no-dir' 2>&1)"
[ "$out" = "ran-no-dir" ] || fail "missing tasks/: expected 'ran-no-dir', got '$out'"
ok "missing tasks/ directory runs wrapped command"

# --- archive/processed subdirs do NOT count as queued ------------------------
mkdir -p "$TMPDIR/tasks/archive" "$TMPDIR/tasks/processed"
touch "$TMPDIR/tasks/archive/task-old1.txt" "$TMPDIR/tasks/processed/task-old2.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-archived echo 'ran-archived-ok' 2>&1)"
[ "$out" = "ran-archived-ok" ] || fail "archive/processed: expected 'ran-archived-ok', got '$out'"
ok "archive/processed subdirs do not trigger deferral"

# --- non-task-* file in tasks/ does NOT count -------------------------------
touch "$TMPDIR/tasks/README.md"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-non-task echo 'ran-non-task-ok' 2>&1)"
[ "$out" = "ran-non-task-ok" ] || fail "non-task file: expected 'ran-non-task-ok', got '$out'"
ok "non-task-*.txt files do not trigger deferral"

# --- task-cron-*.txt (cron-runner emission) does NOT count -------------------
# Regression: a cron-gate-wrapped entry migrated to launchd is delivered as its
# own task-cron-<name>-<ms>.txt file; that file must not make the gate defer, or
# the entry defers on its own delivery vehicle forever.
touch "$TMPDIR/tasks/task-cron-sync-workspace-1785114482357.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-cron-emit echo 'ran-despite-cron-file' 2>&1)"
[ "$out" = "ran-despite-cron-file" ] || fail "task-cron-* file: expected 'ran-despite-cron-file', got '$out'"
ok "task-cron-*.txt (cron-runner emission) does not trigger deferral"

# --- but a genuine owner task-*.txt alongside it STILL defers -----------------
touch "$TMPDIR/tasks/task-9876543210987.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-owner-plus-cron echo 'should-not-run' 2>&1)"
case "$out" in
  *"deferring test-owner-plus-cron"*) : ;;
  *) fail "owner+cron: expected deferral, got '$out'" ;;
esac
case "$out" in
  *"should-not-run"*) fail "owner+cron: wrapped command ran" ;;
  *) : ;;
esac
ok "genuine owner task still defers even when a task-cron-* file is present"
rm -f "$TMPDIR/tasks/task-cron-sync-workspace-1785114482357.txt" "$TMPDIR/tasks/task-9876543210987.txt"

# --- task-workstream-grouping-*.txt (classifier emission) does NOT count ------
# Queued only while the core is idle and declares access_tier: owner, so the
# tier filter cannot tell it from a human DM. Body is the real emitted shape.
cat > "$TMPDIR/tasks/task-workstream-grouping-1786301397837.txt" <<'CLASSIFIER'
id: task-workstream-grouping-1786301397837
source: task-workstream-grouping
access_tier: owner
priority: low
task: Internal maintenance only.
CLASSIFIER
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-classifier-emit echo 'ran-despite-classifier' 2>&1)"
[ "$out" = "ran-despite-classifier" ] || fail "task-workstream-grouping-*: expected 'ran-despite-classifier', got '$out'"
ok "task-workstream-grouping-*.txt (classifier emission) does not trigger deferral"

# legacy name is still recognised by the emitter, so exclude it too
touch "$TMPDIR/tasks/task-project-grouping-1786301397838.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-legacy-emit echo 'ran-despite-legacy' 2>&1)"
[ "$out" = "ran-despite-legacy" ] || fail "task-project-grouping-*: expected 'ran-despite-legacy', got '$out'"
ok "legacy task-project-grouping-*.txt does not trigger deferral"

# --- but a genuine owner task alongside a classifier task STILL defers --------
touch "$TMPDIR/tasks/task-9876543210988.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-owner-plus-classifier echo 'should-not-run' 2>&1)"
case "$out" in
  *"deferring test-owner-plus-classifier"*) : ;;
  *) fail "owner+classifier: expected deferral, got '$out'" ;;
esac
case "$out" in
  *"should-not-run"*) fail "owner+classifier: wrapped command ran" ;;
  *) : ;;
esac
ok "genuine owner task still defers alongside a classifier task"
rm -f "$TMPDIR/tasks/task-workstream-grouping-1786301397837.txt" \
      "$TMPDIR/tasks/task-project-grouping-1786301397838.txt" \
      "$TMPDIR/tasks/task-9876543210988.txt"

# --- usage error: no command → exit 2 -----------------------------------------
set +e
SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-usage >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "usage error: expected exit 2, got $rc"
ok "missing command produces usage error (exit 2)"

# --- wrapped command's exit code propagates ----------------------------------
set +e
SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-rc bash -c 'exit 42' >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 42 ] || fail "exit propagation: expected 42, got $rc"
ok "wrapped command exit code propagates via exec"

echo

# --- a task that EXPLICITLY declares a non-owner tier does NOT defer ----------
# Regression for the 2026-08-03 starvation: peer #bot2bot notices carry
# access_tier: team and were deferring owner-facing crons. 6 team-tier tasks
# arrived in one hour that night, so a busy peer can starve the cron entirely.
rm -f "$TMPDIR/tasks/"task-*.txt
printf 'id: t\naccess_tier: team\ntask: peer notice\n' > "$TMPDIR/tasks/task-team-1.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-team echo 'ran-despite-team' 2>&1)"
[ "$out" = "ran-despite-team" ] || fail "team-tier task must NOT defer, got '$out'"
ok "access_tier: team does not trigger deferral"

for tier in other ambient guest collaborator; do
  rm -f "$TMPDIR/tasks/"task-*.txt
  printf 'id: t\naccess_tier: %s\n' "$tier" > "$TMPDIR/tasks/task-$tier-1.txt"
  out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-$tier echo "ran-$tier" 2>&1)"
  [ "$out" = "ran-$tier" ] || fail "access_tier: $tier must NOT defer, got '$out'"
done
ok "access_tier: other / ambient / guest / collaborator do not trigger deferral"

# --- but an OWNER task still defers, even beside non-owner ones ---------------
printf 'id: t\naccess_tier: owner\ntask: real owner work\n' > "$TMPDIR/tasks/task-owner-1.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-mixed echo 'should-not-run' 2>&1)"
case "$out" in
  *"deferring test-mixed"*) : ;;
  *) fail "owner task beside non-owner ones must still defer, got '$out'" ;;
esac
ok "an owner task still defers even alongside team/other/ambient/guest tasks"

# --- a task with NO access_tier line still defers (fails CLOSED) --------------
# CLAUDE.md: tasks without an access_tier field get full owner processing, so an
# unstated tier must yield rather than be silently treated as peer traffic.
rm -f "$TMPDIR/tasks/"task-*.txt
printf 'id: t\ntask: no tier declared\n' > "$TMPDIR/tasks/task-notier-1.txt"
out="$(SUTANDO_WORKSPACE="$TMPDIR" SUTANDO_TEST_MODE=1 bash "$GATE" test-notier echo 'should-not-run' 2>&1)"
case "$out" in
  *"deferring test-notier"*) : ;;
  *) fail "task with no access_tier must still defer (fail closed), got '$out'" ;;
esac
ok "a task with no access_tier still defers — unknown tier fails closed"
rm -f "$TMPDIR/tasks/"task-*.txt

echo "OK — $PASSED/$PASSED cron-gate tests passed"
