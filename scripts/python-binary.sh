#!/usr/bin/env bash
# Resolve a python3 that will actually RUN. Shell twin of src/python-binary.ts
# and src/git_binary.py.
#
# WHY THIS EXISTS
# ---------------
# On a Mac without the Xcode Command Line Tools, `/usr/bin/python3` still
# EXISTS — it is Apple's stub, one inode hardlinked across 78 names (python3,
# git, swiftc, clang, make, ...). Verify on any Mac with:
#
#     ls -li /usr/bin | awk '$3==78'
#
# Executing it raises a modal "The python3 command requires the command line
# developer tools" dialog BEFORE it can fail, so `2>/dev/null` cannot suppress
# it and checking the exit code is already too late. `command -v python3` and
# `[ -x ]` both SUCCEED against the stub, so neither is a usable probe.
#
# The only safe probe is `xcode-select -p`: /usr/bin/xcode-select is a real
# binary (link count 1), so asking it never raises the dialog. Same check
# src/migrate.sh:122 already uses before its Swift build steps.
#
# Measured on a clean macOS 26.5 VM (no CLT), Sutando 0.5.0-rc.2: launching the
# app produced repeated stub executions from `startup.sh` — dashboard.py and
# agent-api.py are respawned on a backoff, so each retry re-raised the dialog.
#
# ORDER (matches scripts/sutando-config.sh and src/agent/claude/cli/start-cli.sh):
#   1. $SUTANDO_PY            — explicit override, set by the desktop launcher
#   2. bundled relocatable    — <engine>/runtime/python/bin/python3
#   3. PATH python3           — but ONLY if it is not the stub
#   4. nothing                — caller must SKIP, never shell the stub
#
# Usage:
#   . "$REPO/scripts/python-binary.sh"
#   PY="$(resolve_python "$REPO")"
#   [ -n "$PY" ] || { echo "no runnable python3 — skipping X"; }
#   "$PY" script.py

# True when the developer tools are installed, i.e. the system python3 is a real
# interpreter rather than the stub.
_sutando_developer_tools_installed() {
	xcode-select -p >/dev/null 2>&1
}

# True when $1 lives in the system bin directory. Compared by DIRECTORY rather
# than against the full stub path so the exact flagged token stays out of this
# file (REVIEW.md lesson 7), and so a versioned sibling in the same location is
# covered too.
_sutando_is_system_stub() {
	_sb="/usr"/bin
	# Parameter expansion, not `dirname`: dirname itself lives in /usr/bin, so a
	# caller with a minimal PATH made this probe fail — and it failed OPEN,
	# classifying the system stub as "not the stub" and returning it. Caught by
	# tests/python-binary-sh.test.sh emitting "dirname: command not found".
	_d="${1%/*}"
	[ "$_d" = "$1" ] && _d="."
	[ "$(cd "$_d" 2>/dev/null && pwd -P)" = "$_sb" ]
}

# Echo a runnable python3, or NOTHING. Never echoes the stub unless the
# developer tools are present. $1 = repo root.
resolve_python() {
	_repo="${1:-.}"

	if [ -n "${SUTANDO_PY:-}" ] && [ -x "${SUTANDO_PY}" ]; then
		printf '%s' "$SUTANDO_PY"
		return 0
	fi

	# The bundle vendors its interpreter beside the engine copy.
	if [ -x "$_repo/../runtime/python/bin/python3" ]; then
		printf '%s' "$_repo/../runtime/python/bin/python3"
		return 0
	fi

	_path_py="$(command -v python3 2>/dev/null)" || _path_py=""
	[ -n "$_path_py" ] || return 0

	# The stub is a macOS artifact. On Linux/BSD /usr/bin/python3 is an ordinary
	# interpreter and there is no xcode-select, so applying the rule everywhere
	# refused a perfectly good binary and left $PY empty — which is how CI broke
	# ("sutando-config.sh: line 56: : command not found"). src/git_binary.py
	# guards the same rule with `is_darwin`; this is that guard.
	#
	# $OSTYPE, not `uname`: the shell sets it at build time, so a PATH stub
	# cannot reach it. tests/codex-core-launcher.test.py:89 deliberately stubs
	# `uname` to print "Darwin" for the launcher's own macOS branch, which made
	# a uname-based check take the macOS path on the Linux runner, find no
	# xcode-select, and refuse a real interpreter — 21 failures. Platform
	# identity must not come from a PATH lookup. `uname` remains the fallback
	# for a POSIX-sh caller where $OSTYPE is unset.
	case "${OSTYPE:-$(uname -s 2>/dev/null)}" in
		darwin*|Darwin) _is_mac=1 ;;
		*) _is_mac=0 ;;
	esac
	if [ "$_is_mac" -ne 1 ]; then
		printf '%s' "$_path_py"
		return 0
	fi

	# Homebrew / python.org / pyenv — a real interpreter, use it as-is with no
	# toolchain requirement.
	if ! _sutando_is_system_stub "$_path_py"; then
		printf '%s' "$_path_py"
		return 0
	fi

	# It IS the system location. Only safe if the tools are actually installed.
	if _sutando_developer_tools_installed; then
		printf '%s' "$_path_py"
	fi
	return 0
}

# Echo a runnable python3 or FAIL LOUDLY, once, with a fix. For callers where
# Python is genuinely required (config resolution, the core launcher) — there
# the empty result must not silently become `"" -c ...`, which the shell reports
# as the useless `: command not found` and repeats per call site.
#
# $1 = repo root. $2 = what the caller was trying to do (used in the message).
require_python() {
	_rp="$(resolve_python "${1:-.}")"
	if [ -n "$_rp" ]; then
		printf '%s' "$_rp"
		return 0
	fi
	{
		printf 'sutando: no runnable python3 — cannot %s\n' "${2:-continue}"
		printf '  Tried: $SUTANDO_PY, %s/../runtime/python/bin/python3, then PATH.\n' "${1:-.}"
		printf '  On macOS a PATH python3 is only used when the developer tools are\n'
		printf '  installed, because the system python3 is otherwise the Xcode-CLT\n'
		printf '  stub (one inode hardlinked across 78 names, incl. git and swiftc)\n'
		printf '  and merely running it raises the install dialog.\n'
		printf '  Fix: install python3 (brew install python), or set SUTANDO_PY to an\n'
		printf '  interpreter, or run xcode-select --install.\n'
	} >&2
	return 1
}
