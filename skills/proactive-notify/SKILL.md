---
name: proactive-notify
description: Sutando-initiated proactive pings to the owner. Declarative pings.yaml registry; runner cron picks the channel via escalation policy (presence + urgency + quiet-hours). Pluggable sources + actions.
---

# proactive-notify

Owner-facing skill that gives Sutando a single, opinionated way to reach out: declare a ping ("when X happens, tell me Y"), and the runner picks the best channel based on owner presence + urgency + quiet-hours.

This is the **Sutando → owner** direction of communication. The task-bridge handles the other direction (owner → Sutando). `proactive-loop` decides what to do; `proactive-notify` decides whether/how to interrupt about it.

## Design

Full design: `<workspace>/notes/proactive-notify-design.md`.

Three concepts:
- **Ping** — one event that wants to reach the owner. Carries `urgency` (critical/important/fyi), `voice_natural`, `body`, `dedup_key`.
- **Source** — where the trigger comes from (calendar, github, gmail, vercel-webhook, …). Pluggable under `scripts/sources/`.
- **Action** — how the delivery actually happens (sms, call, discord-dm, voice, macos-notif, queue). Pluggable under `scripts/actions/`.

The **channel router** sits between them: given a Ping + the current presence snapshot, pick the action.

## Usage

The runner is driven by cron, not invoked by hand:

```bash
python3 skills/proactive-notify/scripts/runner.py             # dry-run (default ON)
python3 skills/proactive-notify/scripts/runner.py --live      # actually deliver
python3 skills/proactive-notify/scripts/runner.py --once      # one pass then exit (cron default)
```

Cron entry (auto-added by `/schedule-crons` once `crons.json` has the entry below):

```json
{
  "name": "proactive-notify-runner",
  "cron": "*/3 * * * *",
  "prompt": "Run python3 skills/proactive-notify/scripts/runner.py --once to fire any due Pings to the owner."
}
```

## Adding a ping

Edit `config/pings.yaml`. Each entry needs: `name`, `source`, `match`, `urgency`, `body_template`. Optional: `voice_natural`, `prefer_channel`, `quiet_hours_override`.

## Adding a source / action

Drop a new module in `scripts/sources/<name>.py` or `scripts/actions/<name>.py` with the contract documented in their `__init__.py`. The runner picks them up by `import_module` of the `source:` / channel name.

## Default escalation rules

`config/channel-policy.yaml` defines:
- Quiet hours: 23:00–07:00 PT — non-critical pings queue for morning briefing.
- Default channel per urgency: critical → call, important → sms, fyi → queue.
- Overrides (first match wins): presenter-mode-mutes-call, voice-connected-prefers-voice, owner-active-in-discord-last-5min, in-quiet-hours-downgrades.

Critical pings bypass quiet-hours by default. Per-ping `quiet_hours_override: false` opts out.

## State

- `state/fired.json` — dedup map `{dedup_key: iso_timestamp}`. Never re-fire.
- `state/muted.json` — per-ping mute overrides (not in MVP; future CLI `proactive-notify mute <name>`).

## What this does NOT replace

- `morning-briefing` — scheduled daily aggregate, owner-initiated. proactive-notify is event-driven Sutando-initiated.
- `pending-questions.md` — questions needing structured owner decision. proactive-notify pings about state, doesn't ask.
- task-bridge bridges — those handle owner → Sutando. proactive-notify is the other direction.
- Other skills' notifications — they keep calling SMS/DM/call directly when they have a specific reason. proactive-notify is the opinionated default.
