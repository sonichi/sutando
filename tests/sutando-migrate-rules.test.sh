#!/usr/bin/env bash
# Per-CLASS_RULES-row test fixtures for sutando-migrate.sh.
# Exercises `explain <path>` against a representative path for each rule and
# asserts the expected class matches. Catches glob-order regressions where a
# generic catchall starts swallowing paths a more-specific rule was meant to
# handle.
#
# Mini #design 2026-06-02: "Add a unit-test fixture per CLASS_RULES row.
# Catches future glob-order regressions."

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"

assert_class() {
    # $1=relpath, $2=expected class, $3=expected dest pattern (optional)
    local rel="$1" expected="$2" dest_pattern="${3:-}"
    local out cls dest
    out="$(bash "$MIGRATE" explain "$rel" 2>/dev/null)" || {
        echo "  FAIL: explain $rel exited non-zero"
        return 1
    }
    cls="$(echo "$out" | grep '^class:' | sed 's/^class:[[:space:]]*//')"
    dest="$(echo "$out" | grep '^dest:' | sed 's/^dest:[[:space:]]*//')"
    if [ "$cls" != "$expected" ]; then
        echo "  FAIL: $rel — expected class=$expected got $cls"
        return 1
    fi
    if [ -n "$dest_pattern" ] && ! echo "$dest" | grep -q "$dest_pattern"; then
        echo "  FAIL: $rel — expected dest match /$dest_pattern/ got $dest"
        return 1
    fi
    echo "  OK: $rel  →  $cls  ($dest)"
    return 0
}

fail=0

echo "==== Per-CLASS_RULES-row assertions ===="

# Root files
assert_class "build_log.md" "append" "<dest>/build_log.md" || fail=1
assert_class "conversation.log" "rehome-narrative-log" "workspace-narrative.log" || fail=1
assert_class "context-drop.txt" "append" || fail=1
assert_class "pending-questions.md" "append" || fail=1
assert_class "pending-questions-resolved-archive-2026-05-24.md" "rehome-dated-snapshot" "notes/archive" || fail=1
assert_class "session-state.md" "newest-mtime" || fail=1

# Loose JSONs (rehome-state)
assert_class "cloud-auth.json" "rehome-state" "state/auth/cloud-auth.json" || fail=1
assert_class "device.json" "rehome-state" "state/auth/device.json" || fail=1
assert_class "contextual-chips.json" "rehome-state" "state/contextual-chips.json" || fail=1
assert_class "voice-state.json" "rehome-state" "state/voice-state.json" || fail=1
assert_class "core-status.json" "rehome-state" "state/core-status.json" || fail=1

# tasks/
assert_class "tasks/archive/2026-05/task-x.txt" "structural" || fail=1
assert_class "tasks/processed/task-old.txt" "structural" || fail=1
assert_class "tasks/done/task-2025.txt" "structural" || fail=1
assert_class "tasks/task-1780000000000.txt" "inflight-guard" || fail=1
assert_class "tasks/random-file.txt" "skip-unknown" || fail=1

# results/
assert_class "results/archive/2026-06/task-x.txt" "structural" || fail=1
assert_class "results/calls/abc.txt" "structural" || fail=1
assert_class "results/processed/old.txt" "structural" || fail=1
assert_class "results/done/done.txt" "structural" || fail=1
assert_class "results/task-1780000000000.txt" "inflight-guard" || fail=1
assert_class "results/random-file.txt" "skip-unknown" || fail=1

# state/
assert_class "state/cores/Qingyuns-MacBook-Pro.alive" "skip-ephemeral" || fail=1
assert_class "state/auth/cloud-auth.json" "structural" || fail=1
assert_class "state/auth/device.json" "structural" || fail=1
# Per Lucy #design 2026-06-02: per-host status JSONs at state/ are now carved
# out to structural (was newest-mtime; multi-host scan would have dropped a host's data).
assert_class "state/contextual-chips.json" "structural" || fail=1
assert_class "state/voice-state.json" "structural" || fail=1
assert_class "state/core-status.json" "structural" || fail=1
assert_class "state/quota-state.json" "structural" || fail=1
assert_class "state/dynamic-content.json" "structural" || fail=1
# Other state/*.json (not in the per-host carve-out list) still hit newest-mtime
assert_class "state/random-other.json" "newest-mtime" || fail=1
assert_class "state/loop-paused-until.sentinel" "structural" || fail=1

# notes/ + logs/ + data/ + config/
assert_class "notes/m1-design.md" "collision-keep-both" || fail=1
assert_class "notes/archive/old.md" "collision-keep-both" || fail=1
assert_class "logs/discord-bridge.log" "structural" || fail=1
assert_class "logs/conversation.log" "structural" || fail=1
assert_class "data/conversation.sqlite" "collision-keep-both" || fail=1
assert_class "config/voice-agent.json" "collision-keep-both" || fail=1

# inboxes
assert_class "slack-inbox/screenshot.png" "structural" || fail=1
assert_class "telegram-inbox/voice.mp3" "structural" || fail=1

# Defensive non-canonical adds (Mini #7)
assert_class "agents/foo.json" "structural" || fail=1
assert_class "docs/design.md" "structural" || fail=1
assert_class "email-drafts/task-email.txt" "structural" || fail=1
assert_class "agent-inbox/processed/x.json" "structural" || fail=1

# Ordering check: state/auth/X.json should match state/auth/* BEFORE state/*.json|newest-mtime.
# This is Mini's #2 catch — without explicit ordering, auth files would be newest-mtime'd
# across sources, wrong for per-host identity.
assert_class "state/auth/cloud-auth.json" "structural" || fail=1

# Edge: path without any explicit rule match — falls to the catchall
# `*|quarantine-unknown` (added per Lucy #design + owner direction 2026-06-02).
# This is the new correct behavior: user content is preserved under
# legacy/<src-tag>/quarantine/, not silently skipped.
out="$(bash "$MIGRATE" explain "totally-novel-root-file.xyz" 2>/dev/null)"
if ! echo "$out" | grep -q "class:  quarantine-unknown"; then
    echo "  FAIL: novel root file should match quarantine-unknown catchall (got: $(echo "$out" | grep '^class:'))"
    fail=1
else
    echo "  OK: novel root file → quarantine-unknown (preserved under <dest>/legacy/<src-tag>/quarantine/)"
fi

echo
if [ "$fail" = "0" ]; then
    echo "==== ALL CLASS_RULES ROW ASSERTIONS PASSED ===="
    exit 0
else
    echo "==== TEST FAILED ===="
    exit 1
fi
