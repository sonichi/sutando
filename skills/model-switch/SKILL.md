---
name: model-switch
description: "Change the core's model without the CLI: record the switch, pin it in the runtime's settings.json for the next launch, and send /model to the live core's tmux pane."
user-invocable: true
---

# model-switch

`scripts/switch-model.sh <model> [--dry-run]` — `<model>` is one of the CLI's aliases
(`default|opus|sonnet|haiku|fable`) or a `claude-*` id with an optional `[1m]` tag; anything else
is refused before any write (exit 2).

Three effects, in this order: (1) `<workspace>/state/model-switch.json` records `{model, previous,
ts, by, settings}` — written FIRST, so a pin can never exist unrecorded (the #2742 lesson: a pin
nothing could see became a 17-day downgrade); (2) `model` is pinned in the runtime's
`settings.json`, resolved by `sutando-config.sh claude-home-path`, which the launcher inherits
(it passes no `--model`); a failed pin removes the record it would have described; (3) `/model
<model>` + Enter is sent to the core's tmux pane, socket from `sutando-config.sh tmux-socket`
(or `SUTANDO_TMUX_SOCKET` / `--socket`), session `sutando-core` (or `SUTANDO_TMUX_SESSION` /
`--session`). Exit 3 = recorded and pinned, no live pane found; exit 1 = nothing changed.

Python comes from `sutando-config.sh python-bin`, never bare `python3`. The core-side entry
`scripts/switch-model.sh` only execs this script. An owner message like "switch model to opus" is
handled by running it; the dashboard's Quota tile shows the model the proxy then sees.
