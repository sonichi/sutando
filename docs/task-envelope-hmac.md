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
byte-identical copy of a sealed file re-verifies — and there is **no general
replay protection before Phase 3**. The live watcher
(`src/watch-tasks-stream.sh`) dispatches every task file it observes without
consulting `results/`. The one result-existence check that exists is the
Stop-hook sweep in `src/check-pending-tasks.sh`, which skips a task whose
result file is already present — a narrow re-prompt heuristic on that single
path, not an execution-admission guard. Execution uniqueness is Phase 3.

## Mechanism

- **Key**: 32 bytes as 64 hex chars at `<workspace>/state/auth/task-hmac.key`,
  mode 0600, never leaving the host. Both writers mint it via temp file +
  `link()` first-writer-wins, which publishes only complete bytes — a reader
  can never observe a partially written key. Python does this in
  `src/task_envelope.py`; TS does the same in `src/task_envelope.ts`
  (complete bytes land in a `wx`-flagged temp file, `linkSync` publishes
  atomically; a losing concurrent creator reads the winner's key). Both
  loaders additionally reject a present-but-corrupt key loudly (exactly
  64 hex chars / 32 bytes) rather than operating with a truncated key;
  shipped in #3058 (TS) and #3065 (Python), both merged 2026-08-18.
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

## Current status — Phase 1 partial (telemetry live, writer coverage incomplete)

**Inventory scope:** every production code path on current `main` that
publishes a file into `tasks/` — creation or re-publication under a new id —
enumerated from call sites, not from the PR trail. Consumers that only read,
move, or archive task files are out of scope; nothing else is scoped out.

**Stamping on main** (call the stamper at their writer edge): the remote
gateway bridge (which also injects the stamper into the vendored
`local_task_protocol` seam via `set_task_stamper`), discord-bridge,
telegram-bridge, slack-bridge, cron-runner, the workstream classifier
(`src/task_workstreams.py`), and the TS delegation lineage
(`task-bridge.ts`, `task-delegation.ts`).

**Not yet stamping — unsigned writer edges on current `main`, no PR:**

| Writer edge | Site(s) | Notes |
|---|---|---|
| `src/agent-api.py` | `:1016`, `:1051`, `:1078`, `:1325`; answer-injection rewrite at `:1230` | writes `task_content` directly |
| `src/github-webhook.py` | `:191` | external events, `access_tier: other` |
| `src/web-client.ts` | `:4433` | owner-tier scan trigger from the web UI |
| `skills/phone-conversation/scripts/conversation-server.ts` | `:422` delegated task; `:1171` call summary; `:1567` meeting approval | phone tasks, owner or other tier. Three separate writer edges in one file — none references the stamper |
| `src/inline-tools.ts` | `:710` | voice `CANCEL_INSTRUCTION` writer, owner tier — the voice-agent process's remaining unsigned edge (ordinary voice delegation stamps via `task-delegation.ts`) |
| `src/health-check.py` | `emit_task_for_failures` → `local_task_protocol.write_task_file` | the seam stamps only in a process that injected a stamper; only the gateway bridge does, so health tasks are unsigned |
| `skills/schedule-crons/scripts/codex-scheduler.py` | `_enqueue()` `:264-269` | scheduled-cron tasks, owner tier. Installed and reconciled by `src/agent/codex/cli/start-cli.sh:226`, so it is live whenever the Codex runtime is selected |
| `src/dedup_recovery.py` | `:90`, via `build_requeued_task` (`src/result_markers.py:393-430`) | re-publishes under a NEW id. The rewrite copies every unrecognised header through verbatim, so an existing `envelope_hmac:` is PRESERVED while `id:` changes. The MAC covers the whole text minus the stamp line (`task_envelope.py:115-117`), so a sealed original verifies `invalid`, not `unsigned` — only an unsigned original stays `unsigned`. Same rewrite reached from `src/discord-bridge.py`'s sibling re-ask path |
| `src/Sutando/main.swift` | context-drop `writeTask` (~`:2020`) | desktop hotkey task; no Swift stamper implementation exists |

Until each edge stamps (or is explicitly scoped out with a recorded
rationale), tasks from those writers are `unsigned` — except the
re-publication edge above, which yields `invalid` whenever the original was
sealed. `unsigned` and `invalid` are different telemetry facts and a census
that reports only the first will undercount. **Phase 1 is complete
when every in-scope writer edge stamps** — not when the PR trail has no open
rows, and not when telemetry is running. Telemetry went live first; writer
coverage is the incomplete half, and the census's unsigned count is its live
measure.

Stamps are **telemetry only** today: `src/task_envelope_census.py` counts
verified/unsigned so the unsigned population can be watched draining during
the soak window. No consumer changes behavior on a verdict yet. Phase 2
enforcement must not be enabled until this inventory reads clean — flipping
fail-closed against an incomplete ledger would reject owner tasks arriving
through the health, phone, web, and voice-cancel paths above.

**Read that count with its window in mind.** The default invocation is
`python3 src/task_envelope_census.py --days 7`, and it scans `tasks/` **and**
`tasks/archive/`. So the unsigned population is dominated by *history* — tasks
written before each writer's stamper merged — not by uncovered writers. Run bare,
it shows a large unsigned count that looks like a coverage gap and is mostly
backlog. To tell them apart, compare a row's **newest** unsigned timestamp against
that writer's stamper merge date: if the newest unsigned predates the stamper, the
row is draining, not uncovered. Worked example from a second host — newest unsigned
`discord` task 2026-08-15T20:27Z against the stamper landing 2026-08-17T12:20Z
(#3014), i.e. backlog.

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
| 1 | HMAC telemetry: every writer edge stamps; census counts verdicts | **Partial — telemetry live and soaking; writer coverage incomplete (see the unsigned-edge table above)** |
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

- **P1** Mutation of sealed bytes is detectable. *(Phase 1 — live for
  stamped writer edges; unstamped edges emit `unsigned`, which carries no
  mutation evidence)*
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

Author identity is two-part: the GitHub handle (`qingyun-wu`, shared by
the fleet) plus the acting agent's mxid, matching each PR body's
`owner-identifier` line.

| PR | What it shipped | Author | State |
|---|---|---|---|
| #3014 | The envelope itself: `src/task_envelope.py` (key, canonical slot, four verdicts) + contract/falsifier suite + the Discord and gateway writer edges | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3034 | Census (`src/task_envelope_census.py`) — soak-window telemetry; note: its auto-merge raced its own review fix | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3044 | Workstream classifier stamps at its writer edge | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3046 | telegram-bridge + slack-bridge stamp at their writer edges | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3055 | Census read/stat TOCTOU fix (the #3034 race, recovered) | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3058 | TS mirror `src/task_envelope.ts` + delegation-seam/context-drop stamping + cross-language parity tests + TS corrupt-key guard | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3065 | Python corrupt-key guard: loud error or `unverifiable`, never a zero-length key | qingyun-wu (@sutando-qingyun-001:ag2.space) | merged |
| #3070 | This document | qingyun-wu (@sutando-qingyun-001:ag2.space) | open |

Reviewer/approver identities are the GitHub accounts on each PR (sonichi,
yixuan-ag2, john-the-dev, keweichen — individually held, not shared); the
corrupt-key finding driving #3058/#3065 was @qingyun-air.agent:ag2.space's
verified report.

Related hardening from the same design thread: #3069 (Team-result guard
derives marker detection from the canonical grammar; withheld bodies persist
for owner review — **closed unmerged 2026-08-19, so this hardening is not on
main**) and the staged spawn-cwd resolver (Phase-5-adjacent).

## Pointers

- Stamper/verifier: `src/task_envelope.py`, `src/task_envelope.ts`
- Census: `src/task_envelope_census.py`
- Contract + falsifier tests: `tests/task-envelope.test.py`,
  `tests/task-envelope-ts.test.ts`, `tests/task-envelope-census.test.py`
- Related boundary work: `docs/architecture-boundaries.md`; the
  delegated-subprocess cwd isolation (Phase-5-adjacent, see above).
