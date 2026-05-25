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

0. **Catchup first (fresh-session only).** Check `state/proactive-loop-started.sentinel` under `${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/`. If absent AND the `catchup-after-startup` skill is installed (i.e. `~/.claude/skills/catchup-after-startup/` exists) → invoke `/catchup-after-startup` BEFORE the cron-scheduling work below, so the conversation buffer carries cross-restart context (last session's PRs, in-flight tasks, recent voice/discord activity, build-log tail) before any cron fires. After invocation, `mkdir -p` the workspace `state/` and `touch` the sentinel. If sentinel is present (cron-driven re-invocation within an already-running session) OR catchup skill not installed → skip step 0 silently and proceed. The sentinel is cleared by the SessionStop hook in `src/session-handoff.sh` (per `6e5c58d` in this PR) so the next fresh session re-fires catchup.

   Rationale: post-#954, the CLI boots with `-- "/schedule-crons"` (not `/proactive-loop`), so this skill IS the actual startup entry — wiring catchup here means it runs synchronously at session start instead of waiting until the first `main-loop` cron fire (~5 min later) to reach `/proactive-loop`'s catchup step (which `822e630` of this PR added). Identical sentinel guard semantics across both paths, so they cooperate idempotently — whichever runs first touches the sentinel; the other skips.

1. Read `skills/schedule-crons/crons.json`
2. Check existing cron jobs with CronList
3. For each job in the config:
   - Skip if a job with matching prompt/name already exists
   - If `prompt_skill` is set, invoke it as `/skill-name`
   - Call CronCreate with the cron expression and prompt
4. **Fallback — ensure `/proactive-loop` is scheduled.** After step 3, check whether any job in `crons.json` references `/proactive-loop` (either `"prompt_skill": "proactive-loop"` or a `"prompt"` whose body invokes the loop). If none does, call `CronCreate` directly with `cron: "*/10 * * * *"` and `prompt: "/proactive-loop"` as a bootstrap-safety net. Rationale: post-#954 the CLI boots with `-- "/schedule-crons"` and exits after step 5, so if `crons.json` is missing/empty/forgot-to-include-the-loop-entry the session goes idle with no recurring work driver. The fallback guarantees the loop runs at least every 10 min regardless of config state. Idempotent: if the user has a custom `*/5 * * * *` or `*/15 * * * *` entry, that satisfies the check and the fallback is skipped (no duplicate cron).
5. Start the streaming task watcher via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive. (Pattern mirrors `/proactive-loop` activation step 2 — both bootstrap paths land here, so post-#954 CLI startup via `/schedule-crons` immediately gets a watcher; no gap until the first `main-loop` cron fire.) If `pgrep -f watch-tasks-stream` already shows a running watcher, skip the Monitor call — the existing one continues. Don't kick off `bash src/watch-tasks.sh` (retired 2026-05-14).
6. Confirm what was scheduled — note whether the proactive-loop fallback was triggered (informs operator that crons.json may need a persistent entry).

## Adding New Crons

Edit `crons.json` to add/remove jobs. No need to change this skill file. The proactive-loop fallback (step 4 above) auto-armed if your `crons.json` is missing the loop entry; add an explicit `proactive-loop` entry to suppress the fallback message and pick your own cadence.
