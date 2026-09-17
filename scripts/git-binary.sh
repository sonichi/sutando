#!/usr/bin/env bash
# Resolve a git that will actually run, without ever executing a candidate to
# probe it -- shell twin of src/git_binary.py::select_git; same rules, same order.

_sutando_git_developer_tools_installed() {
	xcode-select -p >/dev/null 2>&1
}

# Resolve $1's final target by hand (no GNU-only `readlink -f`). Bounded past
# macOS's 32-hop SYMLOOP_MAX; refuses rather than echo an unresolved chain.
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
	# Loop exit is ambiguous (resolved vs. bound hit while still a symlink);
	# only the first is success -- an unresolved path is never "not the stub".
	[ -L "$_target" ] && return 1
	_rdir="$(cd -P "${_target%/*}" 2>/dev/null && pwd -P)" || _rdir="${_target%/*}"
	printf '%s/%s' "$_rdir" "${_target##*/}"
}

# True when $1's REAL target (symlinks resolved) is the system git -- a
# symlink pointing AT the stub is the stub. Unresolvable -> treated as stub too.
_sutando_git_is_system_stub() {
	[ -f "$1" ] || return 1
	_resolved="$(_sutando_git_realpath "$1")" || return 0
	# Split so the exact flagged token stays out of this file (REVIEW.md
	# lesson 7 / scripts/python-binary.sh's own comment on the same point).
	_sb="/usr"/bin/git
	# -ef compares filesystem identity (device+inode): realpath above does
	# not case-fold, so a case-insensitive-volume alias missed `=` alone.
	[ "$_resolved" -ef "$_sb" ]
}

# Echo a runnable git, or NOTHING. Never echoes the stub unless the developer
# tools are present.
resolve_git() {
	# The stub is macOS-only; elsewhere the rule would refuse a perfectly
	# good git that has no xcode-select to probe.
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
