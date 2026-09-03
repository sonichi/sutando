# scheduled-panel

Publishes this host's durable schedule as the Room Context doc the AG2 Space
"Scheduled" Activity tab reads (`folder=activity`, `name=SCHEDULE.md`).

**Status: a manual utility.** Nothing in the repo invokes it on its own; the
agent (or a cron entry the owner adds) runs it. It does not change any schedule.

## Usage

```bash
python3 skills/scheduled-panel/publish_schedule.py                 # print the markdown
python3 skills/scheduled-panel/publish_schedule.py --json          # rows as JSON
python3 skills/scheduled-panel/publish_schedule.py --publish --room '!room:ag2.space'
```

`--publish` needs the gateway credentials `skills/agent-room-ops` already uses
(`REMOTE_TASK_TOKEN`). Verify a publish by reading it back:

```bash
python3 skills/agent-room-ops/room_ops.py doc get '!room:ag2.space' --folder activity --name SCHEDULE.md
```

## What each column means

| Column | Source |
|---|---|
| Schedule | the cron expression, humanised only when it has no calendar restriction |
| Fires via | `dashboard_schedules.schedule_owner`: session, launchd, codex, dynamic-loop |
| Last fired | the owning scheduler's own record: codex `state/schedules/codex-scheduler.json` `last_scheduled_slot`, launchd `state/cron-runner-state.json`, dynamic loop `.alive` mtime; session crons have no record and show `—` |
| Next fire | `dashboard_schedules.next_run_for_job`, evaluated in the job's zone (codex: its `timezone`, default America/Los_Angeles; others: host-local), rendered in UTC |

Cron semantics come from `src/cron_eval.py`, the evaluator the launchd runner
and the codex scheduler fire on, so the panel cannot disagree with them.

## To publish on a schedule

Add a launchd-owned shell entry to `hosts/<host>/crons.json`, for example:

```json
{"name": "scheduled-panel-refresh", "cron": "7 */6 * * *", "launchd": true,
 "shell_command": "python3 skills/scheduled-panel/publish_schedule.py --publish --room '!room:ag2.space'"}
```
