# Shared-checkout discipline

**Status:** Proposed working practice. This document is standalone guidance; it
does not change any always-loaded agent instruction file.

Several agent sessions work one checkout concurrently, and a sibling clone of the
same repository sits next to it. Under those conditions a working tree is a
shared mutable variable: its branch and its file content change between two of
your own commands, without either command causing the change.

Three failures on the same day share one root cause — **no session had exclusive
use of the tree it was mutating.** Each also shows an expired pin, but that is the
mechanism by which the missing isolation surfaced, not the defect itself: a pin
that never expired would still not have stopped a peer from writing.

## The rule

### 1. Isolate the writer — exclusive mutation is the control

**No assertion fixes a shared tree, because there is no check-and-write pair a peer
cannot interleave: `&&` sequences two processes, it does not lock the worktree.**
Every mutating session gets its own worktree or clone, or every writer participates
in one ownership lock. Sessions sharing the live tree are **read-only**.

Everything below is what you do *inside* that boundary, or what you fall back to
when you genuinely cannot have one. None of it substitutes for the boundary.

### 2. Measure against an immutable OID, and report it

"Zero hits" is not a finding. But "zero hits in `origin/<branch>`" is not one
either: **worktrees share refs**, so a fetch or a peer can move that ref after you
measured, and the same reported name then identifies two different commits:

```text
reported-ref=measured oid=9965422e subject=first
same-reported-ref=measured oid=ba0735b4 subject=second
```

So resolve the ref to an immutable OID **once**, measure against the OID, and
report both — the name for meaning, the OID for identity:

```bash
OID=$(git rev-parse "origin/$BRANCH")     # pin ONCE
git grep <pattern> "$OID" -- <path>       # measure the pinned state
echo "zero hits in origin/$BRANCH ($OID)" # report both
```

A bare value gives a reader no way to detect that it was measured against the
wrong tree state, which is the failure this document exists for; a bare ref gives
them no way to detect that the state moved underneath it. The obligation is
heaviest when the report contradicts something the reader observed directly.

**Superseded form — citing the ref alone.** An earlier version of this rule said to
read content through the ref you mean, which is better than reading the tree but
still ambient:

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

Use it only to decide *which* ref you mean; resolve that ref to an OID before you
measure, and report the OID.

### 3. Assert the branch before each write — defense in depth, never the control

Before `rebase`, `reset`, `checkout`, `commit`, or `push` — before each one, not
once per pass — assert that `git branch --show-current` is the branch you intend
to operate on. Do not infer it from what you last did, and do not carry an
earlier answer forward.

The per-pass form is the trap precisely because it does not look like one: it is
a real check, it passes, and running it reads as compliance. But what it
establishes is where the tree *was*. On a variable another session can write,
that answer expires the moment control leaves your process.

A reviewer demonstrated the residual hole in the check-then-act form by forcing one
switch after the branch read returned `intended`:

```text
branch-check-returned=intended
raced-commit-branch=other
remote-intended-equals-wrong-branch=yes
```

`git push origin HEAD:"$INTENDED"` names the *destination* and leaves the **source**
ambient, so when `HEAD` had moved the push fast-forwarded remote `intended` to
another branch's commit — the unreviewed-code path this document exists to close.

Inside an exclusive boundary, these remain useful as defense-in-depth — never as the
control itself. Name both ends, so the source cannot be supplied by ambient `HEAD`:

```bash
[ "$(git branch --show-current)" = "$INTENDED" ] && git commit -m "<message>"
git push origin "$INTENDED:$INTENDED"   # both ends named; no ambient HEAD
```

## Corollary: a running process is not its file

Python binds module-level constants at import. A daemon that imported a module
hours ago is executing the values the file held *then*, so current file content
is not evidence about it — in either direction. What you want is the revision it
imported; what these commands give you is **context, not proof**:

```bash
ps -o lstart= -p <pid>                   # process START — not import time
lsof -a -p <pid> -d cwd                  # CURRENT cwd — not module provenance
git reflog --date=format:'%H:%M:%S'      # host-local, and may not be recoverable
```

Measured gaps: start-to-import was **1.218 s** in one control, and `lsof` cwd read
`/private/tmp` while the module actually came from `src/`. Each is off by an
amount you cannot bound from outside. **Exact provenance needs a witness of the
BYTES that were imported**, not an inference from these three.

A startup build sha is not by itself that witness. It names the ref at process
start, which is the imported revision only when execution comes from a verified
clean, immutable artifact. Two adjacent cases break it: a dirty tree yields the
same OID for different bytes, and a ref that moves after start leaves the
recorded OID naming a revision the process never ran. Treat a startup sha as
CONTEXT; for a dirty tree or anything lazily imported, require a content or
import-time witness.

`runtime-identity` is stronger than a build sha alone: `src/remote-gateway-bridge.py:117-120`
records `loader_sha256` and `module_sha256` beside `build_sha`, so a same-HEAD edit
is visible where a build sha alone shows nothing
(`tests/gateway-runtime-identity.test.py:224-241` pins that drift).

**They are startup DISK SNAPSHOTS, not proof of the bytes running.** The loader
hashes the file and then reads it a second time for `compile`, so a checkout
switch between those reads leaves the digest describing bytes that were never
executed. Kewei demonstrated it by interposing the second read: `alternate bytes
executed = True, reported == disk = True, reported == compiled = False`, while
the identity suite still passed. The loader's self-hash has the mirror gap —
Python has already loaded the entrypoint before the path is re-read to hash it.

