---
name: model-switch
description: "Change the core's model without the CLI: send /model to the live core's pane through the shared sender, wait for the CLI to accept it (answering its confirm dialog only under --confirm), record only once accepted; persistence is the CLI's own."
user-invocable: true
---

# model-switch

`scripts/switch-model.sh <model> [--dry-run] [--confirm] [--accept-timeout S]` — `<model>` is one of the
CLI's aliases (`default|opus|sonnet|haiku|fable`) or a `claude-*` id, either with an optional `[1m]`
tag (`default` takes none);
anything else is refused before any write (exit 2).

Order, under one lock per brain (`<workspace>/state/.model-switch.lock`, held across preflight, send,
observation and record so overlapping switches linearize): (1) preflight through the shared sender
(`scripts/tmux-send-line.sh --dry-run --refuse-if-pending`) — refuse on a Codex runtime (exit 4) or
when the core's input box carries text (exit 5), nothing written; exit 3 when no live pane exists;
(2) count the `Set model to` lines already on screen (`scripts/pane-observe.sh --count`), so a stale
acceptance from an earlier switch cannot pass as this one; (3) `/model <model>` + Enter through the
shared sender; (4) **wait for the CLI to accept**: a NEW `Set model to` line is the switch. A warm
core (cached conversation) first shows *"Yes, switch / No, go back"*; with `--confirm` — pass it on
an owner instruction, the flag is the authorization — the helper answers Enter once and waits again;
without it the dialog is cancelled (Escape) and the script exits 6 with nothing recorded. No
acceptance within `--accept-timeout` (default 20 s) is exit 8, nothing recorded. (5) Only then record
`<workspace>/state/model-switch.json` `{model, previous, previous_source, accepted, confirmed, ts, by,
settings_read}` — `previous` from the runtime's `settings.json`, else the last record (the CLI does not
persist every `/model` pick, so the file can lag the live session), else null.
**The script never writes `settings.json`: Claude Code's own `/model` persists the choice there**
(measured 2026-09-04: the key changed hours into a session with no repo writer). Measured the same
day on a live core: the send alone exited 0 while the CLI sat at the confirm dialog and the model had
not changed — which is why the send is treated as initiation and the record follows acceptance.

**`--offline` (core down).** With no core process for the session there is no CLI to take `/model`,
so `--offline` writes `model` into the runtime's `settings.json` (other keys kept) and records
`model-switch.json` with `accepted: false`; the core starts on it at its next launch. It is refused
(exit 9) while a `claude` process named for the session runs (the launcher's own test, from
`src/agent/claude/cli/session-launch.sh`; the tmux server, whose argv also names the session, is not
a core), and never overwrites a settings file it cannot read (exit 1). The launcher's provider-limit
hold uses it for the owner's "switch to <model>" reply.

