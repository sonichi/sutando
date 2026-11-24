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
  `escape_prompt`, `refresh_options`), filtered to what the core's runtime can carry out (below).
  `switch_models` are keys of the runtime's own model list (with `model_labels`, its words), the
  one in use and the capped one left out; `switch_runtimes` the other runtimes whose CLI is on
  this host; `menu` the on-screen menu's choices.
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
| Limit, refused turn (core) | Switch to <model> for each row of the runtime's own model list but the one in use and the capped one, Switch runtime, Retry now, Add another account, Refresh options, Restart core |
| Limit, refused turn (worker seat) | Retry now, Add another account, Refresh options; no model switch (below) |
| Limit menu (`/rate-limit-options`) | every choice the menu shows, in its own words, but a purchase or a model switch (keystrokes), Add another account, Refresh options; the core also gets Restart core |
| Paused by the relaunch hold | Retry now, Switch to <model>, Switch runtime, Add another account, Refresh options |
| Other blocked prompt (core) | Refresh options, Restart core |
| Crash loop (`core_crash_loop`) | the core: Restart core; a stopped service: none (only an app restart clears it) |

No card offers a purchase. `Open terminal` stays last, as the client-local action.

**The runtime's own options.** The model buttons are the rows of the runtime's own model picker
(`src/runtime_models.py`): Claude Code's `/model` menu or Codex's model list, read whenever it is
on the core's pane, recorded in `state/runtime-models.<runtime>.json`, and labelled as the runtime
labels them with the model and version each runs ("Sonnet 5 (1M context)", "Haiku 4.5"). The card
also names every row, marking the one in use and the one that hit its limit. The executor accepts
`switch_model:<key>` only for a row of that same list and sends the model that row runs through
`scripts/switch-model.sh`, so the button, the switch and the receipt name one model. No list read
yet means no model button; the card says to tap Refresh options. Refresh options (owner-initiated)
closes the card; the next tick reads the pane again and posts a new card, and on a core idle at a
limit refusal it types `/model` once, reads the picker on the next tick and dismisses it with
Escape (nothing chosen, no card for that picker). The monitor never types into the pane to read
options unless the owner asked. A menu choice is delivered by the keystroke driver into that same
menu: if the screen changed since the tap, nothing is typed. The record publishes the same lists:
`switch_models` (keys), `model_labels` and `menu` (the runtime's words, at most 8, never a
purchase).

A worker seat is never offered or run a switch: it shares the core's config dir, where the CLI's
`/model` saves the default for every session. The card, the notice and the reply say so in one
sentence (`hitl.recovery.SEAT_MODEL_LOCKED`). Codex rows are shown but not switchable yet.

A tap goes through the HITL reply handler (owner only, the card's own room, revision and guard)
to `state/recovery/actions/<hitl_id>.json`. The monitor watching that card's session runs it
(`src/recovery_actions.py`): once, within 10 minutes, only an id the card offered and the
runtime carries out, with fixed argv, never what a subprocess printed. It writes the receipt to
`state/recovery/receipts/<hitl_id>.json`, one line to `state/recovery/audit.jsonl` (who, what,
session, outcome) and edits the card in place. `add_account` and `switch_account` are refused
until the confirmed, re-authenticated sign-in flow exists; a worker seat's card never runs
`switch_model`, `restart_core` or `switch_runtime`.

Restart core runs detached from the monitor. On a desktop install (`$SUTANDO_APP_SUPPORT`, or a
checkout under the app's `engine/` directory) it runs the app's `sutando-ctl.sh restart-core`,
installed beside the checkout, which re-runs the app's own launcher: for Claude it replaces only
the core window, so the gateway keeps its session. In the app's own bundle
(`<app-support>/engine/<checkout>`) with that script missing, the tap is refused before anything
runs and the card says to restart the core from the desktop app: `start-cli.sh --restart` would
kill the gateway's session. Before using the ctl it runs `sutando-ctl.sh capabilities` (5 s): a
ctl that does not list `launch-lock` (an app older than the engine) is refused the same way, so a
restart never overlaps the app's own launcher. A standalone clone, even with
`$SUTANDO_APP_SUPPORT` exported, and any install without the app run
`src/agent/start-cli.sh --restart`; with an app dir configured the card says the standalone
restart was used and the audit adds `via_reason: not-bundle`. The audit line's `via` says which:
`ctl`, `ctl-missing`, `ctl-no-lock`, `start-cli` or `start-cli-fallback`. Switch runtime takes
the same route. Through the ctl it takes the app's launch lock itself (`SUTANDO_CTL_LAUNCH_LOCK_WAIT_S`,
default 60 s; busy is `rc: 12`, and a lock file that cannot be opened is refused, `rc: 13`; nothing
changed either way), writes the runtime as the desktop's selection (the
app's launcher reads it), runs the ctl inside that hold, and if the ctl does not exit 0 puts the
previous selection back (removes it when there was none) unless a newer choice replaced it
meanwhile. Elsewhere it runs `start-cli.sh --runtime <r> --restart`.

That route is `src/core_restart.py`, and every engine core restart takes it: these cards, the
paused core's retry (`core_limit_hold` relaunch), `src/agent/graceful-restart.sh` (exit 13 when
refused; a restart that did not happen releases its restart lock), `health-check.py --recover-core`
and `switch-model.sh` starting a core paused for a provider limit (`core_restart.py restart --start`:
the launcher with no flag outside the bundle; in it, under the launch lock, the ctl's `status` is
read and the app's `launch-sutando.sh` runs only when the core is down, while a core that is running
is left alone, `outcome: skipped`, `rc: 14`). `tests/core-restart-callers.test.py` fails on any
other `start-cli.sh --restart` caller, and on any other direct `start-cli.sh` / `launch-sutando.sh`
invocation. Both the ctl and standalone launcher run inside a
`core_restart.py` recorder, which appends two `state/recovery/audit.jsonl` lines:
`event: core_restart` with `phase: intent` before the command and `phase: outcome` (`done` or
`failed`, with its `rc`) after it. Each carries `caller`; a card's restart adds its `hitl_id` and
`revision`, while every automatic restart carries `actor: host-supervisor`. The two lines of one
attempt share an `attempt` id (`att_` and 12 hex digits), so overlapping attempts pair up. A line
that cannot be written is reported on the recorder's stderr, and an outcome whose intent was not
written carries `intent_recorded: false`. A caller that bounds its restart (the watchdog, the
paused core's relaunch, a card's detached restart) passes that bound to the route; the standalone
recorder then ends its launcher 20 seconds earlier, and also when it is itself told to stop
(SIGTERM, SIGHUP, SIGINT). In both cases the launcher is killed and reaped before the outcome is
written: `failed` with `reason: deadline` or `reason: cancelled` and no `rc`. That deadline is one
budget for the intent line, the launcher and the outcome line: each line waits at most 5 seconds
for the audit lock (less on a short budget), and a line that waits longer is reported as not
written. The recorder kills the launcher process itself, not commands the launcher had already
started. A foreground `core_restart.py restart` passes no bound, because its launcher may stay
attached to a terminal, and it alone leaves the launcher its stdin (`--foreground`); every other
route gives the launcher no stdin.
When the ctl exits 12 (another launcher run held the app's launch lock; nothing was killed or
started), the receipt is `failed`, the card says the core is already being restarted and to try
again shortly, any hold it lifted returns, and nothing is retried automatically.

