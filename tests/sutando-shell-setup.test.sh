#!/usr/bin/env bash
# Tests for scripts/sutando-shell-setup.sh
#
# Covers: --check (4 states), --commit (4 states), --migrate (2 states),
# dry-run (default mode). Isolation strategy: each test runs with a fresh
# $HOME set to a tmpdir, a fresh $SUTANDO_WORKSPACE pointing at another
# tmpdir, and a fresh tmpdir-rooted "fake claude" stub on PATH so no real
# user state is touched.
#
# Run:    bash tests/sutando-shell-setup.test.sh
# Exit:   0 on pass, 1 on first failure (last failure if -k is set).
#
# Design note: we deliberately avoid the "set up a fake git repo" path for
# tests 1-11 by passing through SUTANDO_WORKSPACE — the helper resolves
# CLAUDE_CONFIG_DIR via `bash scripts/sutando-config.sh claude-sutando-config-dir`
# which honors the env var. That keeps the test setup small.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="$REPO/scripts/sutando-shell-setup.sh"

if [ ! -x "$HELPER" ]; then
  echo "FATAL: $HELPER not found or not executable" >&2
  exit 1
fi

PASS=0
FAIL=0
KEEP_GOING="${KEEP_GOING:-0}"  # set to 1 to keep going on failure

# ----------------------------------------------------------------------
# Test harness
# ----------------------------------------------------------------------

assert_eq() {
  local actual="$1" expected="$2" msg="${3:-}"
  if [ "$actual" = "$expected" ]; then return 0; fi
  echo "  FAIL ${msg}: expected '$expected', got '$actual'"
  return 1
}

assert_grep() {
  local pattern="$1" haystack="$2" msg="${3:-}"
  if echo "$haystack" | grep -qE "$pattern"; then return 0; fi
  echo "  FAIL ${msg}: pattern '$pattern' not in haystack"
  echo "    haystack: $(echo "$haystack" | head -3)"
  return 1
}

assert_nogrep() {
  local pattern="$1" haystack="$2" msg="${3:-}"
  if ! echo "$haystack" | grep -qE "$pattern"; then return 0; fi
  echo "  FAIL ${msg}: pattern '$pattern' unexpectedly present"
  return 1
}

assert_file_contains() {
  local pattern="$1" file="$2" msg="${3:-}"
  if grep -qE "$pattern" "$file"; then return 0; fi
  echo "  FAIL ${msg}: pattern '$pattern' not in $file"
  echo "    file contents:"
  sed 's/^/      /' "$file"
  return 1
}

assert_file_not_contains() {
  local pattern="$1" file="$2" msg="${3:-}"
  if ! grep -qE "$pattern" "$file"; then return 0; fi
  echo "  FAIL ${msg}: pattern '$pattern' unexpectedly in $file"
  return 1
}

# Each test runs in a fresh sandbox. Sandbox lifecycle: setup_test sets
# HOME + SUTANDO_WORKSPACE to fresh tmpdirs, populates an empty rc file
# at $HOME/.zshrc (mimicking zsh which is the default on macOS).
setup_test() {
  TEST_TMP="$(mktemp -d -t sutando-shell-setup-test.XXXXXX)"
  export HOME="$TEST_TMP/home"
  export SUTANDO_WORKSPACE="$TEST_TMP/workspace"
  export SHELL="/bin/zsh"  # force zsh path so $HOME/.zshrc is the target rc
  mkdir -p "$HOME" "$SUTANDO_WORKSPACE/state"
  touch "$HOME/.zshrc"
  RC_FILE="$HOME/.zshrc"
}

teardown_test() {
  [ -n "${TEST_TMP:-}" ] && rm -rf "$TEST_TMP" || true
  unset HOME SUTANDO_WORKSPACE SHELL TEST_TMP RC_FILE
}

run_test() {
  local name="$1"; shift
  printf '%-60s' "$name"
  setup_test
  if "$@"; then
    echo "ok"
    PASS=$((PASS + 1))
  else
    echo "FAIL"
    FAIL=$((FAIL + 1))
    [ "$KEEP_GOING" = "1" ] || { teardown_test; finalize_and_exit; }
  fi
  teardown_test
}

