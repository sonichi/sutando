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
  # Cross-platform inode equality: stat -f %i (macOS BSD) / -c %i (Linux GNU).
  # Use -L on both so symlinks are followed to their target's inode (BSD stat's
  # default is lstat-semantics; -L flips it to stat-semantics, matching GNU).
  # This is the symlink-equivalent case the B1 guard needs to detect.
  local a b
  a=$(stat -L -f %i "$1" 2>/dev/null || stat -L -c %i "$1" 2>/dev/null)
  b=$(stat -L -f %i "$2" 2>/dev/null || stat -L -c %i "$2" 2>/dev/null)
  [ -n "$a" ] && [ -n "$b" ] && [ "$a" = "$b" ]
}

_is_unsafe_for_migration() {
  # Deny-list for auto-migration's rm -rf target. Anything on this list →
  # refuse the destructive step entirely (split state is safer than data loss).
  # Per PR #1440 review B3 (Mini): a malformed $SUTANDO_WORKSPACE pointing at
  # /, $HOME, repo root, or a path with surviving `..` after normalization
  # would otherwise be compressed-and-deleted on a "successful" migration.
  local p="$1"
  local real
  real="$(_realpath "$p")"
  [ -z "$real" ] && return 0  # cannot resolve → unsafe
  case "$real" in
    /|/usr|/usr/*|/etc|/etc/*|/var|/var/*|/bin|/bin/*|/sbin|/sbin/*|/System|/System/*|/Library|/Library/*|/Applications|/Applications/*)
      return 0 ;;
    # macOS: /etc, /var, /tmp are symlinks into /private/<x>; realpath resolves
    # there. Include the resolved forms so the deny matches either spelling.
    /private/etc|/private/etc/*|/private/var|/private/var/*)
      return 0 ;;
    "$HOME"|"$HOME/Documents"|"$HOME/Desktop"|"$HOME/Downloads")
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
