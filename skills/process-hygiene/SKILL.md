---
name: process-hygiene
description: "Report, and with --reap stop, orphaned processes that interrupted agent test runs left behind in per-session scratchpads (PPID 1, this user, older than 2 h by default, cwd inside <tmp>/claude-<uid>/<project>/<session>/scratchpad, and that session has ended). --spotlight keeps Spotlight from indexing scratchpad churn. For Sutando developers only; not scheduled. Use when the Mac is noisy or loaded with leftover test processes."
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
0 for every run that parsed its arguments, so a scheduled caller never retries in a loop; 2
on a usage error.

## What is a candidate

ALL of:

1. owned by the user running the script;
2. PPID 1 — whatever started it is gone;
3. elapsed time over `PROCESS_HYGIENE_MIN_AGE_S` (default 7200);
4. its working directory (`lsof -a -p PID -d cwd -Fn` on macOS, `/proc/PID/cwd`
   on Linux), as a realpath, is inside a scratchpad root matching
   `/private/tmp/claude-<uid>/*/*/scratchpad` or `/tmp/claude-<uid>/*/*/scratchpad`.
   A root is used only when `claude-<uid>` is a real directory owned by this
   user, nothing between it and `scratchpad` is a symlink, and the resolved root
   sits under the resolved `claude-<uid>`; a symlinked scratchpad would otherwise
   make its target (a repo, the workspace) a root, for reaping and for
   `--spotlight` alike;
5. the Claude Code session that owns that scratchpad has **ended** (below);
6. **no process in its process group holds a server socket** — a listening TCP
   socket or any UDP socket (`lsof -w -a -p PID -iTCP -sTCP:LISTEN -iUDP` on
   macOS; the pid's socket inodes against `/proc/PID/net/{tcp,tcp6,udp,udp6}` on
   Linux). A dev server, database or tunnel an agent started on purpose is spared
   even after its session ended. If the sockets cannot be read for any member,
   the whole group is spared.

**The owning session.** A scratchpad is
`<tmp>/claude-<uid>/<project-slug>/<session-id>/scratchpad`. A live session may
have started a server there on purpose, so its orphans are never candidates. For
each Claude config dir (`CLAUDE_CONFIG_DIR`, the home `.claude`, and every
`claude` entry of `core_config_dirs`; `--claude-config-dir` replaces the list):

- **alive** — a session marker `<config>/sessions/<pid>.json` names the session
  (`sessionId`) and its `pid` is running with the marker's `procStart` (compared
  with `ps -o lstart=` under `TZ=UTC` and the C locale, so a reused pid is not
  mistaken for the session), or the session transcript
  `<config>/projects/<slug>/<session-id>.jsonl` changed within the minimum age;
- **gone** — that transcript exists, a `sessions/` directory exists to have held
  a marker, neither alive condition holds, and no live Claude process of the same
  project could still own the scratchpad after a `/clear` (below);
- **unknown** — anything else: no transcript in any known config dir, a marker or
  `sessions/` directory that cannot be read, a marker without `pid`/`procStart`.
  Unknown is treated as alive: never reaped.

**`/clear`.** Measured with a throwaway interactive `claude`: `/clear` rewrites the
running process's marker with a **new** `sessionId` (same `pid`, same
`procStart`), starts a new session directory and transcript, and leaves the old
session's scratchpad in place. Nothing on disk links the new id back to the old
one (the new transcript does not name it; the process holds nothing open under
the old session directory). So the old scratchpad, still the cwd of anything the
session started, would read gone while its owner is alive. The rule that covers
it: if any **live** marker (pid running with its `procStart`) belongs to the same
project (its `cwd` mapped to a project name the way Claude Code does, every
non-alphanumeric character to `-`) and that process started no later than
`CLEAR_SLACK_S` (120 s) after the session directory was created, the owner is
**unknown** and the scratchpad is spared. A process started after the directory
existed cannot have owned it. A live marker with no `cwd` or no `procStart`
counts as the same project. The consequence: on a host with a long-running
session in a project, no ended session of that project created during its
lifetime is reaped.

Claude Code writes the marker while a session runs and it is the only per-session
liveness record on disk; a session's main process does not hold its transcript
open, and its task-output files are open only while a background command runs,
so neither of those can tell an idle live session from an ended one.

**Never touched, whatever else holds.** These are matched on the command line
(`PROTECTED_SUBSTRINGS` and `PROTECTED_EXECUTABLES` in the script). They are
normally safe anyway, because their cwd is not a scratchpad; the list is what
keeps them safe when a Sutando checkout is run from a scratchpad worktree.

| Matched | Why |
|---|---|
| executable `tmux`, `claude` (also `-claude`), `launchd` | terminal multiplexers and agent sessions own other work |
| `claude-code` | the Claude Code node CLI |
| `sutando-core`, `sutando-worker`, `sutando-tmux`, `sutando-relay`, `sutando-station` | the core, workers, their tmux sockets, relays and the Station |
| `watch-tasks-stream.sh`, `task-notifier.sh` | the task watcher and the Claude and Codex task notifiers |
| `voice-agent`, `core_heartbeat` | the voice agent and the core liveness writer |
| `run-bridge.sh`, `remote_gateway_bridge`, `-bridge.py` | relay and channel bridges (Discord, Telegram, Slack, the remote gateway) |
| `mcp-server`, `mcp_server`, `modelcontextprotocol`, `-mcp` | MCP servers |
| `Google Chrome`, `Chromium`, `chrome-headless-shell`; executables `chrome`, `chromium`, `chromium-browser`, `google-chrome` | browsers, including test-automation shells |
| an executable at `/Applications/<App>.app/Contents/MacOS/…` or the same under `~/Applications` | the user's apps (a toolchain inside an app bundle's `Contents/Developer`, such as Xcode's `swift-frontend` or its `python3`, is not an app) |

Also never touched: every pid `launchctl list` reports, this script and its
ancestors. A process outside this list that matches every rule above is a
candidate; the list only ever adds exclusions.

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
no Sutando session-start hook to add the marker at creation; each run covers
the ones created since the last. Whether Spotlight honours the marker below a
volume root depends on the macOS release (unverified on every release); if
`mdworker` keeps indexing the temp tree, add `/private/tmp` to System Settings →
Spotlight → Privacy instead.

## Developer-only, not scheduled

This skill is for Sutando developers, whose agent sessions run test suites. It
is not added to `skills/schedule-crons/crons.example.json`: new installs copy
that file, and an hourly job there would reach every user. Run it by hand:

```bash
python3 skills/process-hygiene/scripts/reap_orphans.py --spotlight          # report only
python3 skills/process-hygiene/scripts/reap_orphans.py --reap --spotlight   # stop candidates
```

Its triggers come in a follow-up: a health-check load warning that runs it once
when the cause matches, and the end of witness scripts.

`--reap` refuses a minimum age under 600 s (exit 2), so a typo cannot reap young
work. A run holds an exclusive `flock` on `process-hygiene.lock` beside its log;
a second, concurrent run prints that and does nothing.

## Config

Declared in `manifest.json`; read CLI > environment > manifest:

| Key | Default | CLI |
|---|---|---|
| `PROCESS_HYGIENE_MIN_AGE_S` | `7200` | `--min-age` |
| `PROCESS_HYGIENE_GRACE_S` | `5` | `--grace` |

`--claude-config-dir DIR` (repeatable) replaces the config dirs searched for
session markers and transcripts. `--tmp-base DIR` (repeatable) replaces the default `/tmp` search base (on macOS
a symlink, so its realpath is covered too); `--log PATH` replaces the workspace log.
