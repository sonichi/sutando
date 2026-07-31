#!/usr/bin/env bash
# Guard: prove a test suite did not write into a REAL git clone.
#
# Extracted from tests/sync-workspace.test.sh so the guard itself is testable.
# It was previously inline, which meant the only way to check it was to mutate the
# operator's own clone by hand — so its blind spot went unnoticed: it compared only
# `rev-parse HEAD`.
#
# HEAD ALONE IS NOT THE HARM. A reached clone can be left dirty without HEAD moving:
# an untracked probe file, a staged-but-uncommitted write, or a commit that failed
# after `git add`. Each leaves HEAD identical. An untracked file in the operator's
# clone is then carried by the next legitimate sync — the two-hop leak the suite
# exists to stop. So snapshot the index/worktree as well.
#
# Compare BEFORE vs AFTER rather than asserting clean: the operator's clone may
# legitimately be dirty already, and demanding cleanliness would fail on a real box.

# rcg_snapshot <dir> — record HEAD + porcelain status into RCG_* globals.
rcg_snapshot() {
  local dir="$1"
  RCG_DIR="$dir"
  RCG_HEAD_BEFORE=""
  RCG_STATUS_BEFORE=""
  RCG_TAKEN=""
  if [ -d "$dir/.git" ]; then
    RCG_HEAD_BEFORE="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo '')"
    # -uall so a file inside an untracked DIRECTORY is listed individually rather
    # than collapsed to the directory name (which would hide a second write).
    RCG_STATUS_BEFORE="$(git -C "$dir" status --porcelain -uall 2>/dev/null || echo '')"
    RCG_TAKEN=1
  fi
}

# rcg_assert — print the harm and return 1 if the clone changed; 0 otherwise.
rcg_assert() {
  [ -n "$RCG_TAKEN" ] || return 0
  local after_head after_status
  after_head="$(git -C "$RCG_DIR" rev-parse HEAD 2>/dev/null || echo '')"
  after_status="$(git -C "$RCG_DIR" status --porcelain -uall 2>/dev/null || echo '')"
  if [ "$after_head" = "$RCG_HEAD_BEFORE" ] && [ "$after_status" = "$RCG_STATUS_BEFORE" ]; then
    return 0
  fi
  echo ""
  echo "  ✖ TRIPWIRE: the suite wrote to the REAL memory clone $RCG_DIR"
  if [ "$after_head" != "$RCG_HEAD_BEFORE" ]; then
    echo "      HEAD before: $RCG_HEAD_BEFORE"
    echo "      HEAD after : $after_head"
  else
    echo "      HEAD unchanged ($after_head) — the write did not commit."
  fi
  if [ "$after_status" != "$RCG_STATUS_BEFORE" ]; then
    echo "      index/worktree CHANGED. New or altered entries:"
    diff <(printf '%s\n' "$RCG_STATUS_BEFORE") <(printf '%s\n' "$after_status") \
      | sed -n 's/^> /        /p' | head -20
  fi
  echo "      A test reached a real repo. FAILURE even if every check passed."
  return 1
}
