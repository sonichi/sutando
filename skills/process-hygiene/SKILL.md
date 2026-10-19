---
name: process-hygiene
description: "Report, and with --reap stop, orphaned processes that interrupted agent test runs left behind in per-session scratchpads (PPID 1, this user, older than 2 h by default, cwd inside <tmp>/claude-<uid>/<project>/<session>/scratchpad). --spotlight keeps Spotlight from indexing scratchpad churn. Use when the Mac is noisy or loaded with leftover test processes, or schedule it hourly."
user-invocable: true
---

# Process hygiene

Agent sessions run tests from their per-session scratchpads. When a run is
interrupted or times out, background children it started are re-parented to
init and keep running: spin loops waiting on a file that will never appear,
fixture servers, hung interpreters. They accumulate across sessions for days.
This skill finds them by provenance, not by name, and stops them one target at
a time.

**Usage**

```bash
python3 skills/process-hygiene/scripts/reap_orphans.py               # report only
python3 skills/process-hygiene/scripts/reap_orphans.py --reap        # stop candidates
python3 skills/process-hygiene/scripts/reap_orphans.py --spotlight   # Spotlight markers only
```

Every run appends one line per action and a summary to
`<workspace>/logs/process-hygiene.log` and prints the summary. The exit status is
0 for every run that parsed its arguments, so a cron never retries in a loop; 2
on a usage error.

## What is a candidate

ALL of:

1. owned by the user running the script;
2. PPID 1 — whatever started it is gone;
3. elapsed time over `PROCESS_HYGIENE_MIN_AGE_S` (default 7200);
4. its working directory (`lsof -a -p PID -d cwd -Fn` on macOS, `/proc/PID/cwd`
   on Linux), as a realpath, is inside a scratchpad root matching
   `/private/tmp/claude-<uid>/*/*/scratchpad` or `/tmp/claude-<uid>/*/*/scratchpad`.

Never touched, whatever else holds: tmux servers, `claude` sessions (and the
Claude Code node CLI), anything whose command names a Sutando core, worker, tmux
socket or relay (`sutando-core`, `sutando-worker`, `sutando-tmux`,
`sutando-relay`, `run-bridge.sh`, `remote_gateway_bridge`), Chrome/Chromium, apps
under `/Applications`, this script and its ancestors.

**Launchd jobs.** On macOS a launchd job also has PPID 1, so PPID alone cannot
tell an orphan from a service. Two independent rules keep services out: every
pid `launchctl list` reports is skipped, and a launchd job's working directory
is `/` or its plist's `WorkingDirectory`, never a per-session scratchpad. The
second rule holds even when `launchctl` cannot be read, which is why the
scratchpad cwd is a hard requirement rather than a hint.

**Not covered, on purpose.** A leaked process whose cwd is the repo or `/` is
left alone even when it is clearly a test fixture, because nothing about its
provenance distinguishes it from a service the owner started there. Those are
fixed at the source (the test cleans up its own process group). On Linux hosts
whose orphans are re-parented to a subreaper (for example a `systemd --user`
instance) rather than PID 1, nothing qualifies.

## How a candidate is stopped

Per target, never by pattern:

1. Re-read pid, start time (`ps -o lstart=` under the C locale), cwd and
   command immediately before signalling. Any difference aborts the target, so
   a pid reused by another process is never signalled.
2. SIGTERM the process **group** only when every live member of that group is
   itself a candidate; otherwise SIGTERM the single pid. If the group gained or
   lost a member since the scan, each unchanged member is handled on its own.
3. Wait up to `PROCESS_HYGIENE_GRACE_S` (default 5).
4. Re-check again; SIGKILL the survivors only if they are still identical.

## Spotlight

`--spotlight` creates an empty `.metadata_never_index` in each existing
scratchpad root and in each `claude-<uid>` directory above them, skipping any
that already has one. Scratchpads are created by Claude Code itself, so there is
no Sutando session-start hook to add the marker at creation; the hourly run
covers new ones within the hour. Whether Spotlight honours the marker below a
volume root depends on the macOS release (unverified on every release); if
`mdworker` keeps indexing the temp tree, add `/private/tmp` to System Settings →
Spotlight → Privacy instead.

## Scheduling

Hourly, off the top of the hour, as an OS-level shell job (it needs no model
session). Add to `<workspace>/hosts/<host-label>/crons.json` (see
`skills/schedule-crons/crons.example.json`):

```json
{
  "name": "process-hygiene",
  "cron": "23 * * * *",
  "launchd": true,
  "shell_command": "PY=\"$(bash scripts/sutando-config.sh python-bin)\" && \"$PY\" skills/process-hygiene/scripts/reap_orphans.py --reap --spotlight"
}
```

`python-bin` resolves a verified interpreter; a bare `python3` under launchd's
`PATH` can be the Xcode Command Line Tools stub. It needs the OS-level runner
(`bash src/install-cron-runner-launchd.sh`). Run the script once in report mode
first and read what it would stop.

## Config

Declared in `manifest.json`; read CLI > environment > manifest:

| Key | Default | CLI |
|---|---|---|
| `PROCESS_HYGIENE_MIN_AGE_S` | `7200` | `--min-age` |
| `PROCESS_HYGIENE_GRACE_S` | `5` | `--grace` |

`--tmp-base DIR` (repeatable) replaces the default `/tmp` search base (on macOS
a symlink, so its realpath is covered too); `--log PATH` replaces the workspace log.
