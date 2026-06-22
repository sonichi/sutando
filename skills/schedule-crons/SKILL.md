# Schedule Crons

Re-create all session cron jobs for Sutando. Run this on startup or after a session restart.

**Usage**: `/schedule-crons`

## How It Works

Jobs are defined per host in `<workspace>/hosts/<hostname>/crons.json` — **per-host, synced + backed up via the vault** (carried as part of the `hosts/*/` per-host subtree (#1717), which is hostname-qualified so it never collapses across hosts; see [`docs/workspace-hosts-convention.md`](../../docs/workspace-hosts-convention.md) and [`docs/workspace-per-host-paths.md`](../../docs/workspace-per-host-paths.md)). `<hostname>` is `hostname | sed 's/\..*//'`, matching the sync layer's host slug. A template is in `crons.example.json` (in this skill dir, version-controlled). Copy it on first setup:
```bash
WS="$(bash scripts/sutando-config.sh workspace)"; H="$(hostname | sed 's/\..*//')"; mkdir -p "$WS/hosts/$H"
cp skills/schedule-crons/crons.example.json "$WS/hosts/$H/crons.json"
```
(Migrated from the old `skills/schedule-crons/crons.json`, which lived in the code checkout — misfiled per the workspace contract, and per-host-but-unsynced. The new path is proper per-user state: backed up + visible across hosts, each host keeping its own cron set.)

Each entry has:
- `name` — unique identifier (used to avoid duplicates)
- `cron` — 5-field cron expression
- `prompt` — the prompt to run (direct text)
- `prompt_skill` — OR a skill to invoke (e.g. "morning-briefing" → `/morning-briefing`)

## On Activation

1. Read `<workspace>/hosts/<hostname>/crons.json` (resolve `<workspace>` via `bash scripts/sutando-config.sh workspace`; `<hostname>` = `hostname | sed 's/\..*//'`). **Transition / self-heal:** if that file is missing, seed it once — from the interim `<workspace>/crons/<hostname>.json` if it still exists (folded-in from the pre-#1717 layout), else the legacy `skills/schedule-crons/crons.json` (one-time migration), else `skills/schedule-crons/crons.example.json` — then read it: `WS="$(bash scripts/sutando-config.sh workspace)"; H="$(hostname | sed 's/\..*//')"; CF="$WS/hosts/$H/crons.json"; if [ ! -f "$CF" ]; then mkdir -p "$WS/hosts/$H"; SRC="$(ls "$WS/crons/$H.json" 2>/dev/null || ls skills/schedule-crons/crons.json 2>/dev/null || echo skills/schedule-crons/crons.example.json)"; cp "$SRC" "$CF"; fi`
2. Check existing cron jobs with CronList
3. For each job in the config:
   - Skip if a job with matching prompt/name already exists
   - Call `CronCreate` with the cron expression and prompt:
     - If `prompt_skill` is set, pass `prompt: "/skill-name"` (the leading slash makes the scheduled cron fire the skill as a slash command at its scheduled time).
     - Otherwise pass `prompt: <prompt-string-from-config>`.
   - **Do NOT invoke the skill or run the prompt body inline during /schedule-crons.** Crons fire at their scheduled cron expression, never on registration. (Past bug 2026-06-03T16:52Z: a mid-session `/schedule-crons` re-invocation inline-fired every entry — `/morning-briefing` plus 5 cron-body prompts — at one instant, dropping 8 spurious prompts atop legit watcher TASK_FILE events. The long-running session drowned and ended at 16:54 without processing queued owner DMs.)
4. **Fallback — ensure `/proactive-loop` is scheduled.** After step 3, check whether any job in `crons.json` references `/proactive-loop` (either `"prompt_skill": "proactive-loop"` or a `"prompt"` whose body invokes the loop). If none does, call `CronCreate` directly with `cron: "*/10 * * * *"` and `prompt: "/proactive-loop"` as a bootstrap-safety net. Rationale: post-#954 the CLI boots with `-- "/schedule-crons"` and exits after step 5, so if `crons.json` is missing/empty/forgot-to-include-the-loop-entry the session goes idle with no recurring work driver. The fallback guarantees the loop runs at least every 10 min regardless of config state. Idempotent: if the user has a custom `*/5 * * * *` or `*/15 * * * *` entry, that satisfies the check and the fallback is skipped (no duplicate cron).
5. Start the streaming task watcher via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive. (Pattern mirrors `/proactive-loop` activation step 2 — both bootstrap paths land here, so post-#954 CLI startup via `/schedule-crons` immediately gets a watcher; no gap until the first `main-loop` cron fire.) PID-check the watcher sentinel before invoking Monitor — if `"$WORKSPACE/state/watch-tasks-stream.pid"` exists AND its PID is alive (`pid=$(cat "$WORKSPACE/state/watch-tasks-stream.pid" 2>/dev/null); kill -0 "$pid" 2>/dev/null`), skip the Monitor call — the existing one continues. Don't use `pgrep -f watch-tasks-stream`: pgrep's `-f` argument matches the literal string `watch-tasks-stream` against full argv, which matches the bash wrapper invoking this very pgrep call (the wrapper's argv contains the search string), producing a transient self-match that returns a PID for a subshell that's already gone by the next `ps`. Same PID-stamp + `kill -0` pattern as the catchup sentinel in step 0 — single anti-pattern, single fix. Documented as F5 in `workspace/build_log.md` 2026-06-03T00:02Z validation pass; replayed on the very next session bootstrap (07:25Z) — Sutando.app's checkWatcher Timer caught the gap and sent a `watcher` keystroke, but two owner DMs were silently held in `tasks/` for ~5 min first. Don't kick off `bash src/watch-tasks.sh` (retired 2026-05-14).
6. Confirm what was scheduled — note whether the proactive-loop fallback was triggered (informs operator that crons.json may need a persistent entry).

## Adding New Crons

Edit `<workspace>/hosts/<hostname>/crons.json` (this host's cron set) to add/remove jobs. No need to change this skill file. The proactive-loop fallback (step 4 above) auto-armed if your `crons.json` is missing the loop entry; add an explicit `proactive-loop` entry to suppress the fallback message and pick your own cadence.

### Defer non-loop crons when owner tasks are queued

Wrap **sub-daily** non-`main-loop` cron `prompt` bodies (e.g. `*/N`, `*/30`, hourly) with `scripts/cron-gate.sh` so the cron defers when `<workspace>/tasks/` has any `task-*.txt` pending. The next natural tick (≤ a few minutes later for `*/30`, ≤ an hour for hourly) covers a deferred fire. Pattern:

```json
{
  "name": "sync-workspace",
  "cron": "*/30 * * * *",
  "prompt": "Run: bash scripts/cron-gate.sh sync-workspace bash scripts/sync-workspace.sh — <human-readable description>."
}
```

`cron-gate.sh <reason> <command...>` either `exec`s the command (queue empty) or prints `cron-gate: owner tasks queued — deferring <reason>` and exits 0. See `crons.example.json` for canonical wrapped forms.

**When to gate (decision rule):**

| Cron cadence | Gate? | Why |
| --- | --- | --- |
| `main-loop` (`/proactive-loop`) | **NEVER** | `/proactive-loop` IS the owner-task handler; gating would deadlock. |
| Sub-daily (`*/N`, `*/30`, hourly) | **YES** | A skip is recovered by the next natural tick within minutes-hours. |
| Daily / less-frequent (`X Y * * *`) | **NO** | A skip = function is gone until next day (briefing missed, etc.). M1's no-inline-fire rule already kills the avalanche on registration — gating dailies is over-broad. |

Lucy caught this on PR #1437 (2026-06-03): gating daily crons (morning-briefing 06:57, daily-insight 06:50, obsidian-dream 03:37, learned-skills-scan 07:30) means one queued task at briefing time loses the briefing for the entire day. Pinning the gate to sub-daily crons preserves the defense-in-depth where it matters without the missed-day risk.