**Held for a provider limit (no flag needed).** When no core process runs and
`src/core_limit_hold.py held` reports a hold in force, a plain switch (the desktop's Model picker)
takes the `--offline` write, releases the switch lock, and starts the core through
`src/agent/start-cli.sh` (`--launcher` overrides it), whose limit gate lets a launch on another
model family through. Exit 0: the core is starting on the new model; exit 10: the model is set but
the launcher did not start the core (its reason and log path on stderr). Without a hold, a down core
is never started from here.

**A picker left on screen.** Right before the send the script writes an attribution record through
`src/self_opened_gate.py` (`<workspace>/state/self-opened-gate.<session>.json`) and clears it once the
CLI accepted or the script cancelled its own dialog. On exit 8 the record stays, its claim window
closed a few seconds after the exit (a `/model` the owner opens after that is never attributed to the
script): if the send left a picker up (e.g. *"Enter to set as default · s to use this session only ·
Esc to cancel"*), the core supervisor (`src/core-input-watch.py`) presses Escape once after it has sat
unchanged for `MODEL_SWITCH_PICKER_DISMISS_AFTER_S` (manifest default 300; env, then
`--picker-dismiss-after`, override; 0 = never). A picker first seen outside the record's claim window, one someone is moving
through, a picker with no record (a human opened it) and any usage/spend gate are never touched; until
then the owner's chat card can still answer it.

Defaults come from the runtime descriptor (`sutando-config.sh runtime`: `brain`, `socket`,
`session`); `--brain/--socket/--session/--descriptor-file` and `SUTANDO_TMUX_*` are explicit
overrides.

Python comes from `sutando-config.sh python-bin`, never bare `python3`. The core-side entry
`scripts/switch-model.sh` only execs this script. An owner message like "switch model to opus" is
handled by running it; the dashboard's Quota tile shows the model the proxy then sees.

## The model card (`scripts/model_card.py`)

When the owner asks in their DM which model the core is on, or asks to change it without naming one,
post the menu: `python3 skills/model-switch/scripts/model_card.py post --room <the DM's room id>
[--reply-to <their message's event id>]` (`--print` shows it without posting). Its buttons are this
skill's `MODEL_SWITCH_CHOICES` (`manifest.json`, the list the menu-bar app offers), in that order and
under those titles; each carries a slug of its id (`sonnet-1m`), unique on the card, and the exact ids
the card was built with are kept by its event id (`<workspace>/state/model-card-choices.json`). A tap
is resolved through its own card's ids (`sonnet[1m]`), so it never changes the context size the list
names, and it acts only while the list still has that id. An entry whose id is not a model name (one
starting with `-`, say) or whose title is too long for a button is left out. The card names the model in
use, read only from `<workspace>/state/model-switch.json` and matched by exact id (an id the list
lacks is named as recorded and no button is marked); with no record it says "the default model". A
card holds six buttons: with a longer list the first six are shown and the text says to ask for the
others by name. With no readable list the menu is not posted (`{"status": "failed", "why": …}`: say
the `why`). It goes only to the owner's checked DM. `status` prints the same reading as JSON.

A tap is matched by the core's one command (CLAUDE.md, "Card clicks"); when it prints `run:
skills/model-switch/scripts/model_card.py reply`, run that with `--task-file <task file>`:

- `{"status": "started", "event_id": …}`: the switch was started detached (`switch-model.sh <id>
  --confirm --accept-timeout 180`; the CLI takes `/model` once this turn ends) and a card already says
  "Switching the core to X; it takes effect after this turn. I'll confirm here once it has switched.".
  Write `[no-send]` and end the turn. Without `event_id` the card could not be posted: send `say`.
- `{"status": "failed", "say": …}`: the switch could not be started; the card already says so.
- `{"status": "say", "say": …}`: the tapped model is the one in use ("I'm already using X. Nothing
  changed."); say that line.
- `{"status": "refused", "why": "That choice is no longer on my list. Ask me for the model card
  again."}`: the list changed since the card was posted, or the card's ids were not kept; nothing was
  started. Say the `why`.
- `forged_or_stale_click`, `owner_words`, `other_card`, `not_a_card`, `refused`, `failed`: as in
  `skills/agent-cards/SKILL.md`.

Each tap is one attempt with its own id (`attempt` in the output): the "switching" card is posted
before the switch is started, so the outcome can only follow it. The detached line always ends in
`model_card.py settle --room <DM> --model <model id> --attempt <id> --rc <the switch's exit code>`, which
saves the outcome in `<workspace>/state/model-card-outcome.json` and then posts it in the same DM: "Switched. I'm now using X." on exit 0, else "I
could not switch to X. I'm still using Y." and one short reason for the exit code (5 unsent text in the
input box, 8 not accepted in time, 3 the core is not running, and so on; an unknown code is "the switch
did not finish"). A post that fails is tried again a few times inside the detached step; an outcome
still untold is posted by the next `post` or `reply` before its own work (`delivered` in their
output). An outcome is told once, and no retry runs the switch again. `reply` records an attempt
before its "switching" card; an untold outcome with a newer attempt after it is marked `superseded`
in the record and never posted, so only the newest attempt's outcome stands for the card. The menu, the "switching" answer and the outcome carry one card id, and each newer
one retires the older one's buttons. Output of the detached line: `<workspace>/logs/model-card.log`.
