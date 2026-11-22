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

- `since` and `incident` stand while the state does; a limit pause takes both from its hold, so it
  stays one incident across monitor restarts.
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

## Contract fixtures

`contracts/core_recovery.v1/{heartbeat,heartbeat.codex,action,receipt}.json` are literal bodies, copied
byte-identical into the backend and the clients. The engine copy is the source of truth; the tests
assert the writer produces both heartbeat fixtures' `health.recovery` block, the HITL wire parses
`action.json`, and `receipt.json` uses the shared vocabulary.

Show the record as it would be written now: `python3 src/core_recovery_state.py show`.