So treat these digests as drift evidence, which is what they reliably are, and
not as import provenance. Proving the imported bytes needs the buffer passed to
`compile` to be the thing hashed, plus a witness taken at import time; that is a
change to the loader, not to this document. And they attest only the files they
hash, never the whole tree.

The same holds for anything else that caches at load: config read once at
startup, a resolved TypeScript module graph, a skill manifest folded into a tool
table. Editing the file does not change what such a process runs; restarting it
does.

### The inverse: the tree is a pending deploy

The three rules above cover reading the tree and writing it, and the corollary
covers a process that no longer matches its file. There is a fourth direction
with no read, no write, and no stale process involved: **changing the tree
changes what the next restart will load.**

A checkout parked on a non-default ref is a pending deploy of that ref for every
supervised service that can restart itself. Nothing needs to go wrong for the
hazard to exist — a `KeepAlive=true` service that crashes during the window comes
back on whatever the tree holds, and no one issued a deploy.

This repo already ships a detector for it: the **`live-checkout-branch`** probe in
`health-check.py`. Naming it matters — a rule with a live detector behind it is
enforceable, and one without is the discipline-versus-mechanism gap this document
exists to close. It warns when the live checkout is off the default branch, citing
a host that spent four days serving a PR branch from a checkout nobody remembered
parking there:

```
live checkout is on branch 'feat/...', expected 'main' — bridges/core
auto-restart onto this checkout, so a leftover PR-branch checkout ships
stale/unreviewed code
```

So: park the shared checkout on a non-default ref only for as long as you are
watching it, and treat the warn as a deploy notice rather than branch-hygiene
nagging. Author PRs in worktrees, which have no supervised service pointed at
them.

**The remedy is narrower than "never park the tree", and the corollary above is
why.** The reason parking feels necessary is usually that a branch had to be
deployed for a live witness, so restoring the checkout looks like it costs you the
evidence. It does not — but the guarantee is narrower than "same pid, same
behaviour". `git switch main` preserves **already-imported module state**; it does
not preserve anything the process re-reads from disk. Lazy imports, subprocesses and
per-call file reads all change under a stable pid. In this repo the bridges resolve
`skills/audio-transcribe/scripts/transcribe.py` per attachment and
`src/optional_script.py` starts that on-disk script on every call, so a reviewer
drove the production runner across a switch and got:

```text
parent-pid-before=30169
first-call=pr-branch-behaviour
parent-pid-after=30169
second-call=main-behaviour
```

`src/health-check.py` says the same of skills — re-read from the checkout on every
invocation. So restoring the tree clears the parked-tree exposure **only once the
working tree is verifiably clean**, and it keeps already-imported state; it does
**not** make the tree safe to change under a live witness.

The qualifier is load-bearing. `git switch main` moves `HEAD`; it carries any
*compatible* uncommitted tracked edit across with it rather than discarding it. A
reviewer switched a tree holding one such edit onto `main` and measured:

```text
distinct-branches=True branch=main
head-bytes=main-bytes disk-bytes=uncommitted-pr-bytes status=M service.txt
live-checkout-branch=ok: live checkout on 'main'
```

The probe answered `ok` because it reads the branch *name*; a supervised restart at
that point loads the uncommitted bytes, which exist in no commit anyone reviewed.
So the remedy is two steps, and the name vouches only for the first: `git switch
main`, then `git status --porcelain` prints nothing. `health-check.py`'s
**`live-tree-drift`** probe (`check_live_tree_drift`) is the detector for the
second half — it was added after tracked dirty files were found running in
production while existing in no commit. For a witness that must stay stable, use
CONTRIBUTING's detached-worktree service path rather than parking the shared
checkout.

That is the same fact as the corollary, read in the other direction. "A running
process is not its file" is usually stated as a warning — you cannot infer the
process from the tree. Here it pays, but only as narrowly as stated above: you can
clear the parked-tree exposure without giving up **already-imported state**. It
does NOT preserve anything re-read from disk, so a disk-backed witness still needs
a detached worktree. Retargeting a capability path between two calls to the
production runner changed its output with the parent pid unchanged
(`parent-pid-before == parent-pid-after`, first call vs second call differing).

## Evidence

All three incidents occurred on 2026-08-23 in one checkout shared by concurrent
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

**A pass's work landed on a branch the tree moved to mid-pass.** A third session
did check `git branch --show-current` at the start of its pass and got its own
branch. Its reflog shows what happened next:

```
checkout: moving from feat/pool-operability to fix/pool-bridge-assigned-blind
```

That checkout was another session's. Everything after it — edit, added test,
commit, push — went to the other branch: `4c7c3297` landed on
`origin/fix/pool-bridge-assigned-blind` on top of `8f3b355a`. The push was a
clean fast-forward, confirmed with `git merge-base --is-ancestor`, so nothing was
rewritten or lost; the author cherry-picked the work onto its own branch as
`54f71855` and disclosed immediately, and the branch owner reviewed it and chose
to leave it in place. The check here was run and was correct when it ran; it was
simply a per-pass reading of a variable another session can write.

The three are one defect in three positions, and the defect is **an unisolated
writer**: content trusted from a tree a peer could rewrite, a branch mutated in a
tree a peer could switch, and a pin that expired because the tree kept moving after
it was taken. The expired pin is the most tempting to read as "should have
re-pinned"; it is not, because no re-pin interval closes a window a peer can enter.
None of them was carelessness about git — each session knew the commands it was
running. All three came from mutating a working tree nobody owned exclusively.

## Related

- [Testing and coverage](testing-coverage.md) — the same evidence discipline
  applied to gate output.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — "The PR body should answer", including
  running a detached worktree at a PR head instead of switching the live checkout.
