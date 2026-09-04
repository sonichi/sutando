---
name: model-switch
description: "Change the core's model without the CLI: record the switch, pin it in the runtime's settings.json for the next launch, and send /model to the live core's tmux pane."
user-invocable: true
---

# model-switch

`scripts/switch-model.sh <model> [--dry-run]` — `<model>` is one of the CLI's aliases
(`default|opus|sonnet|haiku|fable`) or a `claude-*` id with an optional `[1m]` tag; anything else
is refused before any write (exit 2).

Order and rollback, under one lock per brain (`<workspace>/state/.model-switch.lock`, held across
preflight, the transaction and the live send so overlapping switches linearize): (1) preflight — refuse
on a Codex runtime (exit 4) or when the core's input box carries text (exit 5), nothing written; (2)
the prior `settings.json` bytes are read and a new record `{model, previous, ts, by, settings}` is
STAGED beside `<workspace>/state/model-switch.json`; (3) `model` is pinned in `settings.json` (atomic
replace); a failed pin discards the staged record — the prior record is byte-intact; (4) the staged
record is committed; if that commit fails the settings are rolled back to the exact prior bytes, so a
pin can never exist unrecorded; (5) `/model <model>` + Enter is sent to the core pane. Exit 3 = pinned
and recorded, no live pane; exit 1 = nothing changed (or, on a rollback failure, the message says so).

Defaults come from the runtime descriptor (`sutando-config.sh runtime`: `brain`, `socket`,
`session`, runtime-authored and foreign-caller safe); `--brain/--socket/--session/--descriptor-file`
and `SUTANDO_TMUX_SOCKET`/`SUTANDO_TMUX_SESSION` are explicit overrides.

Python comes from `sutando-config.sh python-bin`, never bare `python3`. The core-side entry
`scripts/switch-model.sh` only execs this script. An owner message like "switch model to opus" is
handled by running it; the dashboard's Quota tile shows the model the proxy then sees.
