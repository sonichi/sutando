# Schedule Crons

Re-create all session cron jobs for Sutando. Run this on startup or after a session restart.

**Usage**: `/schedule-crons`

## How It Works

Jobs are defined per host in `<workspace>/hosts/<hostname>/crons.json` — **per-host, synced + backed up via the vault** (carried as part of the `hosts/*/` per-host subtree (#1717), which is hostname-qualified so it never collapses across hosts; see [`docs/workspace-hosts-convention.md`](../../docs/workspace-hosts-convention.md) and [`docs/workspace-per-host-paths.md`](../../docs/workspace-per-host-paths.md)). `<hostname>` is `bash scripts/sutando-config.sh host-label` — the canonical per-host label (`$SUTANDO_HOST_LABEL` > scutil `LocalHostName` > short `hostname`), matching the sync layer's host slug. (Do NOT use a bare `hostname | sed 's/\..*//'`: a DHCP lease can drift the hostname (e.g. Comcast → `Chis-MBP`) and split per-host paths from the stable label; #1745.) A template is in `crons.example.json` (in this skill dir, version-controlled). Copy it on first setup:
```bash
WS="$(bash scripts/sutando-config.sh workspace)"; H="$(bash scripts/sutando-config.sh host-label)"; mkdir -p "$WS/hosts/$H"
cp skills/schedule-crons/crons.example.json "$WS/hosts/$H/crons.json"
```
(Migrated from the old `skills/schedule-crons/crons.json`, which lived in the code checkout — misfiled per the workspace contract, and per-host-but-unsynced. The new path is proper per-user state: backed up + visible across hosts, each host keeping its own cron set.)

Each entry has:
- `name` — unique identifier (used to avoid duplicates)
- `cron` — 5-field cron expression
- `prompt` — the prompt to run (direct text)
- `prompt_skill` — OR a skill to invoke (e.g. "morning-briefing" → `/morning-briefing`)
- `loop` (optional, value `"dynamic"`) — declares a **dynamic (self-pacing) loop** using the built-in `/loop` primitive. An entry with **no interval** (no `cron` field) + `loop: "dynamic"` is run by schedule-crons as `/loop` *without an interval* (see step 3) — which is exactly the built-in adaptive mode: the loop self-paces via ScheduleWakeup, deciding each next delay by its own judgment. Optional `loop_hint` (free text) guides that pacing (e.g. "~10 min when owner active, ~40 min quiet"). **Durable** because schedule-crons re-launches it every boot; **adaptive** because that's what `/loop`-no-interval already is. No min/max/signal schema and no custom gate — the built-in does the pacing. Example: `{name:"inbox-score", prompt_skill:"inbox-score", loop:"dynamic", loop_hint:"…"}`.
- `execution` (optional, value `"codex-task"`) — opt this entry into the durable OS-backed Codex runner instead of session cron registration. Codex entries may also set `timezone` (IANA name, default `America/Los_Angeles`), `delivery: "proactive"`, `retry_minutes` (default 15), `max_attempts` (default 3), and `active_stale_minutes` (default 60). Only explicitly opted-in entries are handled, so Claude session crons are never duplicated.
- `launchd` (optional bool) — when `true`, the entry is owned by the OS-level cron-runner (`src/cron-runner.py`, installed via `src/install-cron-runner-launchd.sh`), NOT by this session skill. `/schedule-crons` skips these so the two schedulers never double-fire. Use it for daily-deliverable crons that must fire even when no Claude session is idle (the reliability fix for the 2026-07-02 silent 6am-digest miss).

### Durable Codex schedules

Install or reconcile the per-minute launchd runner after adding an `execution: "codex-task"` entry:

```bash
python3 skills/schedule-crons/scripts/codex-scheduler.py install
python3 skills/schedule-crons/scripts/codex-scheduler.py health
```

The runner calculates cron slots in each job's declared timezone, catches up the newest missed slot after sleep, atomically enqueues a deterministic task ID, and uses distinct attempt IDs when an inactive task needs retrying. A queued, claimed, or processed attempt is never duplicated; if it remains active past `active_stale_minutes`, the run fails with a proactive alert so the schedule cannot stall forever. Durable run state lives at `<workspace>/state/schedules/codex-scheduler.json`. Exhausted retries produce a `proactive-schedule-alert-*.txt` result. `health` exits non-zero for a stale scheduler heartbeat or a latest-run failure.

## On Activation

1. Read `<workspace>/hosts/<hostname>/crons.json` (resolve `<workspace>` via `bash scripts/sutando-config.sh workspace`; `<hostname>` = `bash scripts/sutando-config.sh host-label`). **Transition / self-heal:** if that file is missing, seed it once — from the interim `<workspace>/crons/<hostname>.json` if it still exists (folded-in from the pre-#1717 layout), else the legacy `skills/schedule-crons/crons.json` (one-time migration), else `skills/schedule-crons/crons.example.json` — then read it: `WS="$(bash scripts/sutando-config.sh workspace)"; H="$(bash scripts/sutando-config.sh host-label)"; CF="$WS/hosts/$H/crons.json"; if [ ! -f "$CF" ]; then mkdir -p "$WS/hosts/$H"; SRC="$(ls "$WS/crons/$H.json" 2>/dev/null || ls skills/schedule-crons/crons.json 2>/dev/null || echo skills/schedule-crons/crons.example.json)"; cp "$SRC" "$CF"; fi`
2. Check existing cron jobs with CronList
3. For each job in the config:
   - Skip entries with `execution: "codex-task"`; the OS-backed runner owns them.
   - **Skip any entry with `"launchd": true`** — it is owned by the OS-level cron-runner (see "Reliable OS-level crons" below), which emits its task independently. Registering it here too would double-fire (duplicate deliveries — the exact noise class the launchd path was built to avoid).
   - Skip if a job with matching prompt/name already exists
   - Call `CronCreate` with the cron expression and prompt:
     - If `prompt_skill` is set, pass `prompt: "/skill-name"` (the leading slash makes the scheduled cron fire the skill as a slash command at its scheduled time).
     - Otherwise pass `prompt: <prompt-string-from-config>`.
     - **If the entry is a dynamic loop** (`loop: "dynamic"` / no interval), do NOT `CronCreate` it. Instead invoke the built-in **`/loop` with no interval** (the adaptive/self-pacing mode), passing the entry's prompt (`/skill-name` or `prompt`) plus any `loop_hint` as the loop body, and append to that body: "on each re-arm, also stamp `state/dynamic-loop-<entry-name>.alive` with `{ts, next_delay_s}`". `/loop`-no-interval then self-paces via ScheduleWakeup by its own judgment — no min/max/signal needed. **Durability comes from schedule-crons re-launching it on every boot.** Double-launch guard: a dynamic loop is NOT visible in `CronList` (ScheduleWakeup schedules a wakeup, not a cron job), and it isn't an OS process either, so neither the cron check nor a PID sentinel can see it. Use the mtime-freshness heartbeat pattern instead (same shape as `state/cores/<hostname>.alive`): the loop stamps its `.alive` sentinel on every re-arm (per the body clause above). On **boot** (first `/schedule-crons` of a new session), always launch — wakeups are session-scoped and died with the old session, so any leftover sentinel is definitionally stale. On a **mid-session re-run**, skip the launch if the sentinel's `ts` is younger than `next_delay_s + 120` seconds (loop still armed); launch only if stale or absent. This guard is also what prevents the inline-fire failure mode below for dynamic loops: launching `/loop` runs the body's first iteration immediately, which is intended at boot but must not repeat on a mid-session `/schedule-crons` re-invocation.
   - **Do NOT invoke the skill or run the prompt body inline during /schedule-crons.** Crons fire at their scheduled cron expression, never on registration. (Exception: a dynamic-loop entry's first iteration runs at launch by design — at boot only; the freshness-sentinel guard above is what keeps a mid-session re-run from repeating it.) (Past bug 2026-06-03T16:52Z: a mid-session `/schedule-crons` re-invocation inline-fired every entry — `/morning-briefing` plus 5 cron-body prompts — at one instant, dropping 8 spurious prompts atop legit watcher TASK_FILE events. The long-running session drowned and ended at 16:54 without processing queued owner DMs.)
4. **Fallback — ensure `/proactive-loop` is scheduled.** After step 3, check whether any job in `crons.json` references `/proactive-loop` (either `"prompt_skill": "proactive-loop"` or a `"prompt"` whose body invokes the loop). If none does, call `CronCreate` directly with `cron: "*/10 * * * *"` and `prompt: "/proactive-loop"` as a bootstrap-safety net. Rationale: post-#954 the CLI boots with `-- "/schedule-crons"` and exits after step 5, so if `crons.json` is missing/empty/forgot-to-include-the-loop-entry the session goes idle with no recurring work driver. The fallback guarantees the loop runs at least every 10 min regardless of config state. Idempotent: if the user has a custom `*/5 * * * *` or `*/15 * * * *` entry, that satisfies the check and the fallback is skipped (no duplicate cron).
5. Start the streaming task watcher via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive. (Pattern mirrors `/proactive-loop` activation step 2 — both bootstrap paths land here, so post-#954 CLI startup via `/schedule-crons` immediately gets a watcher; no gap until the first `main-loop` cron fire.) PID-check the watcher sentinel before invoking Monitor — if `"$WORKSPACE/state/watch-tasks-stream.pid"` exists AND its PID is alive (`pid=$(cat "$WORKSPACE/state/watch-tasks-stream.pid" 2>/dev/null); kill -0 "$pid" 2>/dev/null`), skip the Monitor call — the existing one continues. Don't use `pgrep -f watch-tasks-stream`: pgrep's `-f` argument matches the literal string `watch-tasks-stream` against full argv, which matches the bash wrapper invoking this very pgrep call (the wrapper's argv contains the search string), producing a transient self-match that returns a PID for a subshell that's already gone by the next `ps`. Same PID-stamp + `kill -0` pattern as the catchup sentinel in step 0 — single anti-pattern, single fix. Documented as F5 in `workspace/build_log.md` 2026-06-03T00:02Z validation pass; replayed on the very next session bootstrap (07:25Z) — Sutando.app's checkWatcher Timer caught the gap and sent a `watcher` keystroke, but two owner DMs were silently held in `tasks/` for ~5 min first. Don't kick off `bash src/watch-tasks.sh` (retired 2026-05-14).
5.6. **Auto session-recap on boot (owner directive 2026-07-13).** When more than one session transcript exists (i.e. there is a previous session to recap), run the `session-recap` skill's boot recap over the previous session. Per the recap contract (`skills/session-recap/SKILL.md` "Automatic recap on restart"), this is **two behaviors with different gates** — do NOT gate the whole step on `recap_room`:
   - **Agent catchup — ALWAYS (gate: a previous transcript exists).** Generate the structured next-session recap and write it to `<workspace>/state/last-session-recap.md` (also stamp `state/last-recap-session.txt`). This is the primary purpose — it seeds the fresh core's context at boot — and does **not** depend on `recap_room`. A host with no `recap.json` still gets this.
   - **Human room post — ONLY if `recap_room` is set (and private).** If `recap_room` is configured in this host's `recap.json` — `<workspace>/hosts/<hostname>/recap.json`, per the hosts/<hostname>/ per-host state convention, sibling of `crons.json` (which itself stays a bare job list) and names a private, owner-only room, additionally post the brief to `recap_room` (gateway op:message). No `recap_room`, or a non-private one → skip the post, leave the recap on disk under `data/session-recaps/`.
   Idempotence lives in the recap skill's `state/last-recap-session.txt` stamp — a mid-session `/schedule-crons` re-run finds the previous session already stamped and skips both the write and the post, so this never double-writes or double-posts (same guard philosophy as the dynamic-loop freshness sentinel in step 3).

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

## Attaching cron output to an AG2 Space room (if connected)

When the agent is connected to AG2 Space, an output-producing cron can post its results into its **own dedicated room** instead of a shared channel — one room per cron, so the owner can monitor each stream separately. This is **opt-in and connectivity-gated**: a cron with no `room` field, or an agent with no gateway token, is unaffected.

**Opt in** by adding `"room": "auto"` to a cron entry in `crons.json`. On `/schedule-crons` activation (after step 3), run the helper once:

```bash
WS="$(bash scripts/sutando-config.sh workspace)"; H="$(bash scripts/sutando-config.sh host-label)"
python3 skills/schedule-crons/ensure-cron-room.py \
  --crons-file "$WS/hosts/$H/crons.json" --owner "@<owner>:ag2.space" --repo .
```

`ensure-cron-room.py` is **idempotent**: for each `"room": "auto"` entry it creates one room (`Sutando · <cron>`), invites the owner, posts a self-identifying first message, and **rewrites `room` to the concrete `!id:ag2.space`**. Entries that already hold a `!id` are skipped — re-running never makes duplicate rooms (the failure mode of ad-hoc creation). If no gateway token resolves, it exits 0 having done nothing. The cron's own prompt then posts output to its `room` id via the gateway op:message path ([[reference_gateway_op_message_room_post]]).

**Which crons opt in:** only *output-producing* crons (pr-shepherd, roadmap-driver, friction-room-sweep, disk-hygiene, ai-frontline-today, morning-briefing). Silent/internal crons (main-loop, sync-memory, briefing-fallback, daily-insight) stay room-less — a room each would be clutter.

**Known gateway constraints (2026-07-11), baked into the helper's design:**
- **No room-list API** (`GET /v1/rooms` 404; `op:list` unknown) → the `room` id recorded in `crons.json` is the *only* handle on a created room. Never create without writing the id back (the helper writes after each create so a mid-batch hang can't orphan a room).
- **`op:state` 502s** → a room's display name can't be set or read after creation. Identity rides on the create-time `name` **and** the identifying first message, never a post-hoc state write.
- **`op:invite` is slow/flaky** — it can take >15s or time out client-side while the invite still lands server-side. So (a) treat invite as best-effort (the helper tolerates a `None` result), and (b) do NOT retry-loop it — repeat calls may queue duplicate invites the owner has to dismiss. These are roadmap track-8 (error-legibility) / broker-reliability items.

**When to gate (decision rule):**

| Cron cadence | Gate? | Why |
| --- | --- | --- |
| `main-loop` (`/proactive-loop`) | **NEVER** | `/proactive-loop` IS the owner-task handler; gating would deadlock. |
| Sub-daily (`*/N`, `*/30`, hourly) | **YES** | A skip is recovered by the next natural tick within minutes-hours. |
| Daily / less-frequent (`X Y * * *`) | **NO** | A skip = function is gone until next day (briefing missed, etc.). M1's no-inline-fire rule already kills the avalanche on registration — gating dailies is over-broad. |

Lucy caught this on PR #1437 (2026-06-03): gating daily crons (morning-briefing 06:57, daily-insight 06:50, obsidian-dream 03:37, learned-skills-scan 07:30) means one queued task at briefing time loses the briefing for the entire day. Pinning the gate to sub-daily crons preserves the defense-in-depth where it matters without the missed-day risk.

## Reliable OS-level crons (`"launchd": true`)

Session `CronCreate` jobs are best-effort: they only fire while the Claude REPL is idle at the fire minute, carry scheduler jitter (recurring fires up to 10% / max 15min late), and die with the session. On 2026-07-02 the 6:02 loop-engineering digest silently never delivered — the owner asked to "make the schedule reliably run".

For a cron that MUST fire regardless of session state, flag it `"launchd": true` and install the OS-level runner once:

```bash
bash src/install-cron-runner-launchd.sh          # install (idempotent)
bash src/install-cron-runner-launchd.sh --status # check
bash src/install-cron-runner-launchd.sh --uninstall
```

This installs `com.sutando.cron-runner` (launchd, every 60s → `src/cron-runner.py`), which reads the same `crons.json`, decides which `"launchd": true` entries are DUE since their last recorded fire, and emits a task file into `tasks/` for each. The streaming watcher hands it to the session — same OS-level → emit-task → process pipeline as `com.sutando.health-check-fallback`. Missed fires (machine asleep/off) catch up exactly once on the next tick, never a backlog storm.

**Ownership partition (no double-fire):** the launchd runner handles ONLY `"launchd": true` entries; this session skill (step 3) skips those same entries. Exactly one scheduler owns each cron. Leave `main-loop` / `/proactive-loop` session-owned (it drives the session itself — it is not a task and must never be launchd-owned).
