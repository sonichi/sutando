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
- `execution` (optional, value `"codex-task"`) — opt this entry into the durable OS-backed Codex runner instead of session cron registration. Codex entries may also set `timezone` (IANA name, default `America/Los_Angeles`), `delivery: "proactive"`, `retry_minutes` (default 15), `max_attempts` (default 3), and `active_stale_minutes` (default 60). Jobs require this explicit opt-in except for the canonical `main-loop` while the selected runtime is Codex; the runtime-specific exception is described below.
- `launchd` (optional bool) — when `true`, the entry is owned by the OS-level cron-runner (`src/cron-runner.py`, installed via `src/install-cron-runner-launchd.sh`), NOT by this session skill. `/schedule-crons` skips these so the two schedulers never double-fire. Use it for daily-deliverable crons that must fire even when no Claude session is idle (the reliability fix for the 2026-07-02 silent 6am-digest miss).
- `monitor` (optional object) — declares a **persistent Monitor-based driver, NOT a cron**: the entry is armed via the `Monitor` tool and re-armed on every `/schedule-crons`, so it survives restarts the way the task watcher (step 1.5) does. Shape: `{"command": "<shell to run>", "description": "<one-line>", "match": "<argv substring that detects a running instance>"}`. It is NOT `CronCreate`d and NOT counted in the stamp's `registered` (it is not a session cron). Armed in step 5.4. Use it for a continuous event-emitting driver (e.g. the content-loop) that must re-arm across restarts.
- `artifact` (optional string) — the filename STEM of the dated output this job produces, e.g. `"fleet-growth"` for `fleet-growth-2026-08-18.mp4`. Read by `health-check.py`'s `daily-cron-punctuality` probe. Without it the probe infers a stem from the last hyphenated token of the job name, so `talk-events-nightly` looks for `nightly-<date>.*`, never observes the real artifact, and reports the job UNCHECKED forever. Declare it whenever the name does not already equal the stem.
- `conditional` (optional bool) — set `true` when the job runs on schedule but produces output only if there is new input (a nightly render with no new beats). The punctuality probe then treats "no artifact today" as evidence of nothing rather than a miss; lateness is still measured from the artifacts that do exist.
  On macOS, the Codex core launcher automatically reconciles ordinary fixed-interval entries to this owner because Codex has no session `CronCreate` surface. It preserves `main-loop`, dynamic loops, and entries already owned by `execution: "codex-task"`, and initializes the runner boundary before changing ownership so activation never replays an old action backlog.

### Durable Codex schedules

Install or reconcile the per-minute launchd runner after adding an `execution: "codex-task"` entry:

```bash
python3 skills/schedule-crons/scripts/codex-scheduler.py install
python3 skills/schedule-crons/scripts/codex-scheduler.py health
```

The runner calculates cron slots in each job's declared timezone, catches up the newest missed slot after sleep, atomically enqueues a deterministic task ID, and uses distinct attempt IDs when an inactive task needs retrying. A queued, claimed, or processed attempt is never duplicated; if it remains active past `active_stale_minutes`, the run fails with a proactive alert so the schedule cannot stall forever. Durable run state lives at `<workspace>/state/schedules/codex-scheduler.json`. Exhausted retries produce a `proactive-schedule-alert-*.txt` result. `health` exits non-zero for a stale scheduler heartbeat or a latest-run failure.

When `core.runtime` is `codex`, the canonical unmarked `main-loop` entry (`prompt_skill: "proactive-loop"`) is also owned automatically by this runner. Codex has no session `CronCreate` surface, so each fire emits one silent, low-priority proactive-pass task. The host's `crons.json` is not rewritten; switching back to Claude restores the normal session-owned loop. The Codex launcher reconciles the launchd runner on every start or attach.

## On Activation

