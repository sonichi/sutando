# Shared-checkout discipline

**Status:** Proposed working practice. This document is standalone guidance; it
does not change any always-loaded agent instruction file.

Several agent sessions work one checkout concurrently, and a sibling clone of the
same repository sits next to it. Under those conditions a working tree is a
shared mutable variable: its branch and its file content change between two of
your own commands, without either command causing the change.

Two failures on the same day, from opposite directions, share one root cause —
neither session pinned the tree state before acting on it.

## The rule

### 1. Assert the branch before any write

Before `rebase`, `reset`, `checkout`, `commit`, or `push`, assert that
`git branch --show-current` is the branch you intend to operate on. Do not infer
it from what you last did; another session may have moved the tree since.

```bash
[ "$(git branch --show-current)" = "$INTENDED" ] || { echo "wrong branch"; exit 1; }
```

### 2. Cite the ref when a conclusion rests on file content

`grep -rn` over the working tree answers *what is on disk this instant*. In a
shared checkout that is a different question from *what is in branch X*, and a
third question from *what is process P running*. Read content through the ref you
actually mean:

```bash
git show <ref>:<path>       # what is in branch X
git grep <pattern> <ref>    # search branch X, not the tree
```

If you cannot name which of the three questions your measurement answered, you
have not measured any of them.

### 3. Report the ref, not just the value

"Zero hits" is not a finding. "Zero hits in `git show origin/<branch>:<path>`" is.
A bare value gives a reader no way to detect that it was measured against the
wrong tree state, which is the failure this document exists for. The obligation
is heaviest when the report contradicts something the reader observed directly.

## Corollary: a running process is not its file

Python binds module-level constants at import. A daemon that imported a module
hours ago is executing the values the file held *then*, so current file content
is not evidence about it — in either direction. For a long-lived process the
discriminating evidence is what it imported at start:

```bash
ps -o lstart= -p <pid>                   # when it imported
lsof -a -p <pid> -d cwd                  # which checkout it imported from
git reflog --date=format:'%H:%M:%S'      # which ref that tree held at that time
```

The same holds for anything else that caches at load: config read once at
startup, a resolved TypeScript module graph, a skill manifest folded into a tool
table. Editing the file does not change what such a process runs; restarting it
does.

## Evidence

Both incidents occurred on 2026-08-23 in one checkout shared by two concurrent
agent sessions.

**A worktree read reported as a branch fact.** One session concluded that an
explicit owner directive had never taken effect: `grep -rn` for
`SUTANDO_AFFINITY_BUSY_MAX` over the working tree returned zero hits, and a
`git grep` on the base branch also returned zero. Both greps were correct.
`git reflog --date=format:'%H:%M:%S'` showed when they ran:

```
12:08:47 rebase (start): checkout origin/feat/lead-follower-pool
12:11:01 rebase (finish): returning to refs/heads/feat/lead-follower-pool
```

The greps ran inside that window — another session's rebase, during which HEAD
detaches to the base and the working tree transiently holds base content. The
tree read was therefore neither branch the conclusion was about. The setting was
live the whole time, at `src/runtime-api/pool_lead.py:32` on the branch carrying
it (commit `225767cc`). Cost: a correction sent to the owner telling them to
distrust a valid evening of observations, then a retraction of that correction.

The same claim carried a second, independently sufficient error. The conclusion
was about what a daemon started at 11:44:28 was executing; because module
constants bind at import, no reading of the current file — right or wrong — is
evidence about that process.

**A branch rebased by the wrong session.** The other session ran
`git rebase origin/<base>` without first checking `git branch --show-current`.
The shared tree was sitting on the first session's branch, so the rebase rewrote
that branch, including a commit the rebasing session had not authored. It was
caught at the `Successfully rebased and updated refs/heads/...` line, never
pushed, and undone with `git reset --hard` to the origin-matching SHA, verified
against `git ls-remote`. No lasting damage; disclosed unprompted.

The two are one defect read from opposite ends: one session trusted the tree's
content without pinning the ref, the other trusted the tree's branch without
pinning it.

## Related

- [Testing and coverage](testing-coverage.md) — the same evidence discipline
  applied to gate output.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — "The PR body should answer", including
  running a detached worktree at a PR head instead of switching the live checkout.
