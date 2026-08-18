# Task-envelope HMAC — current status and phased plan

A host-local authenticity/integrity seal on the `tasks/` file boundary.
Owner-ratified threat model and rollout phases: 2026-08-17/18 design thread.

**Change control (owner directive 2026-08-18): this is a security-critical
component — changes to the envelope, sealer, key handling, or enforcement
behavior MUST be reviewed and approved by key maintainers before landing.**

## What it is (and is not)

Every task file a trusted writer creates carries one stamp line:

```
id: task-123
envelope_hmac: v1:34b18e…9ac7      ← HMAC-SHA256 over the rest of the file
timestamp: 2026-08-17T16:48:00Z
access_tier: owner
task: …
```

The stamp lets a consumer verify that the file's **privileged metadata**
(`access_tier` above all) has not been altered since a trusted writer sealed
it. The invariant it builds toward:

> Filesystem presence is not authority. Only authenticated promotion into the
> trusted mailbox grants a task eligibility for privileged execution.

Two companion invariants, permanent across all phases:

> **Verify-use identity.** The bytes interpreted for privileged execution
> MUST be the exact bytes whose HMAC was verified. A consumer MUST NOT
> verify by path and subsequently re-read that path for execution — the
> file can be replaced between the two reads, and the HMAC never fails
> because it was never re-checked. Read once → immutable in-memory bytes →
> verify AND parse/execute from those same bytes.

> **The seal authenticates a trusted writer's decision; it does not make
> producer-supplied claims authoritative.** HMAC proves a trusted writer
> sealed these bytes. It does not prove `access_tier` was correctly
> derived, the sender was correctly authenticated, or the policy decision
> was right — a buggy gateway seals its bug perfectly. Authoritative
> metadata must be DERIVED (transport-authenticated identity → authority
> derivation → policy → serialize → seal), never accepted from the
> producer and then sealed into authority.

**The precise security claim (current state):** only processes in the host
trusted-writer domain — those able to read the signing key — can generate a
task that verifies. A producer with *write access to `tasks/` but no key
access* (a remote sender, a different-UID process, anything reaching the
directory through a bridge) cannot mint `verified`, and cannot flip
`access_tier: guest` → `owner` on an existing sealed file without turning it
`invalid`.

**What it is NOT yet:** an intra-host privilege boundary. The key file is
0600, so the OS boundary is the UID — and on a single-user host every local
process (bridges, crons, and delegated `claude -p` subprocesses) shares that
UID and can read the key. Closing that gap is Phases 4–5 below, not a
property of the HMAC itself.

**Replay is a separate invariant.** Same key + same bytes = same MAC, so a
byte-identical copy of a sealed file re-verifies. Practical dedupe exists
(consumers check for an existing result before processing —
`_completed_result_exists`), but that is incidental, not a security
property. Execution uniqueness is Phase 3.

## Mechanism

- **Key**: 32 bytes as 64 hex chars at `<workspace>/state/auth/task-hmac.key`,
  mode 0600. Minted once by the first writer that needs it (`O_EXCL` create +
  `link()` first-writer-wins, so concurrent bridges cannot clobber each
  other). Never leaves the host. Both loaders reject a present-but-corrupt
  key loudly (exactly 64 hex chars / 32 bytes) rather than operating with a
  truncated key.
- **Stamping**: HMAC-SHA256 over the entire body (any previous stamp line
  stripped first), spliced into the **canonical slot** — line 1 directly
  after `id:`, or line 0 when there is no id header. Writers fail *open*: a
  stamping error produces an unstamped task (visible as `unsigned` in
  telemetry), never a malformed one.
- **Canonical slot**: a stamp-shaped line anywhere else in the body is user
  content and survives byte-identically. This removes the data-vs-control
  ambiguity — a message that *quotes* a stamp cannot confuse the verifier.
- **Verification** (`verify_text`) returns one of four verdicts:

