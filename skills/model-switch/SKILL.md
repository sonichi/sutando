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
