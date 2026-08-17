#!/usr/bin/env bash
# The evidence generator must capture what actually happened — stdout, stderr,
# and the exit code — and bind the block to a sha. If it can drop a failure or
# emit a block for commands it never ran, it launders a claim instead of proving
# one, which is the exact failure it exists to prevent.
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

# The stderr assertion must key on text the OS produces, NOT on anything inside
# the command string: the command is echoed into the block, so a marker written
# in the command itself still appears when stderr is discarded, and the check
# passes for the wrong reason. Caught by a control that failed to fail.
out="$(bash "$GEN" 'echo alpha-marker' 'cat /definitely-absent-xyz' 2>/dev/null)"
sha="$(git -C "$REPO" rev-parse HEAD)"

has "$out" 'alpha-marker' "captures stdout of a passing command"
has "$out" 'No such file or directory' "captures STDERR — asserts on OS text, not on the echoed command"
has "$out" '[exit 1]' "records the ACTUAL nonzero exit, not just 'failed'"
has "$out" '[exit 0]' "records the zero exit too"
has "$out" '$ echo alpha-marker' "echoes each command it ran"
has "$out" "<!-- pr-evidence HEAD $sha -->" "stamps the block with the sha it was generated at"

# A block naming no sha cannot be told from a stale or hand-written one, and a
# stamp buried mid-block survives a partial paste that drops the evidence.
first="$(printf '%s' "$out" | head -1)"
same "$first" "<!-- pr-evidence HEAD $sha -->" \
    "stamp is the FIRST line, so a truncated paste loses it visibly"

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
# The pinning used to run only when sutando.config.local.json existed, so in the
# default configuration the worktree resolved its own empty workspace/ and every
# workspace-reading probe reported clean regardless of the code. That is the
# false-clean evidence this tool exists to prevent, so it is a blocking case.
live_ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
ws_out="$(bash "$GEN" --at HEAD~1 'bash scripts/sutando-config.sh workspace' 2>/dev/null)"
has "$ws_out" "$live_ws" "--at reports the LIVE workspace with no local config present"
lacks "$ws_out" "pr-evidence-" "--at does NOT report a workspace under its own temp dir"

# ---- the pinned config is minimal and private ------------------------------
# It used to be a copy of the whole local config (vault, migrate, …) dropped
# into a 0755 worktree, where a failed cleanup strands it world-readable.
perm_out="$(bash "$GEN" --at HEAD~1 \
    'pwd' \
    'stat -f \"%Sp\" sutando.config.local.json 2>/dev/null || stat -c \"%A\" sutando.config.local.json' \
    'stat -f \"%Sp\" .. 2>/dev/null || stat -c \"%A\" ..' \
    'python3 -c "import json;print(sorted(json.load(open(\"sutando.config.local.json\")).keys()))"' \
    2>/dev/null)"
has "$perm_out" '-rw-------' "the pinned config is mode 600"
# `..` alone is not discriminating: with no wrapper the parent is the system
# TMPDIR, which is ALSO 0700, so the mode matches for the wrong reason. The
# wrapper's existence is what the path shape proves.
has "$perm_out" '/wt' "the worktree lives inside a wrapper dir, not directly in TMPDIR"
has "$perm_out" 'drwx------' "that wrapper is private (0700)"
has "$perm_out" "['workspace']" "the pinned config carries ONLY workspace, not the real one"

if [[ "$fail" -ne 0 ]]; then
    echo "FAIL: pr-evidence generator"
    exit 1
fi
echo "PASS: pr-evidence generator captures stdout, stderr, exit codes, and binds to a sha."
