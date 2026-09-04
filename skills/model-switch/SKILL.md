---
name: model-switch
description: "Change the core's model without the CLI: record the switch and send /model to the live core's tmux pane through the shared sender; persistence is the CLI's own."
user-invocable: true
---

# model-switch

`scripts/switch-model.sh <model> [--dry-run]` — `<model>` is one of the CLI's aliases
(`default|opus|sonnet|haiku|fable`) or a `claude-*` id with an optional `[1m]` tag; anything else
is refused before any write (exit 2).

Order, under one lock per brain (`<workspace>/state/.model-switch.lock`, held across preflight,
record and send so overlapping switches linearize): (1) preflight through the shared sender
(`scripts/tmux-send-line.sh --dry-run --refuse-if-pending`) — refuse on a Codex runtime (exit 4) or
when the core's input box carries text (exit 5), nothing written; (2) record
`<workspace>/state/model-switch.json` `{model, previous, ts, by, settings_read}`, with `previous`
read from the runtime's `settings.json`; (3) `/model <model>` + Enter through the shared sender.
**The script never writes `settings.json`: Claude Code's own `/model` persists the choice there**
(measured 2026-09-04: the key changed hours into a session with no repo writer), so a switch
survives a restart because the CLI made it, not this script. Exit 3 = recorded, no live pane.

Defaults come from the runtime descriptor (`sutando-config.sh runtime`: `brain`, `socket`,
`session`); `--brain/--socket/--session/--descriptor-file` and `SUTANDO_TMUX_*` are explicit
overrides.

Python comes from `sutando-config.sh python-bin`, never bare `python3`. The core-side entry
`scripts/switch-model.sh` only execs this script. An owner message like "switch model to opus" is
handled by running it; the dashboard's Quota tile shows the model the proxy then sees.
