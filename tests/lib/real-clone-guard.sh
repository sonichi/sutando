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
#
# STATUS CODES ARE NOT CONTENT. `git status --porcelain` reports an already-modified
# tracked file as " M path" both before and after the suite overwrites it, and an
# existing untracked file as "?? path" either way — so a write that CLOBBERS the
# operator's own uncommitted work leaves the status output byte-identical and the
# guard green. Fingerprint the actual bytes too: the tracked diff against HEAD, plus
# a hash per untracked file.

# _rcg_content_digest <dir> — bytes-level fingerprint of the working tree's dirt.
# Tracked modifications come from `git diff HEAD` (covers staged AND unstaged
# content); untracked files are hashed individually, since no diff covers them.
_rcg_content_digest() {
  local dir="$1"
  {
    # THE INDEX IS A THIRD SURFACE. `git diff HEAD` compares the WORKTREE to HEAD and
    # ignores staged content entirely, so on an already-`MM` path the staged blob can
    # be replaced while status, worktree bytes and this diff all stay identical.
    # Verified: before_index=index-before -> after_index=index-after, guard passed.
    # `--cached` (index vs HEAD) is the missing comparison.
    git -C "$dir" diff --cached 2>/dev/null
    git -C "$dir" diff HEAD 2>/dev/null
    git -C "$dir" ls-files --others --exclude-standard -z 2>/dev/null \
      | while IFS= read -r -d "" f; do
          printf '%s ' "$f"
          shasum -a 256 "$dir/$f" 2>/dev/null | awk '{print $1}'
        done
    # THE REPO'S OWN CONTROL SURFACE. `git status` and `git diff` describe the
    # WORKING TREE; they say nothing about .git/ itself. Found by shape-hunt after
    # the HEAD and status-code rounds: a suite could drop a post-commit HOOK into
    # the operator's clone (arbitrary code on their next commit) or repoint
    # remote.origin.url (redirecting where the vault pushes) and this guard
    # returned success. Both are worse than the data write it was built to catch.
    #
    # Only config + hooks are covered, deliberately. The rest of .git/ churns on
    # ordinary reads (index mtime, logs/HEAD, packed refs), so hashing it wholesale
    # would produce false positives and the guard would be disabled within a week.
    shasum -a 256 "$dir/.git/config" 2>/dev/null | awk '{print $1}'
    find "$dir/.git/hooks" -type f 2>/dev/null | LC_ALL=C sort | while IFS= read -r h; do
      printf '%s ' "${h#$dir/}"
      shasum -a 256 "$h" 2>/dev/null | awk '{print $1}'
    done
  } | shasum -a 256 | awk '{print $1}'
}

# KNOWN, NAMED LIMITATION — a write to a path matching .gitignore is NOT detected.
# `ls-files --others --exclude-standard` skips ignored files by design, and dropping
# --exclude-standard would walk node_modules-scale trees on a real clone: slow, and
# noisy enough that the guard would get switched off. Recorded rather than silently
# accepted, because "the guard covers every write" would be false.

# rcg_snapshot <dir> — record HEAD + porcelain status + content digest into RCG_* globals.
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
    RCG_CONTENT_BEFORE="$(_rcg_content_digest "$dir")"
    RCG_TAKEN=1
  fi
}

# rcg_assert — print the harm and return 1 if the clone changed; 0 otherwise.
rcg_assert() {
  [ -n "$RCG_TAKEN" ] || return 0
  local after_head after_status after_content
  after_head="$(git -C "$RCG_DIR" rev-parse HEAD 2>/dev/null || echo '')"
  after_status="$(git -C "$RCG_DIR" status --porcelain -uall 2>/dev/null || echo '')"
  after_content="$(_rcg_content_digest "$RCG_DIR")"
  if [ "$after_head" = "$RCG_HEAD_BEFORE" ] \
     && [ "$after_status" = "$RCG_STATUS_BEFORE" ] \
     && [ "$after_content" = "$RCG_CONTENT_BEFORE" ]; then
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
  if [ "$after_content" != "$RCG_CONTENT_BEFORE" ] && [ "$after_status" = "$RCG_STATUS_BEFORE" ]; then
    echo "      status codes UNCHANGED but CONTENT changed — an already-dirty file"
    echo "      was overwritten. This is the clobber case status alone cannot see."
  fi
  echo "      A test reached a real repo. FAILURE even if every check passed."
  return 1
}
