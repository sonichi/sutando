# Core recovery record (`core_recovery.v1`)

What the owner's phone, web client or another computer needs to show the core's real state and
offer the fix that applies, without access to the Mac. Code: `src/core_recovery_state.py`; tests:
`tests/core-recovery-state.test.py`; wire fixtures: `contracts/core_recovery.v1/`.

## Who writes it

`src/core_recovery_state.py` is the only writer of `<workspace>/state/core-recovery.json`. The
core's monitor (`src/core-input-watch.py`) calls `update()` on every tick; a worker seat's monitor
does not write it. `derive()` is pure and folds records other modules already keep:

| Input | Owner |
|---|---|
| `state/core-supervisor.json` | the monitor (`core-input-watch.py`) |
| `state/core-limit-hold.json` | `core_limit_hold.py` |
| `state/supervisor-status.json` | the desktop's backend supervisor |
| `state/quota-state.json` | the credential proxy |
| the pane's abnormal text | `cli_wedge.frame_abnormal` |
| `state/session-starts.log` | the core launchers (launches on this host in the last 10 minutes) |

The file is written atomically (`workspace_default.write_status`), at once on a change and every
60 s while unchanged, so its age says whether the monitor still runs. A reader treats a record
older than 5 minutes, larger than 4 KiB or outside the schema as absent.

## The record

```json
{"v": 1, "incident": "inc_1026786a", "state": "paused_limit", "since": 1763730000,
 "reset_at": 1763737200, "runtime": "claude", "model": "opus", "account": "acct_3f9a2b",
 "waiting_tasks": 2, "restarts_10m": 0,
 "actions": ["retry_now", "switch_model", "switch_runtime", "restart_core"],
 "switch_models": ["sonnet", "haiku"], "switch_runtimes": ["codex"]}
```

| `state` | When |
|---|---|
| `running` / `idle` | the monitor reads a turn in flight / the idle prompt (or a gate it answers itself) |
| `paused_limit` | a usage-limit screen or refusal, or a relaunch hold in force |
| `out_of_credits` | the same, when the refusal names an exhausted balance (pane text, the wedge banner, or a recent proxy rejection) |
| `signed_out` | logged out, or sitting at the sign-in prompt |
| `blocked_prompt` | any other prompt that needs a person |
| `crash_loop` | the core is down after 4 launches in 10 minutes, or the backend supervisor spent a service's recovery budget |
| `starting` | the core is down and will be revived |
| `unknown` | nothing reliable to read (hung, unobserved, gateway down, no monitor record) |

`offline` is never written: the server derives it from missing heartbeats.

- `since` stands while the state does and the last record is fresh (at most 5 minutes old);
  `incident` is always derived from the state and `since` (a limit pause: from its hold), never
  copied from a stored record, so a pause stays one incident across monitor restarts.
- Every identifier has one shape, enforced on write and on read: `incident` is `inc_` + 8 hex,
  `account` is `acct_` + 6 hex, `model` is one of `opus`, `sonnet`, `haiku`, `fable`, `default`.
  A value of any other shape is dropped, not cut. Numbers are finite, non-negative and at most
  2^53; anything else (NaN, infinities, booleans) is dropped.
- A field the inputs cannot supply is left out, never defaulted (`reset_at` only when the provider
  gave one).
- `actions` come from a fixed set (`retry_now`, `switch_model`, `switch_runtime`, `restart_core`,
  `escape_prompt`), filtered to what the core's runtime can carry out (below).
  `switch_models` lists the runtime's own aliases without the running and the limited family;
  `switch_runtimes` the other runtimes whose CLI is on this host.
- No prompt text, path, token, model output or account identity: `account` is a 6-hex prefix of
  the hold's account digest.

## Runtimes

`runtime` is `claude` or `codex`: the session's own stamp, else the launcher's
`state/core-runtime.json`. The states are the same for both; the inputs and actions differ.

| | Claude Code | Codex |
|---|---|---|
| running, idle, signed out, blocked prompt, crash loop, starting | monitor states | the same monitor states (shared pane classifier, Codex picker rows) |
| paused at a limit | limit screen, refused turn, or relaunch hold | the limit banner under the last answer (`codex_tail`) while the prompt is idle |
| out of credits | credit text on the pane or in the proxy record | credit text on the pane |
| `reset_at`, `account`, `model` | from the hold and the launch identity | not read (no Codex hold or identity record yet) |
| actions | all five | `switch_runtime`, `restart_core`, `escape_prompt` |