```
             stamp?
           /        \
         no          yes
          |           |
      unsigned       key?
                   /      \
                 no        yes
                  |          |
           unverifiable   MAC match?
                          /        \
                       yes          no
                        |            |
                    verified      invalid
```

  `invalid` is positive evidence of tamper (or content change under an
  in-place stamp); `unverifiable` means no capacity to judge (keyless or
  corrupt-key host). The two must never be conflated: one is a security
  signal, the other an operational one. Note that an edit which *displaces*
  the stamp out of its canonical slot reads `unsigned`, not `invalid` —
  enforcement must therefore fail closed on `unsigned`/`unverifiable` too,
  never treat `invalid` as the only bad case.

- **Implementations**: `src/task_envelope.py` (stamp + verify) and
  `src/task_envelope.ts` (stamp only; verification stays Python). Wire-format
  parity is pinned by a cross-language test that stamps in TS and verifies
  through the real Python verifier.

## Key lifecycle (v1 contract)

**v1 = one active host key, no rotation protocol.** The stamp format
`v1:<mac>` carries no key id, so after a key replacement, previously sealed
files verify against the new key as `invalid` — indistinguishable from
tamper. Stated non-goals for v1, so the behavior is defined rather than
discovered: v1 does not provide historical verification across key
replacement, and does not provide compromise recovery. The compatible
upgrade path is a versioned stamp with a key id
(`envelope_hmac: v2:<key_id>:<mac>`, `key_id` → verification key), which can
coexist with v1 during a migration window.

## Current status — Phase 1 live

Every known writer on both lineages stamps: the remote gateway bridge,
discord-bridge, agent-api, voice-agent, cron-runner, the workstream
classifier, and the TS delegation seam + context-drop writers. Stamps are
**telemetry only** today: `src/task_envelope_census.py` counts
verified/unsigned so the unsigned population can be watched draining during
the soak window. No consumer changes behavior on a verdict yet.

## Phased plan

Two independent invariants, deliberately shipped separately so a failure in
one rollout cannot be confused with the other:

- **A — authenticity**: a privileged task must be `verified`.
- **B — uniqueness**: a terminal `task_id` must never execute again.

One-line intent per phase — *observe authenticity, enforce authenticity,
enforce execution uniqueness, make the trust transition explicit, make the
trust boundary OS-enforced*. The last two differ precisely: **Phase 4 makes
authority explicit in the architecture; Phase 5 makes that boundary
non-bypassable by the processes it constrains.**