finalize_and_exit() {
  echo
  echo "----------------------------------------"
  echo "PASSED: $PASS"
  echo "FAILED: $FAIL"
  if [ "$FAIL" -gt 0 ]; then exit 1; fi
  exit 0
}

# Marker constants (must match the helper script's MARKER_BEGIN / MARKER_END).
MARKER_BEGIN='# >>> sutando-shell-setup managed block — do not edit between markers'
MARKER_END='# <<< sutando-shell-setup managed block'

# ----------------------------------------------------------------------
# 1. --check on empty rc → "absent", exit 1
# ----------------------------------------------------------------------
test_check_empty() {
  out="$(bash "$HELPER" --check 2>&1)"; rc=$?
  assert_eq "$rc" "1" "exit code" || return 1
  assert_grep "absent" "$out" "stderr says absent" || return 1
}

# ----------------------------------------------------------------------
# 2. --check on rc with legacy alias → "legacy", exit 1
# ----------------------------------------------------------------------
test_check_legacy_alias() {
  echo "alias claude-sutando='CLAUDE_CONFIG_DIR=/some/old/path claude'" >> "$RC_FILE"
  out="$(bash "$HELPER" --check 2>&1)"; rc=$?
  assert_eq "$rc" "1" "exit code" || return 1
  assert_grep "legacy" "$out" "stderr says legacy" || return 1
}

# ----------------------------------------------------------------------
# 3. --check on rc with current managed block → "ok", exit 0
# ----------------------------------------------------------------------
test_check_current_block() {
  # Use --commit to install the current block, then --check should report ok.
  bash "$HELPER" --commit >/dev/null 2>&1
  out="$(bash "$HELPER" --check 2>&1)"; rc=$?
  assert_eq "$rc" "0" "exit code" || return 1
  assert_grep "ok" "$out" "stderr says ok" || return 1
}

# ----------------------------------------------------------------------
# 4. --check on rc with stale managed block (drift) → "drift", exit 1
# ----------------------------------------------------------------------
test_check_drift_block() {
  # Install a managed block with a stale function body.
  {
    echo
    echo "$MARKER_BEGIN"
    echo "claude-sutando() { echo 'old stale body'; }"
    echo "$MARKER_END"
  } >> "$RC_FILE"
  out="$(bash "$HELPER" --check 2>&1)"; rc=$?
  assert_eq "$rc" "1" "exit code" || return 1
  assert_grep "drift" "$out" "stderr says drift" || return 1
}

# ----------------------------------------------------------------------
# 5. --commit on empty rc → appends managed block, exit 0
# ----------------------------------------------------------------------
test_commit_clean_append() {
  bash "$HELPER" --commit >/dev/null 2>&1; rc=$?
  assert_eq "$rc" "0" "exit code" || return 1
  assert_file_contains "$MARKER_BEGIN" "$RC_FILE" "begin marker present" || return 1
  assert_file_contains "$MARKER_END" "$RC_FILE" "end marker present" || return 1
  assert_file_contains "claude-sutando\(\)" "$RC_FILE" "function definition present" || return 1
}

# ----------------------------------------------------------------------
# 6. --commit on rc with stale managed block → in-place rewrite, exit 0
# ----------------------------------------------------------------------
test_commit_rewrite_drift() {
  # Seed with a stale block + an UNRELATED line that must survive the rewrite.
  echo "# Some unrelated line before" > "$RC_FILE"
  {
    echo "$MARKER_BEGIN"
    echo "claude-sutando() { echo 'old stale body'; }"
    echo "$MARKER_END"
    echo "# Some unrelated line after"
  } >> "$RC_FILE"

  bash "$HELPER" --commit >/dev/null 2>&1; rc=$?
  assert_eq "$rc" "0" "exit code" || return 1

  # Markers still present, exactly once.
  count="$(grep -cF "$MARKER_BEGIN" "$RC_FILE")"
  assert_eq "$count" "1" "exactly one begin marker" || return 1

  # Stale body gone, new body present.
  assert_file_not_contains "old stale body" "$RC_FILE" "stale body removed" || return 1
  assert_file_contains "rev-parse" "$RC_FILE" "new body uses rev-parse" || return 1

  # Unrelated lines preserved.
  assert_file_contains "Some unrelated line before" "$RC_FILE" "unrelated before preserved" || return 1
  assert_file_contains "Some unrelated line after" "$RC_FILE" "unrelated after preserved" || return 1
}