An unknown runtime gets `restart_core` and `escape_prompt` only.

## On the heartbeat

The gateway bridge sends the record as `health.recovery` with the `core_recovery.v1`
capability, on the core's lanes only; the wire rules are in
[the gateway protocol](remote-gateway-protocol.md).

## The shared vocabulary

`parse_action_id(action_id, runtime)` is the allowlist for the owner's click (`action.json`:
`retry_now`, `restart_core`, `escape_prompt`, `switch_model:<alias>`, `switch_runtime:<runtime>`,
`switch_account:<id>`); the id is matched whole, and an id the runtime cannot carry out, an argument
not in the host's own list (aliases, runtimes, stored accounts), or any unknown id parses to None.
`switch_account` is in `STEP_UP_ACTIONS`: never offered in the record, and confirmed by the owner
before it runs.
`RECEIPT_OUTCOMES` (`done`, `refused`, `stale`, `failed`) is the receipt's (`receipt.json`).
The grammar itself lives in `src/hitl/recovery.py` (vendored into ag2-sparrow, since the bridge's
reply handler parses the click); `parse_action_id` adds the runtime's and the host's lists to it.

## The buttons and who runs them

A blocked core's or worker seat's card (`hitl.tui_gate`, built by `core-input-watch.py`) carries
the recovery actions for its state, picked by `recovery_actions.card_controls`:

| Card | Buttons |
|---|---|
| Limit, refused turn | Switch to <model> (the runtime's list, minus the model in use and the capped one), Retry now, Add another account, Add usage credits; the core also gets Switch runtime and Restart core |
| Limit menu (`/rate-limit-options`) | Wait for reset, Continue automatically at <time> (keystrokes), Add another account, Add usage credits; the core also gets Restart core |
| Paused by the relaunch hold | Retry now, Switch to <model>, Switch runtime, Add another account, Add usage credits |
| Other blocked prompt (core) | Restart core |
| Crash loop (`core_crash_loop`) | the core: Restart core; a stopped service: none (only an app restart clears it) |

`Add usage credits` (`buy_credits`) has its own action kind, `purchase`, and is the only text on
the card that names a purchase, so a client that must not show one drops exactly that button. The
relay's `space.ag2.agent_alert` carries `has_non_purchase_actions: true` for a hard block.
`Open terminal` stays last, as the client-local action.

A tap goes through the HITL reply handler (owner only, the card's own room, revision and guard)
to `state/recovery/actions/<hitl_id>.json`. The monitor watching that card's session runs it
(`src/recovery_actions.py`): once, within 10 minutes, only an id the card offered and the
runtime carries out, with fixed argv, never what a subprocess printed. It writes the receipt to
`state/recovery/receipts/<hitl_id>.json`, one line to `state/recovery/audit.jsonl` (who, what,
session, outcome) and edits the card in place. `buy_credits`, `add_account` and `switch_account`
are refused until the confirmed, re-authenticated sign-in flow exists; a worker seat's card never
runs `restart_core` or `switch_runtime`.

Retry now on a live core or worker seat lifts its hold only for the restart and puts it back
(`core_limit_hold.reinstate`) when the restart fails, so the card says it is still paused and a
second tap counts. The owner's bare "retry now" records the restart's dispatch before the
detached child starts; the child lifts the hold, runs the restart, records the outcome on the
action and puts the hold back on failure. A dispatch whose child never said it started within 60
seconds is reported once, and the hold stays in force.

## Contract fixtures

`contracts/core_recovery.v1/{heartbeat,heartbeat.codex,action,receipt}.json` are literal bodies, copied
byte-identical into the backend and the clients. The engine copy is the source of truth; the tests
assert the writer produces both heartbeat fixtures' `health.recovery` block, the HITL wire parses
`action.json`, and `receipt.json` uses the shared vocabulary.

Show the record as it would be written now: `python3 src/core_recovery_state.py show`.