1. Read `<workspace>/hosts/<hostname>/crons.json` (resolve `<workspace>` via `bash scripts/sutando-config.sh workspace`; `<hostname>` = `bash scripts/sutando-config.sh host-label`). **Transition / self-heal:** if that file is missing, seed it once — from the interim `<workspace>/crons/<hostname>.json` if it still exists (folded-in from the pre-#1717 layout), else the legacy `skills/schedule-crons/crons.json` (one-time migration), else `skills/schedule-crons/crons.example.json` — then read it: `WS="$(bash scripts/sutando-config.sh workspace)"; H="$(bash scripts/sutando-config.sh host-label)"; CF="$WS/hosts/$H/crons.json"; if [ ! -f "$CF" ]; then mkdir -p "$WS/hosts/$H"; SRC="$(ls "$WS/crons/$H.json" 2>/dev/null || ls skills/schedule-crons/crons.json 2>/dev/null || echo skills/schedule-crons/crons.example.json)"; cp "$SRC" "$CF"; fi`

1.5. **Start the streaming task watcher NOW, before registering any cron jobs.** This step used to run last (as step 5, after every `CronCreate` round-trip below) — moved here so an incoming task isn't queued unprocessed for the entire registration loop. Measured impact (startup-latency post-mortem, 2026-08-24, RC9 onboarding): with N session crons the old ordering left the watcher unarmed for ~76 seconds on a fresh boot (one `CronCreate` round-trip per entry, 9 entries measured), during which a brand-new user's first onboarding ping sat unprocessed — the single biggest contributor to "Sutando took a while to respond" reports on first boot.

   Start it via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive. **Gate on running watcher TREES, not on the sentinel** — if one is already running, skip the Monitor call; the existing one continues. Reuse the shared enumerator rather than restating its rules here (the copies drift, and step 5 used to be one of the copies that did):

   ```bash
   python3 -c "
   import importlib.util, sys
   s = importlib.util.spec_from_file_location('hc', 'src/health-check.py')
   m = importlib.util.module_from_spec(s)
   try: s.loader.exec_module(m)
   except SystemExit: pass
   ps = m._ps_snapshot()
   sys.exit(2 if ps is None else (0 if m._watcher_trees(ps) else 1))"
   case $? in
     0) echo skip;;
     1) echo start;;
     *) echo 'UNKNOWN: ps did not run — do NOT start; a watcher may be live';;
   esac
   ```

   **Three states, not two: an unavailable `ps` is UNKNOWN, not "no watcher".**
   `_watcher_trees()` catches every `ps` timeout/error and returns `{}`, which is
   byte-identical to a clean empty scan — so the earlier `0 if m._watcher_trees()
   else 1` form printed `start` when enumeration merely failed. Starting there is
   exactly the duplicate this step exists to prevent, and it is the same
   can't-distinguish defect the paragraph below names for the sentinel, pointing
   the other way. `_ps_snapshot()` separates them: `None` means ps did not run,
   `""` means it ran and found nothing. On UNKNOWN, do nothing — a missing
   watcher costs delayed tasks, a duplicate one processes every task twice.

   **Do not gate on the sentinel alone.** `watch-tasks-stream.sh` writes it once at startup, so an absent file means "no watcher" OR "a live watcher whose file was removed" — indistinguishable. Measured 2026-08-07 on a live core: `_watcher_trees()` returned `{'12631': ['12631']}` (functioning — it emitted `TASK_FILE:` for a probe) with the sentinel absent from disk. Gating on the sentinel there would have started a **second** watcher, and both then emit every task, so every task is processed twice. `_watcher_trees()` is also what makes the `pgrep` warning below unnecessary to re-solve: it drops its own pid and matches on argv shape. Don't use `pgrep -f watch-tasks-stream`: pgrep's `-f` argument matches the literal string `watch-tasks-stream` against full argv, which matches the bash wrapper invoking this very pgrep call (the wrapper's argv contains the search string), producing a transient self-match that returns a PID for a subshell that's already gone by the next `ps`. Same PID-stamp + `kill -0` pattern as the catchup sentinel in step 0 — single anti-pattern, single fix. Documented as F5 in `workspace/build_log.md` 2026-06-03T00:02Z validation pass; replayed on the very next session bootstrap (07:25Z) — Sutando.app's checkWatcher Timer caught the gap and sent a `watcher` keystroke, but two owner DMs were silently held in `tasks/` for ~5 min first. Don't kick off `bash src/watch-tasks.sh` (retired 2026-05-14).

1.6. **Arm the registration watchdog, then handle tasks freely — registration is guarded by a mechanism, not by deferral.** Because step 1.5 arms the watcher before the registration loop, a `TASK_FILE` notification (the startup sweep's, or a live arrival) can land while steps 2-4 are still running. Without a rule here the agent improvises under the general "process notifications when they arrive" instruction, pivots to the task, and may never finish registration — losing the `/proactive-loop` fallback, the one thing that guarantees the session has a recurring work driver (that silent loss is the incident class this step exists to close). The old cure was a deferral rule ("finish registration first"), which cost a waiting user the whole registration window before any answer. This step replaces discipline with a watchdog: immediately after step 1.5, before touching any task, arm ONE Monitor:

   ```
   Monitor tool — description "schedule-crons watchdog", timeout_ms 3600000, command:
   cd "$(git -C . rev-parse --show-toplevel 2>/dev/null || echo .)" 2>/dev/null || true
   T0=$(date +%s); WS="$(bash scripts/sutando-config.sh workspace)"
   STAMP="$WS/hosts/$(bash scripts/sutando-config.sh host-label)/schedule-crons-stamp.json"
   [ -n "$WS" ] || { echo "CRONS-WATCHDOG-INERT: workspace did not resolve — guard not armed"; exit 1; }
   sleep 90
   while [ "$(stat -f %m "$STAMP" 2>/dev/null || echo 0)" -lt "$T0" ]; do
     echo "CRONS-UNREGISTERED: schedule-crons has not stamped this boot — return and register now"
     sleep 60
   done
   echo "CRONS-STAMPED: registration complete"
   ```

   Then respond to task notifications as they land: send the fail-open progress ack first (`python3 skills/task-progress/scripts/notify.py ...` — one subprocess, no state mutation, cannot derail anything), and use judgment on handling — a short waiting owner task may simply be served before registration continues; anything substantial gets the ack and is handled when the agent is next free. Either way registration cannot be silently dropped: if it slips, the watchdog emits a nag every 60s until step 5.7's completion stamp lands, and the terminal `CRONS-STAMPED` line confirms the guard saw the stamp — which is also why that stamp write is not optional bookkeeping; it is what releases the watchdog. The 90s grace means a normal boot never hears from it, and the 1-hour cap bounds noise if a boot dies (an unregistered state dies with its session). Detection and re-prompt share one source of truth: the same stamp `health-check.py`'s `session-crons` probe already reads. `stat -f %m` is the darwin form; swap for a `python3 -c` mtime read on linux. **Resolve the workspace INSIDE the command** — a Monitor is a fresh shell and the loop's `WORKSPACE` is a per-command local that is never exported, so an inherited `$WORKSPACE` is empty there and the stamp path silently becomes `/hosts/<label>/...`, which never exists: the guard would then nag on every healthy boot (~58 times before the cap) and never print `CRONS-STAMPED`. That failure looks like the watchdog working hard, which is why the empty-`WS` refusal above is loud instead: an unarmed guard must announce itself rather than pass as a busy one.

2. Check existing cron jobs with CronList
3. For each job in the config:
   - Skip entries carrying a `monitor` object — they are Monitors, not crons (no `cron`, no prompt to register); step 5.4 owns their arming, and a `CronCreate` attempt on one is invalid.
   - Skip entries with `execution: "codex-task"`; the OS-backed runner owns them.
   - **Skip any entry with `"launchd": true`** — it is owned by the OS-level cron-runner (see "Reliable OS-level crons" below), which emits its task independently. Registering it here too would double-fire (duplicate deliveries — the exact noise class the launchd path was built to avoid).
   - **For a `CronCreate`-registered entry (one visible as a job in `CronList`): if a job for this
     entry already exists, RE-REGISTER it rather than skipping** — `CronDelete` the existing job,
     then `CronCreate` from the current `crons.json` text, and confirm the replacement in
     `CronList`. This bullet does **not** apply to the dynamic-loop branch below (`loop: "dynamic"`),
     which is never `CronCreate`d and keeps its own freshness-sentinel guard — re-launching one
     mid-session would re-run the loop body immediately, the exact failure that guard prevents. A session cron is a **snapshot of the prompt taken at registration
     time**; editing `crons.json` afterwards does not reach it. The former rule here was "skip if a
     job with matching prompt/name already exists", which made that snapshot permanent for the life
     of the session: a long-lived core kept firing a prompt its own config had already superseded,
     and every cheap check agreed the config was right.
     Observed on a long-lived core: it booted 2026-07-30, the `pr-flag` entry gained
     `--stand "<stand>"` on 2026-08-03, and the registered job kept firing the pre-edit text for two
     days. That flag is what makes the pr-triage skill's `pr_flag.py` populate `is_mine` (it is deliberately `null`
     without one), so the cron's own instruction — "judge from `ci/mergeable/review/approvals/
     is_mine`" — was reading a field that was structurally always null, with a correct script *and*
     a correct config file. Re-registering fixed it: `is_mine` went from null on all 27 PRs to
     20 true / 5 false.
     **Do not "compare the prompt and only re-register on a mismatch": `CronList` truncates the
     prompt**, so the differing tail is exactly what a comparison cannot see — the `--stand` drift
     above sat past the truncation point. Unconditional re-registration is the only reliable form.
     This does not risk the inline-fire failure described at the end of this step: `CronCreate`
     schedules the next fire time and never runs the prompt on registration.
   - Call `CronCreate` with the cron expression and prompt:
     - If `prompt_skill` is set, pass `prompt: "/skill-name"` (the leading slash makes the scheduled cron fire the skill as a slash command at its scheduled time).
     - Otherwise pass `prompt: <prompt-string-from-config>`.
     - **If the entry is a dynamic loop** (`loop: "dynamic"` / no interval), do NOT `CronCreate` it. Instead invoke the built-in **`/loop` with no interval** (the adaptive/self-pacing mode), passing the entry's prompt (`/skill-name` or `prompt`) plus any `loop_hint` as the loop body, and append to that body: "on each re-arm, also stamp `state/dynamic-loop-<entry-name>.alive` with `{ts, next_delay_s}`". `/loop`-no-interval then self-paces via ScheduleWakeup by its own judgment — no min/max/signal needed. **Durability comes from schedule-crons re-launching it on every boot.** Double-launch guard: a dynamic loop is NOT visible in `CronList` (ScheduleWakeup schedules a wakeup, not a cron job), and it isn't an OS process either, so neither the cron check nor a PID sentinel can see it. Use the mtime-freshness heartbeat pattern instead (same shape as `state/cores/<hostname>.alive`): the loop stamps its `.alive` sentinel on every re-arm (per the body clause above). On **boot** (first `/schedule-crons` of a new session), always launch — wakeups are session-scoped and died with the old session, so any leftover sentinel is definitionally stale. On a **mid-session re-run**, skip the launch if the sentinel's `ts` is younger than `next_delay_s + 120` seconds (loop still armed); launch only if stale or absent. This guard is also what prevents the inline-fire failure mode below for dynamic loops: launching `/loop` runs the body's first iteration immediately, which is intended at boot but must not repeat on a mid-session `/schedule-crons` re-invocation.
   - **Do NOT invoke the skill or run the prompt body inline during /schedule-crons.** Crons fire at their scheduled cron expression, never on registration. (Exception: a dynamic-loop entry's first iteration runs at launch by design — at boot only; the freshness-sentinel guard above is what keeps a mid-session re-run from repeating it.) (Past bug 2026-06-03T16:52Z: a mid-session `/schedule-crons` re-invocation inline-fired every entry — `/morning-briefing` plus 5 cron-body prompts — at one instant, dropping 8 spurious prompts atop legit watcher TASK_FILE events. The long-running session drowned and ended at 16:54 without processing queued owner DMs.)
4. **Fallback — ensure `/proactive-loop` is scheduled.** After step 3, check whether any job in `crons.json` references `/proactive-loop` (either `"prompt_skill": "proactive-loop"` or a `"prompt"` whose body invokes the loop). If none does, call `CronCreate` directly with `cron: "*/10 * * * *"` and `prompt: "/proactive-loop"` as a bootstrap-safety net. Rationale: post-#954 the CLI boots with `-- "/schedule-crons"` and exits once registration finishes, so if `crons.json` is missing/empty/forgot-to-include-the-loop-entry the session goes idle with no recurring work driver. The fallback guarantees the loop runs at least every 10 min regardless of config state. Idempotent: if the user has a custom `*/5 * * * *` or `*/15 * * * *` entry, that satisfies the check and the fallback is skipped (no duplicate cron).
5. **Streaming task watcher — already started in step 1.5, before this step's registration loop ran.** (Moved 2026-08-24: previously ran here, after every `CronCreate` round-trip in steps 2-4, leaving the watcher unarmed for the whole registration window — see step 1.5 for the measured impact and the gate logic, which is unchanged, just earlier.) No action needed here on a normal pass; nothing in steps 2-4 above needs the watcher running to complete.

5.4. **Arm `monitor`-type entries (durable Monitors).** For each crons.json entry carrying a `monitor` object, arm its persistent driver via the `Monitor` tool — `command`: the entry's `monitor.command`, `persistent: true`, `description`: the entry's `monitor.description` — UNLESS a process matching `monitor.match` is already running. Gate exactly like step 1.5's watcher: on a running PROCESS, never on a sentinel. At boot (a fresh session) nothing is running so it always arms; on a mid-session re-run it skips if the driver is live, so it never double-drives. Use a check whose own process tree can NEVER contain the match string (the same self-match trap step 1.5 warns about — do NOT use `pgrep -f`, and do NOT pass the match string as a literal anywhere on the command line: not as an argv to the checker, and not via a `printf | python` pipe either — the `bash -c` wrapper running the pipeline carries the WHOLE command text, match literal included, in its own argv, and it is alive during the scan, so excluding only the checker's pid still false-matches and reports `skip` while the monitor never arms). The checker must read the match from the crons.json FILE, keyed by entry NAME — the entry name is the only literal on the command line, and no driver's argv contains entry names. AND ITS FAILURES MUST NOT ARM: a two-way `&& skip || arm` maps every checker failure (resolver error, missing/malformed crons.json, ps failure, exception) to `arm`, duplicating the monitor exactly when the host is least healthy. Three-way exit semantics — 0 = driver RUNNING (skip), 1 = VERIFIED absent (arm), 2 = CHECK FAILED (do NOT arm; post the failure like a loud-stop):
   ```bash
   python3 -c "
   import subprocess,sys,json,os
   try:
       r=subprocess.run(['bash','scripts/sutando-config.sh','workspace'],capture_output=True,text=True,timeout=15)
       h=subprocess.run(['bash','scripts/sutando-config.sh','host-label'],capture_output=True,text=True,timeout=15)
       if r.returncode or h.returncode: sys.exit(2)
       entries=[e for e in json.load(open(os.path.join(r.stdout.strip(),'hosts',h.stdout.strip(),'crons.json'))) if e['name']==sys.argv[1]]
       if not entries: sys.exit(2)
       match=entries[0]['monitor']['match']
       p=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True,timeout=15)
       if p.returncode: sys.exit(2)
       run=[l for l in p.stdout.splitlines() if match in l and 'ps -axo' not in l]
       sys.exit(0 if run else 1)
   except Exception:
       sys.exit(2)" '<entry-name>'; rc=$?
   case $rc in 0) echo skip;; 1) echo arm;; *) echo "CHECK-FAILED rc=$rc — do NOT arm";; esac
   ```
   (No pid-exclusion is needed: the match literal exists only inside crons.json and the python process's HEAP, never in any argv in the checker's ancestry — so any `ps` hit is a genuine driver process. Requirement on `monitor.match`: pick a substring of the driver's command path that no entry NAME collides with, e.g. `personal-content-loop/scripts/content-driver.sh`.)
   `arm` → call the `Monitor` tool with the entry's `command`/`description` and `persistent: true`. These entries are Monitors, not crons: do NOT `CronCreate` them and do NOT count them in step 5.7's `registered`. This is the durable-Monitor mechanism — a Monitor declared in crons.json and re-armed on every boot, the same shape the task watcher (step 1.5) uses, but data-driven.
5.5. **Ensure the core heartbeat is running (sonichi/sutando#2198 prerequisite).** `src/core_heartbeat.py` (the writer of `state/cores/<hostname>.alive`) is started by `src/startup.sh` — but the CLI boot path lands here without ever running startup.sh (observed 2026-07-20: desktop-supervised core running for 20+ min with `state/cores/` empty, so the dashboard/health-check read the core as dead and the stop-path had no pid/socket target). Check freshness of `"$WORKSPACE/state/cores/$(bash scripts/sutando-config.sh host-label).alive"` — if the file is missing or its mtime is older than 90 seconds (the documented staleness threshold), start the heartbeat: `nohup python3 src/core_heartbeat.py > /tmp/core-heartbeat.log 2>&1 &`. Freshness-of-.alive is the running-check by design — do NOT use `pgrep -f core_heartbeat` (same wrapper-argv self-match anti-pattern as step 1.5's watcher note), and a fresh mtime is exactly the signal every other reader of the file trusts. Idempotent on mid-session re-runs: a live heartbeat keeps the mtime younger than 90s, so the start is skipped.

5.6. **Auto session-recap on boot (owner directive 2026-07-13).** When more than one session transcript exists (i.e. there is a previous session to recap), run the `session-recap` skill's boot recap over the previous session. Per the recap contract (`skills/session-recap/SKILL.md` "Automatic recap on restart"), this is **two behaviors with different gates** — do NOT gate the whole step on `recap_room`:
   - **Agent catchup — ALWAYS (gate: a previous transcript exists).** Generate the structured next-session recap and write it to `<workspace>/state/last-session-recap.md` (also stamp `state/last-recap-session.txt`). This is the primary purpose — it seeds the fresh core's context at boot — and does **not** depend on `recap_room`. A host with no `recap.json` still gets this.
   - **Human room post — ONLY if `recap_room` is set (and private).** If `recap_room` is configured in this host's `recap.json` — `<workspace>/hosts/<hostname>/recap.json`, per the hosts/<hostname>/ per-host state convention, sibling of `crons.json` (which itself stays a bare job list) and names a private, owner-only room, additionally post the brief to `recap_room` (gateway op:message). No `recap_room`, or a non-private one → skip the post, leave the recap on disk under `data/session-recaps/`.
   Idempotence lives in the recap skill's `state/last-recap-session.txt` stamp — a mid-session `/schedule-crons` re-run finds the previous session already stamped and skips both the write and the post, so this never double-writes or double-posts (same guard philosophy as the dynamic-loop freshness sentinel in step 3).

5.7. **Stamp completion for the health-check divergence guard.** After all registrations (and the fallback check in step 4), count the session-owned entries you actually registered this run (CronCreate successes + pre-existing matches from step 3, including the main-loop/fallback; EXCLUDE `monitor`-type, `launchd`, and `codex-task` entries — they are not session crons) and write the stamp — script-visible proof that THIS core boot completed registration:
   ```bash
   WS="$(bash scripts/sutando-config.sh workspace)"
   H="$(bash scripts/sutando-config.sh host-label)"
   mkdir -p "$WS/hosts/$H"
   DIGESTS="$(python3 src/cron_entry_digest.py "$WS/hosts/$H/crons.json")"
   echo "{\"ts\": $(date +%s), \"registered\": <count>, \"config_total\": <total entries in crons.json>, \"config_digests\": $DIGESTS}" > "$WS/hosts/$H/schedule-crons-stamp.json"
   ```
   `health-check.py`'s `session-crons` probe compares this host-owned stamp against the same host's core heartbeat `started_at`: a stamp older than the boot means session crons died with a previous session and were never re-registered (the silent 2/18 failure observed on a peer instance 2026-07-23). Do not skip the stamp on re-runs — a fresh stamp is what keeps the guard quiet.

   **`config_digests` is what makes an edit visible.** Everything else in the stamp is a COUNT, and a
   count cannot see an entry whose prompt changed after it was registered. Stamp the digest map of
   the `crons.json` you just registered from; the probe recomputes it and names any session-owned
   entry whose digest moved. Write it in the SAME command as the counts — a digest map stamped
   separately can be skipped, and a stamp with fresh counts and a stale digest map is worse than one
   with no digest map at all. Omitting the field is safe (the probe skips the check); a WRONG map
   would report drift that is not there.

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

**Which crons opt in:** only *output-producing* crons (pr-shepherd, roadmap-driver, friction-room-sweep, disk-hygiene, ai-frontline-today, morning-briefing). Silent/internal crons (main-loop, sync-memory, briefing-fallback) stay room-less — a room each would be clutter.

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

### Mechanical shell jobs

Launchd-owned entries may set `"shell_command"` for work that should not wake a
model session (for example, a polling or sync script):

```json
{
  "name": "sync-workspace",
  "cron": "*/30 * * * *",
  "launchd": true,
  "shell_command": "bash scripts/sync-workspace.sh"
}
```

`src/cron-runner.py` executes the command from the repository root, logs its
command, stdout, stderr, and exit code to `<workspace>/logs/cron-runner.log`,
and reports non-zero exits on stderr. A shell job runs even when the core
heartbeat is absent and never creates a `tasks/` file. If an entry contains
more than one execution form, precedence is `shell_command` > `prompt_skill` >
`prompt`; use only one form in new configuration.

When the selected core runtime is Codex on macOS, `src/agent/codex/cli/start-cli.sh` performs this installation/reconciliation automatically. Manual installation remains the opt-in path for Claude-core hosts.

**Ownership partition (no double-fire):** the launchd runner handles ONLY `"launchd": true` entries; this session skill (step 3) skips those same entries. Exactly one scheduler owns each cron. Leave `main-loop` / `/proactive-loop` session-owned (it drives the session itself — it is not a task and must never be launchd-owned).

## Digest cron delivery — write one `results/proactive-*.txt`, nothing else

`notify.py` is for **progress pings only** (≤280 chars). Digest-style cron prompts that produce research summaries (1000–2000 chars) are silently dropped by notify.py's hard limit — the user sees nothing.

**Correct delivery pattern for digest crons — the shared proactive primitive:**

```
DELIVERY: Write the complete digest to results/proactive-<name>-$(date +%s).txt
(and nothing else). Do NOT use notify.py for the final result — it rejects
messages over 280 chars (it is a progress-ping tool, not a delivery channel).
```

`results/proactive-*.txt` is the **one cross-surface delivery contract** — every
configured bridge (Discord, Telegram, Slack) drains it, and `proactive_routing.py`
routes each file to the channel where the owner was **most recently active**, exactly
once (atomic `.sending` claim). That's why it's the primitive to use.

**Do NOT** write `results/briefing-*` and **do NOT** mint a synthetic
`tasks/task-cron-*` "for Tasks-tab visibility":
- `briefing-*` is not a universal prefix — only Discord (`FALLBACK_PREFIXES` in
  `poll_dm_fallback`) and Telegram (a briefing-as-proactive patch) drain it; **Slack
  never delivers it**, so a `briefing-*` digest is silently archived on a Slack-only
  install. `proactive-*` has no such gap.
- A hand-written `tasks/task-cron-*` is an **orphan**: nothing writes a matching
  `results/task-cron-*` keyed to that id, so the task never "completes" — the watcher
  re-processes it (duplicate/noisy execution) and the task-id-keyed consumers
  (Slack/Telegram/agent-api) deliver nothing. Delivery comes from the `proactive-*`
  file alone; you don't need a task file for it.

See `crons.example.json` for the `example-digest` entry that shows this pattern. Scripts (like `src/morning-briefing.py`) already emit `results/proactive-*.txt` themselves and don't need this — only inline prompt crons that produce long output.
