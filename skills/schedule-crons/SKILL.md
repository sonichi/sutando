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
4. Start the streaming task watcher via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive. (Pattern mirrors `/proactive-loop` activation step 2 — both bootstrap paths land here, so post-#954 CLI startup via `/schedule-crons` immediately gets a watcher; no gap until the first `main-loop` cron fire.) If `pgrep -f watch-tasks-stream` already shows a running watcher, skip the Monitor call — the existing one continues. Don't kick off `bash src/watch-tasks.sh` (retired 2026-05-14).
5. Confirm what was scheduled

## Adding New Crons

Edit `crons.json` to add/remove jobs. No need to change this skill file.
