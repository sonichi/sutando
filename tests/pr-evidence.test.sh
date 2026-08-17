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
out="$(bash "$GEN" 'echo alpha-marker' 'ls /definitely-absent-xyz' 2>/dev/null)"
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

if [[ "$fail" -ne 0 ]]; then
    echo "FAIL: pr-evidence generator"
    exit 1
fi
echo "PASS: pr-evidence generator captures stdout, stderr, exit codes, and binds to a sha."
