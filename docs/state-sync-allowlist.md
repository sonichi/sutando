# State sync allowlist — design

**Status:** Design. Tracks #872. Implementation deferred; this doc is the contract that has to land before code does.

## Why

The 3-space model (`docs/workspace-design.md`) deliberately keeps State per-machine:

- State is rebuildable.
- `rm -rf $SUTANDO_WORKSPACE` is meant to be survivable.
- Logs and PIDs from one Mac have no value on another.

Real use case that bends the rule: a user with multiple Macs wants a **cross-machine task queue** — start a Telegram task on the MacBook, finish it on the Mac Studio because that's the machine with the GPU / the right tool / the recording it needs. The relevant State subdirectories are:

- `state/fleet/` — proposed home for cross-machine queue + claim files.
- Possibly `tasks/` and `results/` themselves, if the user wants the queue itself fleet-aware (not just a coordination overlay).

The straightforward fix — sync the whole workspace — breaks the "rebuildable / per-machine" invariant for the 95% of State that genuinely should stay local. The right shape is **opt-in, allowlist-scoped sync**.

## Proposal

Add `$SUTANDO_WORKSPACE/state/.sync-allowlist` — a newline-delimited list of subpaths under `state/` that get synced across the fleet. Default (file missing) → nothing in State syncs. Existing single-machine installs are unaffected.

Example file:

```
fleet/
queue/
```

The sync runs as a sibling script (`scripts/sync-fleet-state.sh`) that the existing `scripts/sync-memory.sh` cron can call after the memory sync, so the fleet picks up new claims at the same cadence Memory propagates.

## Allowlist file format

| Property | Value |
|---|---|
| **Path** | `$SUTANDO_WORKSPACE/state/.sync-allowlist` |
| **Encoding** | UTF-8, newline-delimited |
| **Comments** | `#`-prefixed line, full-line only (no trailing comments) |
| **Empty lines** | Ignored |
| **Each entry** | A relative path under `state/`. Trailing `/` optional; treated identically with or without. No leading `./`. No `..` — entries that resolve outside `state/` are skipped with a warning. |
| **Glob support** | None in v1. Add later if needed; for now, list explicit subpaths. |
| **Case sensitivity** | Inherits the filesystem's case-sensitivity (HFS+ / APFS = case-insensitive by default; ext4 / case-sensitive APFS = case-sensitive). Don't add case-insensitive normalization in v1; let the FS rule. |

Empty file → opt-in but nothing actually syncs. Missing file → opt-out entirely (the default).

## Coordination protocol — cross-machine task queue

This is the tricky bit. Sync alone gives convergence, not coordination. Two Macs polling the same `state/fleet/queue/` will both pick up the same task and both try to execute it. The protocol below adds the claim layer.

### Claim-via-atomic-rename

A new task lands as `state/fleet/queue/task-<id>.txt`. A Mac claims it by renaming it to `state/fleet/queue/task-<id>.claimed-<host>.txt` **using `os.rename` / `fs.renameSync`** — POSIX-atomic on the same filesystem. The losing Mac sees the original name vanish and walks away.

Properties:

- **Single-machine semantics are POSIX-atomic.** Within one filesystem, `rename` is a single inode swap; no two processes both succeed.
- **Across machines (post-sync), races are eventual-consistency races, not POSIX races.** The sync (git, rsync, or whatever the transport is) propagates *some* version. The protocol has to handle the case where two Macs each rename locally before the next sync round.

### Race handling for cross-machine collisions

The honest version: if two Macs rename the same task in the gap between sync rounds, we'll have two claimed-by-different-hosts files post-merge. Three options, in increasing order of effort:

1. **Last-rename-wins** (v1 default). The sync transport's merge rule decides. Whichever claim arrives last in the merged history is the canonical claim; the loser sees its `.claimed-<host>.txt` file disappear on the next pull (the winner already deleted it or moved it to `state/fleet/done/`). The loser must idempotently re-claim from the queue or noop if it already wrote results.
2. **Etcd / DynamoDB / single coordinator** (v2 if v1 hurts). Out of scope for the personal-fleet case.
3. **CRDT-style "all claims valid, results merge"** (over-engineered for two Macs). Out of scope.

The acceptance test for v1: a duplicate-claim race causes at most one duplicate execution, and produces zero "task lost" events.

### Idempotency on the executing side

Both Macs may execute the same task once in a race window. The task processor must:

- Write its result to `state/fleet/results/task-<id>.<host>.txt` (host-qualified filename), not `task-<id>.txt`. The bridge that delivers the result picks one — first-arrived-wins or both with a `[deduped:]` marker if you want strict-once delivery.
- Be safe to run twice on the same input. For tasks with side effects (send email, post Discord message), gate on a per-task "I already did this" flag written **before** the side effect: `state/fleet/done/task-<id>.<host>.flag`. If the flag exists on either Mac post-merge, skip the side effect on that Mac.

Convention: every task processor that opts into fleet execution must implement this gate. Single-machine tasks (the default) don't pay the cost.

### State transitions

```
state/fleet/queue/task-<id>.txt              # new, unclaimed
  → state/fleet/queue/task-<id>.claimed-<host>.txt   # one Mac claimed
  → state/fleet/done/task-<id>.<host>.flag    # processor wrote side-effect flag
  → state/fleet/results/task-<id>.<host>.txt  # processor wrote result
  → (eventually) state/fleet/archive/task-<id>/...    # post-delivery cleanup
```

A Mac that wakes up post-sync and sees `state/fleet/done/task-<id>.<otherhost>.flag` must noop on `task-<id>` even if its own queue file still says claimed-by-self. The flag is the authoritative "this is handled, stop touching it" signal.

