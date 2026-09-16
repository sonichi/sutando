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

# True when $1 lives in the system bin directory — by DIRECTORY, not the full
# stub path, so the exact flagged token stays out of this file (REVIEW.md
# lesson 7) and a versioned sibling in the same location is covered too.
_sutando_git_is_system_stub() {
	_sb="/usr"/bin
	_d="${1%/*}"
	[ "$_d" = "$1" ] && _d="."
	[ "$(cd "$_d" 2>/dev/null && pwd -P)" = "$_sb" ]
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
		[ -x "$_cand" ] || continue
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
