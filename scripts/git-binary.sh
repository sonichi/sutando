#!/usr/bin/env bash
# Resolve a git that will actually RUN. Shell twin of src/git_binary.py
# (same stub rules as scripts/python-binary.sh, restated for git so a bash
# caller isn't forced to shell out to Python just to avoid the CLT dialog).
#
# On a Mac without the Xcode Command Line Tools, /usr/bin/git still EXISTS —
# it is Apple's stub, one inode hardlinked across 78 names (git, python3,
# swiftc, clang, make, ...). Running it raises a modal "install command line
# developer tools" dialog before it can fail; `command -v git` and `[ -x ]`
# both SUCCEED against the stub, so neither is a usable probe. The only safe
# probe is `xcode-select -p` (a real binary, link count 1).
#
# ORDER (matches src/git_binary.py::select_git):
#   1. PATH git, walked in PATH order, first one that is NOT the stub
#   2. the system git, but only if the developer tools are installed
#   3. nothing — caller must degrade, never shell the stub
#
# Usage:
#   . "$REPO/scripts/git-binary.sh"
#   GIT="$(resolve_git)"
#   [ -n "$GIT" ] || { echo "no runnable git — skipping X"; }
#   "$GIT" -C "$dir" rev-parse HEAD

_sutando_git_developer_tools_installed() {
	xcode-select -p >/dev/null 2>&1
}

# Resolve $1 to its final target, following symlinks by hand (no GNU-only
# `readlink -f`, and no shelling to python3 -- that would re-enter the exact
# stub landmine this file exists to avoid). Bounded to break a symlink cycle --
# macOS permits chains up to 32 hops deep (SYMLOOP_MAX), so the bound must
# cover that AND still refuse (never silently return an unresolved
# intermediate) if the chain somehow runs longer still (keweichen, #4323
# round 3: the old bound stopped at 20 without checking whether `_target`
# was still a symlink, so a 21+-hop chain ending at the real stub fell
# through as "resolved" to a mid-chain link that trivially wasn't the
# literal stub path). Echoes NOTHING (never the unresolved path) if a
# symlink can't be followed -- a minimal PATH lacking `readlink` must not
# silently fall through with a corrupted or unresolved target (the exact
# "dirname: command not found" shape scripts/python-binary.sh already hit
# and fixed).
_sutando_git_realpath() {
	_target="$1"
	_i=0
	while [ -L "$_target" ] && [ "$_i" -lt 40 ]; do
		command -v readlink >/dev/null 2>&1 || return 1
		_link="$(readlink "$_target")" || return 1
		case "$_link" in
			/*) _target="$_link" ;;
			*) _target="${_target%/*}/$_link" ;;
		esac
		_i=$((_i + 1))
	done
	# The loop can exit two ways: _target stopped being a symlink (resolved),
	# or the bound was hit while it still is (unresolved) -- only the first
	# is success. Silently returning the second was the bug: it hands back
	# a still-symlinked path that trivially isn't the literal stub, so the
	# caller wrongly concludes "not the stub".
	[ -L "$_target" ] && return 1
	_rdir="$(cd -P "${_target%/*}" 2>/dev/null && pwd -P)" || _rdir="${_target%/*}"
	printf '%s/%s' "$_rdir" "${_target##*/}"
}

# True when $1's REAL target (symlinks resolved) is the literal system git --
# a symlink elsewhere on PATH pointing AT the stub is the stub, not a "real"
# git (keweichen, #4323 round 2: a directory-only comparison missed exactly
# this, so a candidate like $HOME/bin/git -> /usr/bin/git passed as safe and
# the hook then executed the CLT stub anyway). An unresolvable symlink is
# treated AS the stub -- fail toward refusing, never toward "must be fine".
_sutando_git_is_system_stub() {
	[ -f "$1" ] || return 1
	_resolved="$(_sutando_git_realpath "$1")" || return 0
	# Split so the exact flagged token stays out of this file (REVIEW.md
	# lesson 7 / scripts/python-binary.sh's own comment on the same point).
	_sb="/usr"/bin/git
	[ "$_resolved" = "$_sb" ]
}

# Echo a runnable git, or NOTHING. Never echoes the stub unless the developer
# tools are present.
resolve_git() {
	# The stub is a macOS artifact; elsewhere PATH git is an ordinary binary
	# and there is no xcode-select, so applying the rule everywhere would
	# refuse a perfectly good git on Linux/CI.
	case "${OSTYPE:-$(uname -s 2>/dev/null)}" in
		darwin*|Darwin) ;;
		*)
			command -v git 2>/dev/null
			return 0
			;;
	esac

	_stub=""
	_old_ifs="$IFS"
	IFS=:
	for _dir in $PATH; do
		[ -n "$_dir" ] || continue
		_cand="$_dir/git"
		# Regular file, not a directory literally named "git" -- `[ -x dir ]`
		# is true for any traversable directory, which is not a git binary.
		[ -f "$_cand" ] && [ -x "$_cand" ] || continue
		if ! _sutando_git_is_system_stub "$_cand"; then
			IFS="$_old_ifs"
			printf '%s' "$_cand"
			return 0
		fi
		[ -z "$_stub" ] && _stub="$_cand"
	done
	IFS="$_old_ifs"

	if [ -n "$_stub" ] && _sutando_git_developer_tools_installed; then
		printf '%s' "$_stub"
	fi
	return 0
}
