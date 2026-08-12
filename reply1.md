Two reviews came back within minutes of the request. Both landed on the same residual window; one also caught a real weakness in my test. Pushed `dd92dec` for the test point, and I'm not fixing the other in this PR — reasoning below.

## The test criticism is correct, and it was worse than "a gap"

> It cannot fail against origin/main's logic. On base the sourced helper does not exist, so the test exits at lines 32-35 before ever exercising the race. A test that skips on base rather than failing does not demonstrate the defect it names.

Right, and the part I'd missed is that the suite *did* contain a base-sensitive assertion — `startup.sh keeps no unguarded copy` — which the early `exit 1` guaranteed it would never reach. So the negative control I put in the PR body was real but the suite itself was not carrying it.

`dd92dec` gates the four function cases on the helper existing and lets the wiring assertions always run.

**Before (`c275737`, against `origin/main` sources):**

```
$ REPO_UNDER_TEST=/private/tmp/reaper-baseline bash tests/startup-watcher-reaper-ownership.test.sh
startup watch-tasks-stream reaper ownership:
  FAIL reap_stale_task_watcher is defined in src/startup-runtime.sh — not found
FAILED (1)
rc=1
```

**After (`dd92dec`, same base sources):**

```
$ REPO_UNDER_TEST=/private/tmp/reaper-baseline bash tests/startup-watcher-reaper-ownership.test.sh
startup watch-tasks-stream reaper ownership:
  FAIL reap_stale_task_watcher is defined in src/startup-runtime.sh — not found
  FAIL startup.sh delegates to the shared reaper — call site not found
  FAIL startup.sh keeps no unguarded copy — the inline rm -f is still there
FAILED (3)
rc=1
```

The third line is the one that matters: on a pre-fix tree the suite now names the defect instead of stopping short of it.

**At HEAD:**

```
$ bash tests/startup-watcher-reaper-ownership.test.sh
startup watch-tasks-stream reaper ownership:
  ok   reap_stale_task_watcher is defined in src/startup-runtime.sh
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

Regressions re-run at `dd92dec`: `watch-tasks-stream-sentinel-ownership` rc=0, `watch-tasks-stream-trap-exit` rc=0, `startup-voice-wedge-reap` rc=0, `scripts/review-checks.sh --diff` PASS.

## The residual window — I'm leaving it, and here is why

Both reviews are right that `cat` then `rm -f` is not an atomic compare-and-unlink, and that a re-stamp landing between those two lines reproduces the state this PR is closing.

I can't close it inside this function. There is no compare-and-unlink primitive available to a shell script, so any real fix changes the **sentinel protocol** — the writer would have to claim the file (`O_EXCL`, or a lock directory) rather than `>`-truncate it, and every reader that keys off the file today (`health-check.py`'s `task-watcher` probe, `services_status.py`, this reaper, and `cleanup()` in `watch-tasks-stream.sh`) would move with it. That is a different change to different files, and bundling it here would make this diff a protocol rewrite wearing a bug-fix commit message.

What the PR does claim is narrower and I think still worth landing: the delete goes from *unconditional* to *conditional on the value the reap actually inspected*. Unconditional loses the sentinel on every startup that races a watcher launch; conditional loses it only when the re-stamp lands inside two adjacent reads with no I/O between them. Same reduction `cleanup()` already took in #2652 — and yes, `@sutando-rui` is right that this matches a pattern which itself carries the residual race. I'd rather have both halves of the policy at the same bar and then raise the bar in one place than leave one half at "no guard at all".

`dd92dec` names the remaining window in the helper's comment so it isn't discovered again from scratch. If a maintainer wants the protocol-level fix as a gate on this PR rather than a follow-up, say so and I'll close this and open the larger one instead — I'd just rather not do it silently.

## One correction to the sandbox report

> the sandbox denied `mktemp`, so the test could not be executed. The `FAILED (2)` it emitted was a harness-environment failure

Noted, and thanks for flagging it as environment rather than reading it as a result. For anyone re-running: the suite needs `mktemp -d` plus the ability to background a short-lived `bash` child (case 2 checks a real process's argv), so a read-only sandbox will fail it for reasons unrelated to the code.
