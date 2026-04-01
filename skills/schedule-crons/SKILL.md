# Schedule Crons

Re-create all session cron jobs for Sutando. Run this on startup or after a session restart.

**Usage**: `/schedule-crons`

## How It Works

Jobs are defined in `skills/schedule-crons/crons.json`. Each entry has:
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
4. Start the task watcher if not running: `bash src/watch-tasks.sh` (run_in_background)
5. Confirm what was scheduled

## Adding New Crons

Edit `crons.json` to add/remove jobs. No need to change this skill file.
