#!/usr/bin/env bash
# The generator must capture what happened — stdout, stderr, exit code — and bind
# the block to a sha; dropping any of it launders a claim instead of proving one.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
GEN="$REPO/scripts/pr-evidence.sh"
fail=0

pass() { echo "PASS: $1"; }
flunk() { echo "FAIL: $1"; fail=1; }

has() {  # haystack needle label
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else flunk "$3"; fi
}
lacks() {
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else flunk "$3"; fi
}
same() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else flunk "$3"; fi
}
nonzero() {
    if [[ "$1" -ne 0 ]]; then pass "$2"; else flunk "$2"; fi
}

# Key on text the OS produces, never on the command string: the command is
# echoed into the block, so a self-written marker passes for the wrong reason.
out="$(bash "$GEN" 'echo alpha-marker' 'cat /definitely-absent-xyz' 2>/dev/null)"
sha="$(git -C "$REPO" rev-parse HEAD)"

has "$out" 'alpha-marker' "captures stdout of a passing command"
has "$out" 'No such file or directory' "captures STDERR — asserts on OS text, not on the echoed command"
has "$out" '[exit 1]' "records the ACTUAL nonzero exit, not just 'failed'"
has "$out" '[exit 0]' "records the zero exit too"
has "$out" '$ echo alpha-marker' "echoes each command it ran"
has "$out" "<!-- pr-evidence HEAD $sha" "stamps the block with the sha it was generated at"

# A block naming no sha cannot be told from a stale or hand-written one, and a
# stamp buried mid-block survives a partial paste that drops the evidence.
first="$(printf '%s' "$out" | head -1)"
case "$first" in
    "<!-- pr-evidence HEAD $sha"*) pass "stamp is the FIRST line, so a truncated paste loses it visibly" ;;
    *) flunk "stamp is the FIRST line (got: $first)" ;;
esac

# The sha names a COMMIT while commands run against the working TREE. A stamp
# that hides that asserts exact-commit provenance for output the commit lacks.
dirtymark="UNCOMMITTED_EVIDENCE_PROBE"
touch "$REPO/$dirtymark"
dout="$(bash "$GEN" "test -f $dirtymark && echo probe-present" 2>/dev/null)"
rm -f "$REPO/$dirtymark"
has "$dout" "$sha+dirty" "a DIRTY tree is stamped +dirty, not as the bare commit"
has "$dout" "does not" "and the block says so in prose, not only in the marker"
has "$dout" "probe-present" "the uncommitted content is still exercised and shown"

# A command that dirties the tree WHILE producing the stamped output is the case
# a before-only check cannot see.
mout="$(bash "$GEN" "touch $dirtymark" 'echo made-a-file' 2>/dev/null)"
rm -f "$REPO/$dirtymark"
has "$mout" "DURING the run" "a tree dirtied BY the commands is called out as mid-run"

# --at is the "before" half: it must run at the named ref, not at HEAD.
if ! git -C "$REPO" rev-parse --verify --quiet 'HEAD~1^{commit}' >/dev/null; then
    git -C "$REPO" fetch --deepen 2 --quiet >/dev/null 2>&1 || true
fi
if ! git -C "$REPO" rev-parse --verify --quiet 'HEAD~1^{commit}' >/dev/null; then
    echo "SKIP: shallow clone with no HEAD~1 — --at assertions need two commits"
    [[ "$fail" -ne 0 ]] && { echo "FAIL: pr-evidence generator"; exit 1; }
    echo "PASS: pr-evidence generator (--at block skipped: shallow clone)"
    exit 0
fi
prev="$(git -C "$REPO" rev-parse HEAD~1)"
at_out="$(bash "$GEN" --at HEAD~1 'git log --oneline -1' 2>/dev/null)"
has "$at_out" "$prev" "--at reports the requested ref's sha"
lacks "$at_out" "$sha" "--at does NOT report HEAD's sha"

