# The file delivery protocol, written down as a state machine

Scope: `packages/ag2-sparrow/ag2_sparrow/delivery_core/` (Design A `backend_a.py`,
Design C `backend_c.py`, `contract.py`, `migration.py`) plus the `src/outbox.py`
primitives they bind. This is the "small database implemented on a filesystem"
the convergence review (#3279) asked to have written as a formal contract rather
than directory lore. Everything here describes **shipped code on `main`**; the
in-review deltas are fenced in their own section at the end.

**Reading rule:** filenames ARE records. A rename IS a state transition, and the
rename's atomicity IS the linearization point. There is no additional index —
what the directories contain is the whole truth, which is why GC and manual
"cleanup" are protocol operations, not housekeeping.

## Namespaces (Design C, per root)

| dir | meaning | record shape |
|---|---|---|
| `tmp/` | writes in flight, never authoritative | `{key}·{pid}·{ns}` payload staging |
| `ready/` | published, undelivered; the single retry slot | `{key}` (payload file) |
| `inflight/` | claimed by exactly one worker incarnation | `{key}·{worker}·{pid}·{start_usec}·{generation}` — the filename IS the claim token |
| `archive/` | terminal: delivered | rename of the claim file (`{incarnation}·{ns}`) |
| `parked/` | terminal-until-operator: quarantined | `{key}·{reason}·{tag}` |
| `attempts/` | retry accounting | `{key}` containing an integer |

`{key}` = `_safe_key(item_id)` — a sanitized stub plus a sha256 prefix, total
over all strings (the empty id is legal and encodes to `=…`). `worker` uses
`_safe_component`, which **refuses** the empty string at claim time — that
asymmetry is deliberate and load-bearing (see #3260 review history).

## Transitions and their linearization points

| transition | operation | linearization point | guarded by |
|---|---|---|---|
| publish | write `tmp/` → rename to `ready/{key}` | the rename | per-key lock |
| claim | rename `ready/{key}` → `inflight/{token}` | the rename | per-key lock; loser gets ENOENT, returns None |
| complete(CONFIRMED) | rename `inflight/{token}` → `archive/` | the rename | worker-component check on the token (forged tokens refused before any fs op) |
| complete(retryable) | rename `inflight/{token}` → `ready/{key}` | the rename | collision with a re-publish quarantines this copy |
| complete(OUTCOME_UNKNOWN) | rename → `parked/` | the rename | never auto-retried: the remote may have received it |
| park / max-attempts | rename → `parked/` | the rename | reason encoded in the filename |
| recover (dead owner) | rename `inflight/` → `ready/` | the rename | liveness verdict first (below), then per-key lock, then live-holder re-check |

Every transition is a single `os.rename` under the per-key striped lock
(`outbox._item_lock`). Nothing observes an intermediate state because there is
no intermediate state — a crash between any two operations leaves exactly one
of the two directory entries, and `recover()`'s job is to classify which.

## Ownership and fencing

The claim's owner is the **incarnation**, not the pid. The token encodes
`pid` + `start_usec` + a per-claim `generation` nonce; `outbox.process_identity`
classifies the pid as ALIVE / DEAD / UNKNOWN, and an ALIVE pid whose start time
mismatches the token is a **reused pid — the claimant is dead**. UNKNOWN is
never recovered (fail toward at-least-once delay, never toward double-send).
A recover that finds another live claim for the same key quarantines instead of
re-readying (`live-holder` reason): two claims on one key is a protocol breach,
not a race to resolve silently.

## Quarantine reason vocabulary (`parked/` filenames)

`outcome-unknown` · `max-attempts` · `collision` · `live-holder` · operator
reasons via `park()`. Parked entries are **terminal until an operator acts**;
no code path re-drives them today (the inspect/re-drive surface is #3279's
operator-CLI item, and `resolve_delivery` on the #3264 branch is its seed).

## Retry accounting

`attempts/{key}` is read-modify-replace under the per-key lock. It is deleted on
CONFIRMED. `park_at_attempts` compares post-increment, so the Nth failure parks.

## Migration epochs

`migration.py`: one protocol per root per epoch, fenced by an epoch marker.
Drainers check the fence; a mixed-state root (both A and C artifacts) refuses
BOTH backends rather than guessing (the reverse-transition fence shipped via
the #3160 line). Migration is one-shot convert with the fence held; a crash
mid-migration leaves the fence at the old epoch, so the old protocol stays
authoritative.

## Crash-point matrix → which test pins it

| crash window | outcome | pinned by |
|---|---|---|
| during `tmp/` write | orphan tmp file, no state change | `design-c-backend.test.py` (ghost states) |
| after publish rename, before ack to caller | item deliverable exactly once anyway | contract suite |
| between claim rename and work | dead-owner recovery re-readies | `design-c-backend.test.py` |
| worker dies mid-delivery | recovery re-readies (at-least-once) or UNKNOWN parks | contract + enforcement suites |
| between archive rename and lock release | claim gone, archive present — terminal | crash-between-terminal-and-release suite |
| pid reuse after crash | start_usec mismatch → treated dead | backend suite |

## Filesystem assumptions (the support boundary)

- **Single machine, single filesystem per root.** Locks are `flock`-based and
  per-open-file-description; cross-host coordination is explicitly out of scope.
- **Atomic same-directory rename** (POSIX). APFS and ext4 are in-support.
- **NFS and other network filesystems are OUT.** Both rename atomicity across
  clients and flock semantics are unreliable there; nothing in this protocol
  detects that misconfiguration today (a mount-type probe would be a reasonable
  hardening item).

## Garbage collection: current policy is NONE (open item)

`archive/` and `parked/` grow without bound; `attempts/` entries for parked
items persist. This is deliberate for now — every deletion in this protocol is
a state transition, so GC must be specified (age- or count-bounded archive
pruning that provably cannot delete the only evidence of a delivery) rather
than improvised by an operator with `rm`. Tracked under #3279; until then,
**manual deletion inside a root is a protocol violation**, not cleanup.

## In review, not yet on main (fenced)

- **#3260** — receipt-bearing terminal records: CONFIRMED writes a JSON record
  (schema/outcome/receipt/incarnation binding) through a staged-tmp R→F→M
  protocol with its own recovery rows (R-M finalize, M-D retire), total
  validation (`_staged_is_complete`), and fail-closed handling of malformed
  archive records. Changes the archive/ record shape; the legacy rename format
  remains recognized. Also introduces configurable durability modes
  (`strict`/default/`lax`; `strict` adds directory-entry fsync barriers before
  claim release).
- **#3264** — A→C import, `dual_read` fallback counter (the deletion gate for
  action 2 of #3279), and `resolve_delivery` (terminal-record lookup with
  counted A-fallback).

When those merge, their sections above should be folded in as shipped behavior
— this document tracks `main`, not intent.
