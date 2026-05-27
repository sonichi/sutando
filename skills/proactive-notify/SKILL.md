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

## Where config lives (data-vs-code split)

The repo carries only **templates** (`config/*.example`) and code; the live per-user config + state live under the workspace:

```
Repo (shared, git-tracked):
  skills/proactive-notify/
    config/pings.yaml.example
    config/channel-policy.yaml.example
    scripts/*.py
    SKILL.md

Workspace (per-user, NOT git-tracked):
  $SUTANDO_WORKSPACE/skills/proactive-notify/
    pings.yaml             ← edit your pings here
    channel-policy.yaml    ← edit your escalation policy here
    state/fired.json       ← runtime dedup state
```

The runner bootstraps the workspace copy from `.example` template on first run, then reads workspace from then on.

## Adding a ping

Edit `$SUTANDO_WORKSPACE/skills/proactive-notify/pings.yaml`. Each entry needs: `name`, `source`, `match`, `urgency`, `body_template`. Optional: `voice_natural`, `prefer_channel`, `quiet_hours_override`.

## Adding a source / action

Drop a new module in `scripts/sources/<name>.py` or `scripts/actions/<name>.py` with the contract documented in their `__init__.py`. The runner picks them up by `import_module` of the `source:` / channel name.

**No personal literals in code**: per `feedback_user_config_in_workspace`, source/action code must be generic. Use `os.environ.get(K) or shutil.which(K) or "default"` for binaries; pull owner identifiers from `.env`; never hardcode owner phone / Discord ID / personal path in `if` checks.

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