| Phase | What ships | Status |
|---|---|---|
| 1 | HMAC telemetry: all writers stamp; census counts verdicts | **Live, soaking** |
| 2 | Privileged fail-closed on **authenticity only**: `verified` → eligible; `unsigned`/`invalid`/`unverifiable` → the fail-closed arms (no privileged processing / quarantine / fail closed). Enforces authenticity **relative to the current trusted-writer domain — it does not narrow that domain**: same-UID key readers can still mint `verified` until Phase 5 | After soak window, owner sign-off |
| 3 | Explicit replay ledger: `task_id` → terminal disposition (completed / rejected / expired / cancelled); a re-appearing terminal id gets a first-class `REPLAYED`/`ALREADY_TERMINAL` verdict. The id names an **execution identity**, not a filename — rename, move, or re-serialization never resets uniqueness. The ledger guarantees **execution-admission uniqueness, not exactly-once external effects** — a crash between side effect and ledger write still needs idempotency keys / outcome observation / OUTCOME_UNKNOWN reconciliation, the delivery runtime's existing problem class | Planned (order with 4 swappable) |
| 4 | `drop/` → trusted sealer → `ready/`: untrusted producers write `drop/` only; one sealer validates, binds identity, and constructs a **new sealed object** (never editing the drop file in place — untrusted inode ≠ trusted inode, so the producer can't mutate the object mid-seal), fsyncs if durability requires, then atomically renames into `ready/`. Directories encode lifecycle (`drop/` untrusted input, `ready/` authenticated, `archive/` completed history), inspectable with `ls` | Planned |
| 5 | Remove same-UID key exposure: the sealer alone reads the key; delegated subprocesses run without key or `ready/` access | Planned |

Phase 4/5 acceptance is mechanical — the capability matrix below is the
integration-test specification, each row an OS-enforced check, not a
convention:

```
delegated subprocess:  read task-hmac.key → DENIED
                       write ready/       → DENIED
                       write drop/        → ALLOWED
trusted sealer:        read task-hmac.key → ALLOWED
                       read drop/         → ALLOWED
                       write ready/       → ALLOWED
consumer:              read ready/        → ALLOWED
                       privileged exec    → only VERIFIED
```

Note the cwd-isolation work (spawn-cwd resolver) is Phase-5-*adjacent* only:
not inheriting the repo cwd reduces accidental coupling, but a same-UID
subprocess can still read the key at its absolute path. Phase 5's acceptance
is OS-enforceable capability separation, never "the subprocess doesn't know
where the key is."

The boundary is real only when three conditions hold simultaneously for an
untrusted process: cannot read the key, cannot write `ready/`, can only
write `drop/` — and their three complements hold for the sealer. Protecting
the key alone is insufficient: the authenticated namespace itself must be
protected against **create / replace / rename / mutate / delete**. The first
four are integrity/authority attacks (forge or alter what runs privileged);
delete is an availability attack (cannot forge privilege, can deny it) —
Phase 5 protects both, with different security consequences.

Four-layer threat model the phases build toward:

```
L0  remote surfaces (Matrix / Discord / API)
L1  untrusted local producers (delegated subprocesses, tools, scripts)
L2  trusted ingress / sealer            ← the only key-reader (Phase 5)
L3  privileged core execution
```

Authority rises only along: remote → `drop/` → validation + policy → HMAC
seal → `ready/` → privileged processing → replay ledger → side effects.

## Security properties and non-goals

Properties (phase in which each becomes true):

- **P1** Mutation of sealed bytes is detectable. *(Phase 1 — live)*
- **P2** Privileged execution requires `verified`. *(Phase 2)*
- **P3** A terminal execution identity cannot be admitted again. *(Phase 3)*
- **P4** Only the sealer can promote untrusted input. *(Phase 4)*
- **P5** Untrusted local processes cannot mint or modify authenticated
  input. *(Phase 5)*

Non-goals:

- **N1** HMAC does not encrypt task contents.
- **N2** HMAC does not establish freshness or prevent replay by itself.
- **N3** HMAC does not prove producer claims are truthful.
- **N4** v1 does not identify which trusted writer signed a task.
- **N5** Before Phase 5, HMAC does not defend against arbitrary same-UID code.
- **N6** Replay admission control does not imply exactly-once external
  effects.

## PR trail

| PR | What it shipped | State |
|---|---|---|
| #3014 | The envelope itself: `src/task_envelope.py` (key, canonical slot, four verdicts) + contract/falsifier suite | merged |
| #3030 | Remote gateway bridge stamps at the writer edge | merged |
| #3044 | Census (`src/task_envelope_census.py`) + remaining Python writer edges | merged |
| #3034 | Writer-edge stamping (agent-api, voice path) — note: its auto-merge raced its own review fix | merged |
| #3055 | Census read/stat TOCTOU fix (the #3034 race, recovered) | merged |
| #3058 | TS mirror `src/task_envelope.ts` + delegation-seam/context-drop stamping + cross-language parity tests + TS corrupt-key guard | open |
| #3065 | Python corrupt-key guard: loud error or `unverifiable`, never a zero-length key | open |
| #3070 | This document | open |

Related hardening from the same design thread: #3069 (Team-result guard
derives marker detection from the canonical grammar; withheld bodies persist
for owner review) and the staged spawn-cwd resolver (Phase-5-adjacent).

## Pointers

- Stamper/verifier: `src/task_envelope.py`, `src/task_envelope.ts`
- Census: `src/task_envelope_census.py`
- Contract + falsifier tests: `tests/task-envelope.test.py`,
  `tests/task-envelope-ts.test.ts`, `tests/task-envelope-census.test.py`
- Related boundary work: `docs/architecture-boundaries.md`; the
  delegated-subprocess cwd isolation (Phase-5-adjacent, see above).