## Lifecycle invariants

The 3-space model's invariant is `rm -rf $SUTANDO_WORKSPACE` is survivable. With fleet sync added, the invariant becomes:

- `rm -rf $SUTANDO_WORKSPACE` on **one** Mac is survivable — the next sync pulls fleet state back.
- `rm -rf $SUTANDO_WORKSPACE` on **all** Macs simultaneously loses uncommitted claims + uncommitted results. This is a user-action consequence, not a bug; document it but don't engineer for it.
- A Mac that loses network mid-claim retries the claim on next sync. If the same task is already done by another Mac (flag present post-sync), it noops.

Tasks **not** on the allowlist behave exactly as today — no change to single-machine semantics.

## Implementation sketch

This is the rough shape for when code actually lands; not part of this PR.

### `scripts/sync-fleet-state.sh`

```text
1. Read $SUTANDO_WORKSPACE/state/.sync-allowlist. If missing, exit 0 — sync is opt-out by default.
2. For each entry, rsync (or git-add) the subpath into the same private memory-sync repo's `fleet/` subdir.
3. Conflict policy: rsync mtime-wins for v1 (same as memory-sync). Filename collisions in claim files mean a race — let the protocol handle it on the next read.
4. Hook into the existing scripts/sync-memory.sh cron, after the memory sync runs.
```

### Task processor changes

- Existing single-machine task processors: no change.
- A new "fleet-aware" wrapper around the bridge consumers: claims via atomic rename, writes the done flag before side effects, writes host-qualified results.

### Wire-up

- Telegram bridge's task-write goes to `tasks/` (unchanged). A separate `fleet-router` (new process or proactive-loop step) copies tasks the owner marked fleet-eligible into `state/fleet/queue/`.
- Marking eligibility: per-task field `route: fleet` (default `local`) in the task file body. Adds zero overhead to single-machine flows.

## Failure modes worth listing

| Mode | What happens | Mitigation |
|---|---|---|
| Both Macs claim the same task in the sync gap | Two `.claimed-<host>.txt` files post-merge | Last-rename-wins; processor idempotency on side effects |
| One Mac is offline, claims, executes locally, then comes back online with stale state | Claim file is local-only until sync; result lands when network returns | Acceptable — the task did get processed, just on the wrong Mac |
| Allowlist entry points outside `state/` (e.g. `../etc/passwd`) | Path-injection class | Resolve, check `startswith(state_dir)`, skip + log if not |
| User accidentally adds `logs/` to the allowlist | Gigabytes of per-machine logs sync to the memory repo | Document that allowlist entries should be small + cross-machine-meaningful; no enforcement in v1 |
| Sync transport (git) gets stuck on a binary file conflict | Sync pauses; new tasks queue on each Mac but don't propagate | Same as memory-sync today; the operator unblocks manually |
| Both Macs write the done-flag, then both Macs send the same email | Duplicate side effect | Idempotent side-effect helpers (e.g. for email: include a deterministic `Message-Id` derived from task-id, so the receiving SMTP server drops the duplicate). This is the part where the convention matters more than the protocol. |

## Open questions

1. **Transport for the fleet sync — same memory-sync git repo, or its own?**
   Same repo is simpler (one cron, one auth surface). Separate repo isolates a noisy claim/result churn from the relatively quiet memory commits. Lean toward same-repo for v1, split only if commit churn becomes a problem.

2. **Should `tasks/` and `results/` themselves be opt-in to sync, or only `state/fleet/`?**
   Cleaner to keep `tasks/` per-machine and have a separate fleet-aware path (`state/fleet/queue/`). Avoids retro-fitting fleet-semantics onto the single-machine bridge code. Lean fleet-only.

3. **Does the claim-file need a TTL / lease?**
   If a Mac claims and then crashes before writing results, the task is stranded. Lease (e.g. claim files older than 1h auto-released) helps but adds clock-sync sensitivity. Defer; observe in practice before adding.

4. **What's the right granularity for the allowlist?**
   v1 proposes subdir-level (`fleet/`, `queue/`). File-level globs (`*.claim`) might be useful later. Defer until a real use case calls for it.

## Acceptance for the implementation PR (when it happens)

- Owner can opt specific State subdirs into sync without compromising the default "rm -rf workspace is recoverable" invariant.
- Cross-Mac task queue works without **lost-task** incidents.
- **Duplicate-process** incidents bounded to at most one duplicate per task in a sync gap; documented as the v1 trade.
- All claim files use atomic rename, not write-then-copy.
- All side-effect-bearing task processors gate on the done-flag before executing.
- Tests cover: empty allowlist, missing allowlist, allowlist with path-escape attempt, two-Mac race simulation.

## Out of scope (don't bring back in v1)

- CRDT-style claim merging.
- External coordinator (etcd, DynamoDB).
- Per-task SLA / priority across the fleet (priority calculator already exists at `src/task_priority.py`; integrate but don't redesign).
- Multi-user / team-shared fleet. The 3-space model is per-user-per-fleet; this stays inside that scope.

## Relationship to other docs

- [`docs/workspace-design.md`](workspace-design.md) — 3-space model. This proposal sits inside the State space and tunes its sync rule from "none" to "opt-in allowlist."
- [`docs/workspace-contract.md`](workspace-contract.md) — implementation reference. Will need a line referencing this doc once the fleet/ path is real.
- [`docs/memory-sync.md`](memory-sync.md) — Memory sync mechanics. The fleet sync rides on the same transport in v1.

Tracks #872. RFC #858 Decision 4.
