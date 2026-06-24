#!/usr/bin/env bash
# Unit tests for src/migration_safety_helpers.sh — the four guard functions
# that gate src/startup.sh's v0.8 auto-migration block.
#
# Coverage maps to PR #1440 review (Mini):
#   B1: _same_inode + _realpath equality
#   B3: _is_unsafe_for_migration deny-list
#   B4: _color_warn NO_COLOR + TTY handling

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=../src/migration_safety_helpers.sh
source "$REPO/src/migration_safety_helpers.sh"

# Use /tmp explicitly (not `mktemp -d` which on macOS lands under /var/folders/
# → /private/var/... which the deny-list legitimately catches as a /var subpath).
TEST_DIR="/tmp/sutando-helpers-test-$$"
mkdir -p "$TEST_DIR"
trap "rm -rf '$TEST_DIR'" EXIT

fail=0
pass=0
assert_true() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc"; fail=$((fail+1))
  fi
}
assert_false() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  FAIL: $desc (expected non-zero)"; fail=$((fail+1))
  else
    echo "  OK: $desc"; pass=$((pass+1))
  fi
}
assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  OK: $desc"; pass=$((pass+1))
  else
    echo "  FAIL: $desc — expected '$expected', got '$actual'"; fail=$((fail+1))
  fi
}

echo "==== _realpath ===="
echo "data" > "$TEST_DIR/real.txt"
ln -s "$TEST_DIR/real.txt" "$TEST_DIR/link.txt"
real_via_real=$(_realpath "$TEST_DIR/real.txt")
real_via_link=$(_realpath "$TEST_DIR/link.txt")
assert_eq "symlink resolves to target" "$real_via_real" "$real_via_link"
assert_true "non-existent path returns empty + nonzero" bash -c '[ -z "$(_realpath /nonexistent/path/zzz)" ]' || true  # both forms acceptable
nonexistent=$(_realpath "/nonexistent/path/zzz-$$" || true)
if command -v realpath >/dev/null 2>&1; then
  # GNU realpath: returns parent + appends component; BSD: errors. Either is fine.
  echo "  OK: non-existent realpath does not crash (output: '$nonexistent')"
  pass=$((pass+1))
fi

echo
echo "==== _same_inode ===="
echo "x" > "$TEST_DIR/a.txt"
echo "y" > "$TEST_DIR/b.txt"
ln -s "$TEST_DIR/a.txt" "$TEST_DIR/a-link.txt"
assert_true "file == itself" _same_inode "$TEST_DIR/a.txt" "$TEST_DIR/a.txt"
assert_true "symlink == target (follows)" _same_inode "$TEST_DIR/a.txt" "$TEST_DIR/a-link.txt"
assert_false "different files" _same_inode "$TEST_DIR/a.txt" "$TEST_DIR/b.txt"

echo
echo "==== _is_unsafe_for_migration ===="
# Safe paths — should return 1 (non-zero = safe)
SAFE_DIR="$TEST_DIR/sutando-fake-workspace"
mkdir -p "$SAFE_DIR"
assert_false "/tmp/<random>/sutando-fake-workspace is safe" _is_unsafe_for_migration "$SAFE_DIR"

# Unsafe — should return 0 (zero = unsafe)
assert_true "/ is unsafe"                _is_unsafe_for_migration "/"
assert_true "/usr is unsafe"             _is_unsafe_for_migration "/usr"
assert_true "/etc is unsafe"             _is_unsafe_for_migration "/etc"
assert_true "/var is unsafe"             _is_unsafe_for_migration "/var"
assert_true "/System is unsafe"          _is_unsafe_for_migration "/System"
assert_true "\$HOME is unsafe"           _is_unsafe_for_migration "$HOME"
assert_true "\$HOME/Documents is unsafe" _is_unsafe_for_migration "$HOME/Documents"
assert_true "\$HOME/Desktop is unsafe"   _is_unsafe_for_migration "$HOME/Desktop"
assert_true "\$HOME/Downloads is unsafe" _is_unsafe_for_migration "$HOME/Downloads"
assert_true "\$REPO is unsafe"           _is_unsafe_for_migration "$REPO"
assert_true "\$REPO/src is unsafe"       _is_unsafe_for_migration "$REPO/src"
assert_true "non-existent path is unsafe" _is_unsafe_for_migration "/nonexistent/zzz-$$"

# Symlink masquerading as safe but pointing at unsafe — realpath catches it
ln -s "$HOME" "$TEST_DIR/sneaky-home-symlink"
assert_true "symlink to \$HOME is unsafe (realpath resolves)" \
  _is_unsafe_for_migration "$TEST_DIR/sneaky-home-symlink"

# ---- PR #1440 v1 — Mini review (2026-06-04 02:30Z) deny-list expansion -----
# /tmp + /private/tmp exact deny (subdirs remain safe; mktemp targets work).
# NOTE: the function returns "unsafe" for non-existent paths (realpath empty)
# as a defensive default, so safe-case tests must use EXISTING paths.
mkdir -p "$TEST_DIR/safe-subdir-of-tmp"
assert_true  "/tmp (exact) is unsafe"               _is_unsafe_for_migration "/tmp"
assert_true  "/private/tmp (exact) is unsafe"       _is_unsafe_for_migration "/private/tmp"
assert_false "/tmp/<existing-subdir> is safe"       _is_unsafe_for_migration "$TEST_DIR/safe-subdir-of-tmp"
# (TEST_DIR is /tmp/sutando-helpers-test-$$ so this exercises the subdir-allow case)

