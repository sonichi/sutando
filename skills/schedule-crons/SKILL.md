# Schedule Crons

Re-create all session cron jobs for Sutando. Run this on startup or after a session restart.

**Usage**: `/schedule-crons`

## How It Works

Jobs are defined in `skills/schedule-crons/crons.json` (gitignored — personal). A template is in `crons.example.json` — copy it on first setup:
```bash
cp skills/schedule-crons/crons.example.json skills/schedule-crons/crons.json
```

Each entry has:
- `name` — unique identifier (used to avoid duplicates)
- `cron` — 5-field cron expression
- `prompt` — the prompt to run (direct text)
- `prompt_skill` — OR a skill to invoke (e.g. "morning-briefing" → `/morning-briefing`)

## On Activation

1. Read `skills/schedule-crons/crons.json`
2. Check existing cron jobs with CronList
3. For each job in the config:
   - Skip if a job with matching prompt/name already exists
   - If `prompt_skill` is set, invoke it as `/skill-name`
   - Call CronCreate with the cron expression and prompt
4. Ensure a task watcher is running. The canonical pattern is the `Monitor` tool streaming `bash src/watch-tasks-stream.sh` (started by the CLI session itself, not by this skill). If `pgrep -f watch-tasks` finds nothing, prompt the CLI to start it — don't kick off `bash src/watch-tasks.sh` (retired 2026-05-14).
5. Confirm what was scheduled

## Adding New Crons

Edit `crons.json` to add/remove jobs. No need to change this skill file.