A tap from anyone but the owner runs nothing, leaves the card as it was and posts nothing in the
room. On a card this store holds, tapped in the room the card was posted in (its requirement's
`room`, else its projection's), it adds one `state/recovery/audit.jsonl` line, `outcome: refused`
and `reason: not_owner`, with the `hitl_id`, the `action_id` (only an id the card offered, else
empty), the tapper's mxid as `sender` and that `room`. That line is written once per card and
tapper, so repeated taps add nothing. A tap from any other room, or on a card with no known room,
is not recorded; nor is a typed label from a non-owner, which is a message, not a tap. Every audit
writer (the bridge and the recovery executor, in either copy of `hitl/recovery.py`) reads, dedupes,
appends and trims under one lock on `state/recovery/audit.lock` (`file_lock.locked_file`: `flock`
on POSIX, `msvcrt` on Windows), so concurrent writers neither lose nor duplicate a line. A failed
trim keeps the appended line and is only logged. The audit feeds neither the card, the heartbeat nor `core_recovery.v1`: it is the local
record the owner or Sutando reads on the host.

**One card per incident.** After an action the card stays open with its outcome ("Switched the
core to Opus 5.5; checking that it is working again."). It closes, with "It is working again."
and "✓ Done.", only once the session is seen unblocked. Still blocked a minute after a done
action, the same card says so once. A later tick that still sees the block finds that card and
posts no other, and no action asks again until the state changes. While the monitor raises
cards, a paused seat or live core gets no text notice too: the card is the incident's one
surface. A relaunch-hold card whose retry time moves is edited in place.

Retry now on a live core or worker seat lifts its hold only for the restart and puts it back
(`core_limit_hold.reinstate`) when the restart fails, so the card says it is still paused. The
owner's bare "retry now" records the restart's dispatch before the detached child starts; the
child lifts the hold, runs the restart, records the outcome on the action and puts the hold back
on failure. A dispatch whose child never said it started within 60 seconds is reported once, and
the hold stays in force.

A held worker whose session is still alive is woken in place first: Retry now lifts its hold,
presses Escape and sends `continue` once the composer is idle, keeping its turn and context. Only
if the seat is still blocked `VERIFY_S` (60 s) later does the monitor fall back to the restart
below, and the same card says "Still blocked; restarting this worker." The receipt's `recovery.path`
and the audit line's `path` record which way it went: `esc`, `restart` (the session did not take
the key) or `restart-fallback`.

A worker's restart is its pool's `--retry-cmd` (`pool_manage.py retry`): the verb hands the restart to a detached child in its own session and returns, since the take-down
closes the input watcher's own session; the child never leaves the seat paused and tells the
owner when the restart did not happen. The card says "checking" until the seat is seen working;
after an action that did not report done, it says "The block has cleared.", never "It is working
again."

The bridge logs `heartbeat carried core_recovery.v1: state=<state> incident=<id>` once per change
of the record the broker accepted, so a live run can see the record leave the Mac.

## Contract fixtures

`contracts/core_recovery.v1/{heartbeat,heartbeat.codex,action,receipt}.json` are literal bodies, copied
byte-identical into the backend and the clients. The engine copy is the source of truth; the tests
assert the writer produces both heartbeat fixtures' `health.recovery` block, the HITL wire parses
`action.json`, and `receipt.json` uses the shared vocabulary.

Show the record as it would be written now: `python3 src/core_recovery_state.py show`.
