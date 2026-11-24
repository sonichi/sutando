---
name: runtime-switch
description: "The core's runtime card in the owner's DM: which runtime the core runs on (Codex or Claude Code) and a button to switch to the other. Checks the target is signed in first, restarts the core onto it after the current turn, and confirms in the DM. Owner-only, DM-only."
user-invocable: true
---

# runtime-switch

Use it when the **owner, in their DM**, asks to switch the core's runtime: "switch to Claude Code",
"use Codex", "run on Codex", "换成 Claude Code", "切到 Codex", or asks which runtime the core is on
and wants to change it. Anyone else, or any other room: say the runtime is switched from the owner's
DM and do nothing. Never run `start-cli.sh --runtime …` or `--restart` yourself from inside the core.

```bash
python3 skills/runtime-switch/scripts/runtime_card.py post --room <owner DM> [--reply-to <event>]
python3 skills/runtime-switch/scripts/runtime_card.py post --room <owner DM> --print   # the card, not sent
python3 skills/runtime-switch/scripts/runtime_card.py status                          # JSON: in use, next start, confirmed core
```

Post the card even when the owner already named the runtime: the switch happens on the tap.

## The card

Two buttons, **Codex** and **Claude Code**; the runtime in use is the primary one and is named in the
text. The note says what carries over: tasks, results, state, memory and the workspace are shared by
both runtimes; only the live conversation does not.

## A tap

`src/agent_card.py click` routes a tap to `runtime_card.py reply` (the recorded handler). Only the
owner's tap, in the DM the card was posted in, on a card this agent posted, acts
(`agent_card.click_verdict`). A tap on the runtime in use changes nothing; a second switch while one
is under way is refused.

1. **Sign-in first, before anything stops.** Claude Code: `claude auth status --json` under the core's
   config dir; Codex: `src/codex_account.py` on the core's Codex home (the account card's probes).
   Not signed in, or not readable: the card answers **"Sign in first"** with that runtime's sign-in
   controls in the card itself (drawn by the desktop app on the computer that runs the core; elsewhere
   the card says to open it there). Nothing restarts. The tap is kept: `runtime_card.py await-sign-in`
   waits up to 15 minutes, and once the sign-in is there it switches without being asked again.
2. Signed in: it answers "Switching the core from X to Y…" at once and starts `runtime_card.py switch`
   detached, in its own session, because the core restarts.
3. `switch` runs `src/agent/graceful-restart.sh -- --runtime <rt>`: it waits for the core's current
   turn, then `start-cli.sh --restart --runtime <rt>`. The launcher writes the desktop's selection
   (`runtime.json` `coreRuntime`) and `<workspace>/state/core-runtime.json` only once the new core's
   panes run that CLI.
4. `settle` confirms from those two, never from a heartbeat alone: the record names the target with a
   session created after the switch began, and the runtime the launcher picks next is the target. It
   posts "Switched. The core is now running on Y." or what could not be confirmed. When graceful-restart
   did not stop the core (exit 3, 4, 5) it says the core is still on X.

One card, updated in place: the "switching" answer edits the tapped card's own event
(`room.message.edit`, so it then shows "Runtime: Switching…" in place of the card), and the
outcome edits that same event again (`switch --card <event>`). A tap adds no message. The "Sign in
first" card is the one card still posted, since its sign-in controls cannot ride in an edit; the
waiter then edits it (to "switching", to "stopped waiting", or, when the core is already on that
runtime because it was switched another way, to say so). Where an edit is refused, the answer is
posted as a card, as before.

Its log is `<workspace>/logs/runtime-card.log`.

A restart that is put off because another restart is under way is not a failure: the card says it is
waiting, the switch waits for that restart's lock to clear (10 minutes at most) and asks once more,
then the same card says "Switched" or that it is still busy.

On a desktop-hosted engine the restart replaces the tmux session the desktop keeps its gateway window
in, so after its own restart the switch runs the desktop's `sutando-ctl.sh restart-gateway` (the script
beside the engine). If that fails, the outcome on the card says messages may not reach the core.

The sign-in waiter runs in a session of its own and holds a lock for as long as it lives. If something
stops it anyway (a restart that kills every process of the core, a reboot), `runtime_card.py resume`
starts one again for the sign-in still waited for; `post` and `status` do the same on their way.