# ----------------------------------------------------------------------
# 7. --commit on rc with legacy alias → removes alias, appends managed block
# ----------------------------------------------------------------------
test_commit_migrate_legacy_alias() {
  {
    echo "# preexisting content"
    echo "alias claude-sutando='CLAUDE_CONFIG_DIR=/some/old/path claude'"
    echo "# trailing content"
  } > "$RC_FILE"

  bash "$HELPER" --commit >/dev/null 2>&1; rc=$?
  assert_eq "$rc" "0" "exit code" || return 1

  # Legacy alias gone.
  assert_file_not_contains "^alias claude-sutando=" "$RC_FILE" "legacy alias removed" || return 1

  # Managed block present.
  assert_file_contains "$MARKER_BEGIN" "$RC_FILE" "managed block begin present" || return 1
  assert_file_contains "claude-sutando\(\)" "$RC_FILE" "function definition present" || return 1

  # Other content preserved.
  assert_file_contains "preexisting content" "$RC_FILE" "preexisting preserved" || return 1
  assert_file_contains "trailing content" "$RC_FILE" "trailing preserved" || return 1
}

# ----------------------------------------------------------------------
# 8. --commit twice → second run is no-op
# ----------------------------------------------------------------------
test_commit_idempotent() {
  bash "$HELPER" --commit >/dev/null 2>&1
  size_after_first="$(wc -c < "$RC_FILE")"
  out2="$(bash "$HELPER" --commit 2>&1)"; rc=$?
  size_after_second="$(wc -c < "$RC_FILE")"

  assert_eq "$rc" "0" "second exit code" || return 1
  assert_eq "$size_after_first" "$size_after_second" "rc file size unchanged" || return 1
  assert_grep "no-op" "$out2" "second run reports no-op" || return 1
}

# ----------------------------------------------------------------------
# 9. --migrate with source ~/.claude → rsyncs to <ccd>, source unchanged
# ----------------------------------------------------------------------
test_migrate_copies_state() {
  # Seed ~/.claude with FOUR project slug variants to exercise the filesystem-
  # disambiguating filter end to end:
  #   (a) THIS project's exact slug                       → should migrate
  #   (b) subdir variant whose decoded path EXISTS in repo → should migrate
  #   (c) sibling-prefix whose decoded path doesn't exist  → should NOT migrate
  #   (d) totally unrelated project                        → should NOT migrate
  #
  # `workspace` is a real subdir of this repo (the M0 default), so the
  # decoded path /<repo>/workspace exists → (b) is a true subdir. `plus` is
  # NOT a subdir of this repo, so the decoded path doesn't exist → (c) is
  # correctly recognized as a sibling repo and excluded.
  this_slug="$(printf '%s' "$REPO" | tr '/' '-')"
  subdir_slug="${this_slug}-workspace"
  sibling_slug="${this_slug}-plus"
  unrelated_slug="-Users-someone-elses-project"
  mkdir -p "$HOME/.claude/projects/${this_slug}"
  mkdir -p "$HOME/.claude/projects/${subdir_slug}"
  mkdir -p "$HOME/.claude/projects/${sibling_slug}"
  mkdir -p "$HOME/.claude/projects/${unrelated_slug}"
  echo "this session"      > "$HOME/.claude/projects/${this_slug}/session.jsonl"
  echo "subdir session"    > "$HOME/.claude/projects/${subdir_slug}/session.jsonl"
  echo "sibling session"   > "$HOME/.claude/projects/${sibling_slug}/session.jsonl"
  echo "unrelated session" > "$HOME/.claude/projects/${unrelated_slug}/session.jsonl"
  echo '{"k":"v"}' > "$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude/skills/my-skill"
  echo "skill content" > "$HOME/.claude/skills/my-skill/SKILL.md"

  # Resolve target dir up front (where rsync should land). Discard stderr —
  # the loader's legacy-env-var warn would otherwise pollute the captured path.
  ccd="$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"

  # Run --migrate non-interactively (no tty → script auto-confirms).
  bash "$HELPER" --migrate </dev/null >/dev/null 2>&1; rc=$?
  assert_eq "$rc" "0" "exit code" || return 1

  # MUST migrate: THIS project's exact slug.
  [ -f "$ccd/projects/${this_slug}/session.jsonl" ] \
    || { echo "  FAIL: missing migrated session at $ccd/projects/${this_slug}/"; return 1; }

  # MUST migrate: subdir variant (decoded path /<repo>/workspace exists).
  [ -f "$ccd/projects/${subdir_slug}/session.jsonl" ] \
    || { echo "  FAIL: true subdir variant ${subdir_slug} NOT migrated (fs check broken)"; return 1; }

  # MUST NOT migrate: sibling-prefix (decoded path /<repo>/plus doesn't exist).
  if [ -e "$ccd/projects/${sibling_slug}" ]; then
    echo "  FAIL: sibling-with-prefix ${sibling_slug} migrated despite no /<repo>/plus dir — fs disambiguation broken"
    return 1
  fi

  # MUST NOT migrate: unrelated cwd.
  if [ -e "$ccd/projects/${unrelated_slug}" ]; then
    echo "  FAIL: unrelated project leaked into target"
    return 1
  fi

  # Non-projects stuff migrates regardless.
  [ -f "$ccd/settings.json" ] \
    || { echo "  FAIL: missing migrated settings.json"; return 1; }
  [ -f "$ccd/skills/my-skill/SKILL.md" ] \
    || { echo "  FAIL: missing migrated skill"; return 1; }

  # Source unchanged (non-destructive).
  [ -f "$HOME/.claude/projects/${this_slug}/session.jsonl" ] \
    || { echo "  FAIL: source this-slug session unexpectedly deleted"; return 1; }
  [ -f "$HOME/.claude/projects/${sibling_slug}/session.jsonl" ] \
    || { echo "  FAIL: source sibling-slug session unexpectedly deleted"; return 1; }
}

