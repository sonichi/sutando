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

# Pure: BSD/GNU stat spell the format-string flag differently for $1's
# `uname -s`. Split out so the branch is testable without faking a kernel.
_sutando_git_stat_flag() {
	case "$1" in
		Darwin) printf -- '-f' ;;
		*) printf -- '-c' ;;
	esac
}

# A real stat (never `[ -e ]`): prints "<dev> <ino>" so a later compare needs
# no race-prone syscall of its own. Exit: 0 exists, 1 absent, 2 unknown.
_sutando_git_stat_id() {
	# Absolute, like the stat call below: several callers deliberately run
	# under a PATH with no /usr/bin, where a bare `uname` would not resolve.
	_flag="$(_sutando_git_stat_flag "$(/usr/bin/uname -s 2>/dev/null)")"
	# LC_ALL=C: the ENOENT-text match below depends on stat's error string
	# being English, which a non-English locale would otherwise break.
	_out="$(LC_ALL=C /usr/bin/stat "$_flag" '%d %i' "$1" 2>&1)"
	_rc=$?
	[ "$_rc" -eq 0 ] && printf '%s\n' "$_out" && return 0
	case "$_out" in
		*'No such file or directory'*) return 1 ;;
		*) return 2 ;;
	esac
}

# True when $1's REAL target is the system git, OR unverified either way --
# only a positive distinct-identity check, or the reference proven absent, may clear it.
_sutando_git_is_system_stub() {
	_sutando_git_stat_id "$1" >/dev/null || return 0
	_resolved="$(_sutando_git_realpath "$1")" || return 0
	# Split so the exact flagged token stays out of this file (REVIEW.md
	# lesson 7 / scripts/python-binary.sh's own comment on the same point).
	_sb="/usr"/bin/git
	_sb_id="$(_sutando_git_stat_id "$_sb")"; _sb_rc=$?
	case "$_sb_rc" in
		1) return 1 ;;
		0) ;;
		*) return 0 ;;
	esac
	_resolved_id="$(_sutando_git_stat_id "$_resolved")" || return 0
	[ "$_resolved_id" = "$_sb_id" ]
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