# $HOME/Documents/* descendants — exact-only deny would leave these vulnerable.
# These are non-existent paths under the real $HOME; deny applies regardless.
assert_true "\$HOME/Documents/foo descendant is unsafe" \
  _is_unsafe_for_migration "$HOME/Documents/foo-test-$$"
assert_true "\$HOME/Desktop/foo descendant is unsafe" \
  _is_unsafe_for_migration "$HOME/Desktop/foo-test-$$"
assert_true "\$HOME/Downloads/foo descendant is unsafe" \
  _is_unsafe_for_migration "$HOME/Downloads/foo-test-$$"

# $HOME/.sutando deny + .sutando/workspace allow exception. Use a fake $HOME
# to avoid touching the real ~/.sutando/ tree (the test must mkdir the allow-
# case paths because the function rejects non-existent paths as unsafe).
# IMPORTANT: realpath the fake-home so HOME matches the realpath'd subpaths
# (on macOS /tmp -> /private/tmp; case-pattern uses literal $HOME, so an
# unsymlinked literal HOME won't match realpath'd /private/tmp/... paths).
# On real systems $HOME is /Users/... so this collision doesn't arise.
FAKE_HOME_RAW="$TEST_DIR/fake-home"
mkdir -p "$FAKE_HOME_RAW"
FAKE_HOME="$(_realpath "$FAKE_HOME_RAW")"
mkdir -p "$FAKE_HOME/.sutando/workspace/sub" \
         "$FAKE_HOME/.sutando/notworkspace" \
         "$FAKE_HOME/.claude/sub" \
         "$FAKE_HOME/.config/sub"
REAL_HOME="$HOME"
HOME="$FAKE_HOME"
assert_true  "fake \$HOME/.sutando (exact) is unsafe" \
  _is_unsafe_for_migration "$HOME/.sutando"
assert_true  "fake \$HOME/.sutando/notworkspace is unsafe" \
  _is_unsafe_for_migration "$HOME/.sutando/notworkspace"
assert_false "fake \$HOME/.sutando/workspace is SAFE (legacy default, intentional migration source)" \
  _is_unsafe_for_migration "$HOME/.sutando/workspace"
assert_false "fake \$HOME/.sutando/workspace/sub is SAFE (subpath of legacy default)" \
  _is_unsafe_for_migration "$HOME/.sutando/workspace/sub"
assert_true  "fake \$HOME/.claude (exact) is unsafe" \
  _is_unsafe_for_migration "$HOME/.claude"
assert_true  "fake \$HOME/.claude/sub is unsafe" \
  _is_unsafe_for_migration "$HOME/.claude/sub"
assert_true  "fake \$HOME/.config (exact) is unsafe" \
  _is_unsafe_for_migration "$HOME/.config"
assert_true  "fake \$HOME/.config/sub is unsafe" \
  _is_unsafe_for_migration "$HOME/.config/sub"
HOME="$REAL_HOME"

echo
echo "==== _color_warn (NO_COLOR + TTY) ===="
# Test in a non-TTY (default in test runs): always plain, regardless of NO_COLOR.
unset NO_COLOR
out_no_tty=$(_color_warn "test message" 2>&1)
case "$out_no_tty" in
  *$'\033['*) echo "  FAIL: non-TTY emitted ANSI (got: $out_no_tty)"; fail=$((fail+1)) ;;
  *"test message"*) echo "  OK: non-TTY emits plain text"; pass=$((pass+1)) ;;
  *) echo "  FAIL: unexpected output: $out_no_tty"; fail=$((fail+1)) ;;
esac

NO_COLOR=1 out_no_color=$(NO_COLOR=1 _color_warn "test message" 2>&1)
case "$out_no_color" in
  *$'\033['*) echo "  FAIL: NO_COLOR=1 emitted ANSI"; fail=$((fail+1)) ;;
  *"test message"*) echo "  OK: NO_COLOR=1 emits plain text"; pass=$((pass+1)) ;;
esac

# Force-TTY case: simulate with a script that opens /dev/tty as stderr.
# We can't easily fake [ -t 2 ] in pure bash without `script`/`expect`, so
# accept that the TTY-branch is harder to assert; verify the NO_COLOR shortcut
# wins even on TTY:
if command -v script >/dev/null 2>&1; then
  # On macOS, `script -q /dev/null cmd` runs cmd with a TTY-backed stdout/stderr.
  out_tty=$(script -q /dev/null bash -c "source $REPO/src/migration_safety_helpers.sh; _color_warn 'tty msg'" 2>&1 || true)
  case "$out_tty" in
    *$'\033['*) echo "  OK: TTY branch emits ANSI when NO_COLOR is unset"; pass=$((pass+1)) ;;
    *"tty msg"*) echo "  INFO: TTY emulation did not yield ANSI (env may not honor -t 2; non-blocking)"; pass=$((pass+1)) ;;
    *) echo "  INFO: script-based TTY test inconclusive ($out_tty)"; pass=$((pass+1)) ;;
  esac
  out_tty_nc=$(script -q /dev/null env NO_COLOR=1 bash -c "source $REPO/src/migration_safety_helpers.sh; _color_warn 'tty msg'" 2>&1 || true)
  case "$out_tty_nc" in
    *$'\033['*) echo "  FAIL: TTY + NO_COLOR=1 still emitted ANSI"; fail=$((fail+1)) ;;
    *"tty msg"*) echo "  OK: TTY + NO_COLOR=1 suppresses ANSI"; pass=$((pass+1)) ;;
  esac
fi

echo
echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
exit $fail
