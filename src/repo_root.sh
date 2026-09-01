#!/usr/bin/env bash
# Resolve the DURABLE repo that supplies the running `src/` code.
#
# `cd "$dir/.."` applies the `/..` as a path string BEFORE the cd, so a
# symlinked `bundle/repo/src -> durable/repo/src` is never traversed and the
# answer is the bundle wrapper — which does not own `.env`. Resolve the `src/`
# directory physically first, then go up.

# Normalize a repo candidate to the checkout that physically supplies its src/.
# A packaged bundle whose src/ symlinks into a durable checkout normalizes to
# that checkout; a normal checkout resolves to itself.
sutando_repo_root_from_src() {
  local src_dir="$1"
  [ -n "$src_dir" ] || return 1
  [ -d "$src_dir" ] || return 1
  ( cd -P "$src_dir" >/dev/null 2>&1 && cd -P .. >/dev/null 2>&1 && pwd -P ) || return 1
}

# The durable repo root. Order: an explicit candidate (normalized through its
# own src/, so an explicit cross-checkout selection is preserved but a symlinked
# bundle shell cannot own durable credentials), else this file's own location.
# Falls back to the lexical parent only when nothing physical resolves, so a
# caller always receives a path.
sutando_repo_root() {
  local candidate="${1:-${REPO:-}}" resolved
  if [ -n "$candidate" ]; then
    resolved="$(sutando_repo_root_from_src "$candidate/src")" && { printf '%s\n' "$resolved"; return 0; }
    printf '%s\n' "$candidate"
    return 0
  fi
  resolved="$(sutando_repo_root_from_src "$(dirname "${BASH_SOURCE[0]}")")" \
    && { printf '%s\n' "$resolved"; return 0; }
  ( cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd )
}
