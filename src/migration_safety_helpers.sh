# PR #1440 — auto-migration safety helpers (Mini review).
#
# Sourced by src/startup.sh's v0.8 SUTANDO_WORKSPACE auto-migration block and
# by tests/startup-migration.integration.test.sh. Pulled into a standalone
# sourceable so the four guard functions can be unit-tested without driving
# the entire startup sequence.
#
# Functions defined:
#   _realpath <path>                  — cross-platform realpath
#   _same_inode <a> <b>               — inode-equality predicate (BSD + GNU stat)
#   _is_unsafe_for_migration <path>   — deny-list for rm -rf targets
#   _color_warn <message>             — bold-red stderr banner (NO_COLOR-aware)
#
# Caller-supplied env required by `_is_unsafe_for_migration`:
#   $REPO   — absolute path to the sutando repo root (denied + denied-as-prefix)
#   $HOME   — set by every shell; used to deny $HOME and top-level subdirs
#
# Each function returns 0 / non-zero by shell convention; messages go to stderr.

_realpath() {
  if command -v realpath >/dev/null 2>&1; then
    realpath "$1" 2>/dev/null
  elif command -v readlink >/dev/null 2>&1 && readlink -f / >/dev/null 2>&1; then
    readlink -f "$1" 2>/dev/null
  else
    python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "$1" 2>/dev/null
  fi
}

_same_inode() {
  # Cross-platform device:inode equality: stat -f '%d:%i' (macOS BSD) /
  # -c '%d:%i' (Linux GNU). Use -L on both so symlinks are followed to their
  # target (BSD stat's default is lstat-semantics; -L flips it to stat-
  # semantics, matching GNU). Per Mini's PR #1440 v1 review (2026-06-04
  # 02:30Z): comparing inode alone false-positives across unrelated file
  # systems that happen to reuse the same inode number — the device id is
  # what makes the pair globally unique.
  local a b
  a=$(stat -L -f '%d:%i' "$1" 2>/dev/null || stat -L -c '%d:%i' "$1" 2>/dev/null)
  b=$(stat -L -f '%d:%i' "$2" 2>/dev/null || stat -L -c '%d:%i' "$2" 2>/dev/null)
  [ -n "$a" ] && [ -n "$b" ] && [ "$a" = "$b" ]
}

_is_unsafe_for_migration() {
  # Deny-list for auto-migration's rm -rf target. Anything on this list →
  # refuse the destructive step entirely (split state is safer than data loss).
  # Per PR #1440 review B3 (Mini): a malformed $SUTANDO_WORKSPACE pointing at
  # /, $HOME, repo root, or a path with surviving `..` after normalization
  # would otherwise be compressed-and-deleted on a "successful" migration.
  #
  # Expanded per Mini's v1 review (2026-06-04 02:30Z):
  #   - /tmp and /private/tmp exact (subdirs like mktemp targets stay safe)
  #   - $HOME/Documents/*, $HOME/Desktop/*, $HOME/Downloads/* descendants
  #     (the prior pass only denied the exact dirs, leaving `~/Documents/foo`
  #     fair game — that's the user's code repos / personal docs)
  #   - $HOME/.sutando exact + subpaths, EXCEPT $HOME/.sutando/workspace
  #     (that subpath IS the known legacy auto-migration source — we want
  #     to be able to relocate it; everything else under .sutando is the
  #     installer's per-host state)
  #   - $HOME/.claude exact + subpaths
  #   - $HOME/.config exact + subpaths
  local p="$1"
  local real
  real="$(_realpath "$p")"
  [ -z "$real" ] && return 0  # cannot resolve → unsafe

  # Allow exception: the known legacy default (intended migration source).
  # Checked FIRST so the subsequent $HOME/.sutando deny doesn't shadow it.
  case "$real" in
    "$HOME/.sutando/workspace"|"$HOME/.sutando/workspace/"*)
      return 1 ;;  # explicitly safe — this is the auto-migration source
  esac

  case "$real" in
    /|/usr|/usr/*|/etc|/etc/*|/var|/var/*|/bin|/bin/*|/sbin|/sbin/*|/System|/System/*|/Library|/Library/*|/Applications|/Applications/*)
      return 0 ;;
    # macOS: /etc, /var, /tmp are symlinks into /private/<x>; realpath resolves
    # there. Include the resolved forms so the deny matches either spelling.
    /private/etc|/private/etc/*|/private/var|/private/var/*)
      return 0 ;;
    # Exact /tmp + /private/tmp deny (operator typo'd workspace path); mktemp
    # subdirs (`/tmp/foo`) remain safe targets.
    /tmp|/private/tmp)
      return 0 ;;
    "$HOME"|"$HOME/Documents"|"$HOME/Documents/"*|"$HOME/Desktop"|"$HOME/Desktop/"*|"$HOME/Downloads"|"$HOME/Downloads/"*)
      return 0 ;;
    # $HOME dotfile dirs — destructive for user installs of other tools, even
    # if SUTANDO_WORKSPACE was set there by mistake. .sutando/workspace already
    # excluded above.
    "$HOME/.sutando"|"$HOME/.sutando/"*)
      return 0 ;;
    "$HOME/.claude"|"$HOME/.claude/"*)
      return 0 ;;
    "$HOME/.config"|"$HOME/.config/"*)
      return 0 ;;
    "$REPO"|"$REPO/"*)
      return 0 ;;
  esac
  case "$real" in *..*) return 0;; esac
  return 1
}

_color_warn() {
  # Bold-red on TTY when NO_COLOR is unset; plain otherwise. Per PR #1440
  # review B4 (Mini): the prior `[ -t 2 ]` check didn't honor NO_COLOR.
  if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
    printf '\033[1;31m%s\033[0m\n' "$1" >&2
  else
    printf '%s\n' "$1" >&2
  fi
}
