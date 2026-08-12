## The defect

`watch-tasks-stream.sh` stamps `state/watch-tasks-stream.pid` **once** at startup (`src/watch-tasks-stream.sh:360`) and never again. #2652 already established that only the owner may remove it — `cleanup()` compare-and-deletes, with a comment naming the exact hazard ("a duplicate watcher can overwrite the sentinel before the stale watcher exits… otherwise the live watcher would look orphaned").

The reaper on the startup side had the same job and none of that guard (`src/startup.sh:655-663` at `23df7f4`):

```bash
STALE_PID="$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)"
if [ -n "$STALE_PID" ] && ps -p "$STALE_PID" -o args= 2>/dev/null | grep -q "watch-tasks-stream"; then
  kill "$STALE_PID" 2>/dev/null || true
  ...
fi
rm -f "$WATCHER_PID_FILE"     # <-- unconditional
```

When the pid it read is dead or recycled the kill is skipped — correctly — and it deletes anyway. A watcher that stamps its own pid inside that window (startup is exactly when watchers start) keeps running with **no sentinel at all**: alive, draining `tasks/`, and invisible to every reader that keys off the file — `health-check.py:5445` (`task-watcher` probe), `services_status.py:243`, and the next startup's own reap.

Same policy, two implementations, and the copy without the guard is the one that strands a live process.

## Before — `origin/main` @ `23df7f4`

The pre-fix code is inline in `startup.sh`, so it is only reachable by extraction. Extracted **verbatim by line range** so the extraction is checkable (`sed -n '655,663p' src/startup.sh` — output pasted above), then driven through the window: sentinel names a dead pid; a live watcher stamps `424242` between the read and the delete.

```
$ sed -n '655,663p' "$BASE/src/startup.sh" | tee /private/tmp/baseline-block.sh
WATCHER_PID_FILE="$WORKSPACE/state/watch-tasks-stream.pid"
if [ -f "$WATCHER_PID_FILE" ]; then
  STALE_PID="$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)"
  if [ -n "$STALE_PID" ] && ps -p "$STALE_PID" -o args= 2>/dev/null | grep -q "watch-tasks-stream"; then
    kill "$STALE_PID" 2>/dev/null || true
    echo "  ✓ reaped stale watch-tasks-stream watcher (pid $STALE_PID)"
  fi
  rm -f "$WATCHER_PID_FILE"
fi
--- driving that block through case 3 ---
RESULT: sentinel DELETED — the live watcher (pid 424242) is now untrackable
```

And the new test against `origin/main`'s sources:

```
$ REPO_UNDER_TEST=/private/tmp/reaper-baseline bash tests/startup-watcher-reaper-ownership.test.sh
startup watch-tasks-stream reaper ownership:
  FAIL reap_stale_task_watcher is defined in src/startup-runtime.sh — not found
FAILED (1)
rc=1
```

## After — this branch @ `c275737`

```
$ bash tests/startup-watcher-reaper-ownership.test.sh
startup watch-tasks-stream reaper ownership:
  ok   dead pid: sentinel removed
  ok   live stale watcher: sentinel removed
  ok   live stale watcher: signaled
  ok   re-stamped mid-reap: live watcher's sentinel survives
  ok   re-stamped mid-reap: sentinel still names the live watcher
  ok   absent sentinel: no-op, rc 0
  ok   startup.sh delegates to the shared reaper
  ok   startup.sh keeps no unguarded copy
ALL PASS
rc=0
```

## The change

- `reap_stale_task_watcher()` moves into `src/startup-runtime.sh` — where `reap_wedged_voice_agent()` already lives, and where a test can call the production function instead of a copy. `startup.sh` calls it with the resolved path.
- The removal becomes compare-and-delete. **The kill path is untouched**, including its recycled-pid `ps … | grep` check. Every case where the sentinel still names the pid that was inspected — dead pid, live stale watcher, empty file — behaves exactly as before; only the case where the file changed underneath is new, and there it declines and says so.

The test drives the sourced production function. `ps` is a PATH shim in case 3 only (the same technique `tests/startup-voice-wedge-reap.test.py` uses for `curl`) so the window is deterministic rather than a timing race; cases 1, 2 and 4 use the real `ps` and a real process whose argv genuinely carries `watch-tasks-stream`.

## Checks run locally

```
tests/startup-watcher-reaper-ownership.test.sh                 PASS   (new)
tests/watch-tasks-stream-sentinel-ownership.test.py            PASS   (#2652, the other half of this policy)
tests/watch-tasks-stream-trap-exit.test.sh                     PASS
tests/health-check-task-watcher.test.py                        PASS
tests/health-check-supervised-watcher-not-orphan.test.py       PASS
tests/startup-voice-wedge-reap.test.py                         PASS
tests/startup-*.test.sh  (11 files)                            PASS
scripts/review-checks.sh --diff                                PASS (hardcoded-paths clean)
```

## Scope — what this does and does not claim

I found this from a live core where the watcher (pid 63613, up 14h) was draining the correct `tasks/` directory with **no sentinel on disk**, which `health-check.py` reports as `task-watcher warn … wrote no PID sentinel, so health-check cannot track it`. This PR fixes a guard that is missing on its own merits and can produce exactly that state. **I did not capture the reap that removed that particular file** — there is no startup log retaining it — so I am not claiming this PR explains that instance, only that the unguarded delete is a real path to it.

Relationship to #2820 (open, mine): that one is the **recovery** side — `health-check --fix` re-stamps a live watcher's lost sentinel. This is the **prevention** side. Different files (`health-check.py` vs `startup.sh`/`startup-runtime.sh`), no overlap, no merge order between them.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