# ----------------------------------------------------------------------
# 10. --migrate with source missing → exit 1, stderr explains
# ----------------------------------------------------------------------
test_migrate_no_source() {
  # No ~/.claude exists (fresh HOME from setup_test).
  out="$(bash "$HELPER" --migrate 2>&1)"; rc=$?
  assert_eq "$rc" "1" "exit code" || return 1
  assert_grep "doesn't exist" "$out" "stderr says doesn't exist" || return 1
}

# ----------------------------------------------------------------------
# 11. dry-run (default) shows proposed block + target rc
# ----------------------------------------------------------------------
test_dryrun_shows_proposed_block() {
  out="$(bash "$HELPER" 2>&1)"; rc=$?
  assert_eq "$rc" "0" "exit code" || return 1
  assert_grep "Target rc file" "$out" "stdout names target rc" || return 1
  assert_grep "managed block" "$out" "stdout mentions managed block" || return 1
  assert_grep "claude-sutando\(\)" "$out" "stdout shows function definition" || return 1
}

# ----------------------------------------------------------------------
# Run them all
# ----------------------------------------------------------------------
echo "tests/sutando-shell-setup.test.sh — running"
echo

run_test "1.  --check on empty rc → absent, exit 1"            test_check_empty
run_test "2.  --check on legacy alias → legacy, exit 1"        test_check_legacy_alias
run_test "3.  --check on current block → ok, exit 0"           test_check_current_block
run_test "4.  --check on stale block → drift, exit 1"          test_check_drift_block
run_test "5.  --commit on empty rc → clean append"             test_commit_clean_append
run_test "6.  --commit on stale block → in-place rewrite"      test_commit_rewrite_drift
run_test "7.  --commit on legacy alias → migrated to block"    test_commit_migrate_legacy_alias
run_test "8.  --commit twice → no-op"                          test_commit_idempotent
run_test "9.  --migrate copies ~/.claude → ccd, source kept"   test_migrate_copies_state
run_test "10. --migrate with no source → exit 1"               test_migrate_no_source
run_test "11. dry-run shows proposed block + target rc"        test_dryrun_shows_proposed_block

finalize_and_exit
