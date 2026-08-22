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

The separator is `~` throughout. The quarantine namespace's logical name is
PARKED; its **physical directory is `undelivered/`** — readers grepping the
filesystem must use the physical name.

| dir | meaning | record shape |
|---|---|---|
| `tmp/` | writes in flight, never authoritative | `{key}~{pid}~{ns}` payload staging |
| `ready/` | published, undelivered; the single retry slot | `{key}` (payload file) |
| `inflight/` | claimed by exactly one worker incarnation | `{key}~{worker}~{pid}~{start_usec}~{generation}` — the filename IS the claim token |
| `archive/` | terminal: delivered | rename of the claim file (`{incarnation}~{ns}`) |
| `undelivered/` | terminal-until-operator: quarantined (logical name PARKED) | `{key}~{reason}~{tag}` |
| `attempts/` | retry accounting | `{key}` containing an integer |

`{key}` = `_safe_key(item_id)` — a sanitized stub plus a sha256 prefix, total
over all strings (the empty id is legal and encodes to `=…`). `worker` uses
`_safe_component`, which **refuses** the empty string at claim time — that
asymmetry is deliberate and load-bearing (see #3260 review history).

## Transitions and their linearization points

Two mechanisms exist, and they have different crash shapes:

- **`os.rename`** (claim; legacy confirm): one operation, no intermediate
  state — a crash leaves exactly one of the two entries.
- **`os.link` then `os.unlink`** (`_move()`; publish's tmp→ready transfer):
  the **LINK is the linearization point** (atomic create-if-absent — the loser
  of a race gets EEXIST, not a clobber). A crash between link and unlink
  leaves **both** directory entries. That duplicate-name window is REAL, and
  its handling differs by row: a leftover `tmp/` twin is never authoritative
  by rule, and an `inflight/`↔`ready/` twin is absorbed by `recover()`'s
  live-and-dead classification and the `collision` quarantine. **The
  quarantine rows are NOT absorbed today (known hazard):** a crash after
  linking into `undelivered/` but before unlinking the claim leaves both, and
  `recover()` does not consult `undelivered/` — the dead claim is re-readied,
  violating OUTCOME_UNKNOWN's never-auto-retry promise (reproduced on main;
  fix owned by the terminal-record line: the twin shares the claim's inode,
  so recovery can finish the interrupted quarantine instead).

| transition | mechanism | linearization point | guarded by |
|---|---|---|---|
| publish | write `tmp/` → link to `ready/{key}` → unlink tmp | the link | per-key lock; EEXIST = slot occupied, publish returns False |
| claim | rename `ready/{key}` → `inflight/{token}` | the rename | per-key lock; loser gets ENOENT, returns None |
| complete(CONFIRMED) | rename `inflight/{token}` → `archive/` | the rename | worker-component check on the token (forged tokens refused before any fs op) |
| complete(retryable) | link `inflight/{token}` → `ready/{key}` → unlink | the link | link-loss = re-publish won the slot; this copy quarantines as `collision` |
| complete(OUTCOME_UNKNOWN) | link+unlink → `undelivered/` | the link | never auto-retried: the remote may have received it |
| park / max-attempts | link+unlink → `undelivered/` | the link | reason encoded in the filename |
| recover (dead owner) | link `inflight/` → `ready/` → unlink | the link | liveness verdict first (below), then per-key lock, then live-holder re-check |

All transitions run under the per-key striped lock (`outbox._item_lock`).

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
| between link and unlink (`_move`/publish) | BOTH names exist: tmp twin is never authoritative; an inflight/ready twin is classified by recover() (live-and-dead → `collision` quarantine) | backend suite (structural one-slot + collision rows) |
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

## Garbage collection: mechanism exists and is unwired; the POLICY is the open item

The pruning primitive exists **on Design C only**: its `cleanup(max_age_s)`
age-prunes `archive/`, `undelivered/`, `tmp/` and `attempts/`, and its
`attempts/` branch carries the required guard — a LIVE item's counter is
never pruned by age, since it is the item's park ceiling. `cleanup` is on the
shared contract, but **Design A's implementation is a deliberate no-op**
("bounded by lock striping") — interface availability, per-backend
implementation, and live policy are three different facts and only the first
is uniform. **What is missing everywhere is a caller**: no production site
invokes either,
so the directories grow without bound today. The open item is choosing a
schedule and retention (and whether archive pruning must first export the
receipt elsewhere), then wiring the existing primitive — not designing GC
from scratch. Tracked under #3279; until a caller exists, **manual deletion
inside a root is a protocol violation**, not cleanup.

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