# A leaked worktree per invocation would fill the disk and confuse git state.
leaked="$(git -C "$REPO" worktree list | awk '{print $1}' | grep -c 'pr-evidence-' || true)"
same "$leaked" "0" "--at removes its temporary worktree"

# Refusing bad input matters: an empty block reads as "nothing to report"
# rather than "you gave me nothing to run".
rc_none=0
bash "$GEN" >/dev/null 2>&1 || rc_none=$?
nonzero "$rc_none" "refuses with no commands instead of emitting an empty block"

rc_ref=0
bash "$GEN" --at not-a-real-ref 'echo x' >/dev/null 2>&1 || rc_ref=$?
nonzero "$rc_ref" "refuses an unknown ref instead of silently running at HEAD"

# ---- --at must pin the workspace even with NO local config file -------------
# Pinning used to need a local config, so the worktree read its own empty one.
live_ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
ws_out="$(bash "$GEN" --at HEAD~1 'bash scripts/sutando-config.sh workspace' 2>/dev/null)"
has "$ws_out" "$live_ws" "--at reports the LIVE workspace with no local config present"
lacks "$ws_out" "pr-evidence-" "--at does NOT report a workspace under its own temp dir"

# ---- the pinned config is minimal and private ------------------------------
# A whole-config copy in a 0755 worktree survives a failed cleanup, world-readable.
perm_out="$(bash "$GEN" --at HEAD~1 \
    'pwd' \
    'stat -f \"%Sp\" sutando.config.local.json 2>/dev/null || stat -c \"%A\" sutando.config.local.json' \
    'stat -f \"%Sp\" .. 2>/dev/null || stat -c \"%A\" ..' \
    'python3 -c "import json;print(sorted(json.load(open(\"sutando.config.local.json\")).keys()))"' \
    2>/dev/null)"
has "$perm_out" '-rw-------' "the pinned config is mode 600"
# `..` alone is not discriminating: without a wrapper the parent is TMPDIR,
# also 0700, so the mode would match for the wrong reason.
has "$perm_out" '/wt' "the worktree lives inside a wrapper dir, not directly in TMPDIR"
has "$perm_out" 'drwx------' "that wrapper is private (0700)"
has "$perm_out" "['workspace']" "the pinned config carries ONLY workspace, not the real one"

# ---- verbatim: trailing newlines are NOT stripped ---------------------------
# Compare through a FILE, never $( ): that strips the very newlines under test.
nl_file="$(mktemp)"
bash "$GEN" 'printf "alpha\n\n"' > "$nl_file" 2>/dev/null
gap="$(awk '/^alpha$/{f=1;n=0;next} f&&/^\[exit 0\]$/{print n;exit} f{n++}' "$nl_file")"
same "$gap" "1" "a trailing blank line survives byte-for-byte (1 blank before the marker)"

bash "$GEN" 'printf "beta"' > "$nl_file" 2>/dev/null
gap0="$(awk '/^beta$/{f=1;n=0;next} f&&/^\[exit 0\]$/{print n;exit} f{n++}' "$nl_file")"
same "$gap0" "0" "output with NO trailing newline still gets the marker on its own line"
rm -f "$nl_file"

# ---- --help carries its own text ------------------------------------------
# usage() used to sed its own header, so a comment edit silently gutted --help.
help_out="$(bash "$GEN" --help 2>/dev/null)"; help_rc=$?
same "$help_rc" "0" "--help exits 0"
has "$help_out" '--at <ref>' "--help documents the --at form"
has "$help_out" 'false-clean evidence' "--help keeps the reason --at pins the workspace"
noflag_out="$(bash "$GEN" 2>&1)"; noflag_rc=$?
nonzero "$noflag_rc" "no commands is an error, not an empty block"
has "$noflag_out" 'Usage:' "and the error prints the usage text"

if [[ "$fail" -ne 0 ]]; then
    echo "FAIL: pr-evidence generator"
    exit 1
fi
echo "PASS: pr-evidence generator captures stdout, stderr, exit codes, and binds to a sha."
