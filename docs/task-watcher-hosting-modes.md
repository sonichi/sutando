# Task watcher hosting modes: session watcher, standby, supervisor

One inbox (`<workspace>/tasks/` for the core; `<workspace>/deliveries/<id>/` for a pool worker) is
meant to have **exactly one announcer** in steady state. Two things can host that announcer, and the
watcher itself refuses to double an inbox at its own startup (`inbox-holders`, below), so no
launcher or instruction has to get the check right. The remaining exception is that a handoff can
produce two notifications for one pending task; it is named under "Known issues and gaps" with the
issue that closes it. This page is the design as it stands on `main` after #4477, #4585 and #4602;
the behaviour is pinned by the tests named at the end.

## The two hosting modes

| mode | who runs the watcher | how tasks reach the agent | when it is used |
|---|---|---|---|
| **session watcher** | the agent's own CLI session, through the `Monitor` tool: `bash src/watch-tasks-stream.sh --role session --inbox <inbox>` | the tool delivers each `TASK_FILE: <name>` line straight into the session | the normal state of a live core |
| **standby** (external) | `task-notifier-supervisor.sh`, running in the tmux session `<core>-watcher`, starts `task-notifier.sh`, which runs the same watcher script untagged as its child | the notifier types one instruction line into the core's tmux pane: `Sutando task ready: <file>. Read <tasks>/<file>, follow CLAUDE.md, complete the task, and write the result to <results>/<file>.` | whenever no session watcher covers the inbox: before `/startup` finishes, after a session watcher dies, when the core cannot run `Monitor` |

The watcher script decides everything about a task (routing through the task-event handler, priority,
dedupe, holds); the notifier only executes what the watcher announces (#4561). So the two modes differ
in how an announcement reaches the agent, never in what is announced.

## The tag, and why it matters

A session watcher is started **tagged**: `--role session --inbox <inbox>`. The tag is how every other
party sees it:

- `src/watcher_identity.py role-present session --inbox <inbox> [--ready <state dir>]` answers
  `yes` / `no` / `unknown` for that inbox from one `ps` snapshot. With `--ready`, a process counts only
  once the sentinel under `<state dir>` names its pid.
- `src/watcher_identity.py standby-present --inbox <inbox>` answers the same for any watcher on the
  inbox that is **not** session-role: the external standby, or an untagged one.

An **untagged** watcher (the positional form `watch-tasks-stream.sh <inbox>`, which is how the standby's
notifier and, today, a pool worker's watcher run) is invisible to `role-present`. That is by design for
the standby, and it is the known gap for anything else: see "Known gaps".

The sentinel is `<workspace>/state/watch-tasks-stream-<agent id>[+<instance id>].pid`
(`util_paths.watcher_sentinel_path`): one file per instance on a pool host, holding the pid of the
watcher that stamped it.

## Readiness: the session watcher proves it can deliver before the standby leaves

A watcher process that exists is not yet a watcher that delivers: `fswatch` has to be running and
routing events to it. So the session watcher, before it stamps its sentinel, writes a probe file
`.ready-<pid>-<n>` into its inbox and waits for that file's event to come back through `fswatch`;
only after the round-trip does it stamp `<pid>` into the sentinel. `role-present --ready` is therefore
the question "is a session watcher on this inbox able to deliver right now?", and it is the question
the supervisor asks.

Events that arrive during the probe window are buffered and replayed **before** the startup sweep, in
`fswatch` order, so a task never sees a handler config that was published after it (#4585).

## The supervisor's contract (`task-notifier-supervisor.sh`)

The script is `src/agent/codex/cli/task-notifier-supervisor.sh`; despite the directory it is
runtime-agnostic (parameterised by `SUTANDO_NOTIFIER_SCRIPT`), and the Claude launcher pairs it
with the Claude notifier. Started by the launcher (`start-cli.sh`) for the core's inbox, in tmux
session `<core>-watcher`; the supervisor, the notifier and the standby watcher all live in that
session, so nothing that runs inside the core session may kill it (#4585 removed the one kill that
did).

1. **Standby.** Poll `role-present session --inbox <inbox> --ready <state>` every `ROLE_POLL` (5 s).
2. **Arm.** After `GRACE_PERIOD` (45 s) of continuous `no`, start the notifier, which starts the
   untagged standby watcher. The inbox is now announced through the pane.
3. **Stand down.** The moment the verdict is `yes` (a session watcher has proved readiness), stop the
   notifier and its watcher. The session watcher does not kill anything itself; it waits up to
   `SUTANDO_STANDBY_STOP_TIMEOUT` (15 s) for the standby to be gone, then runs its startup sweep.
4. **Re-arm.** If the verdict returns to `no` for `GRACE_PERIOD` again (the session watcher died, the
   `Monitor` expired), arm again. The supervisor is the recovery owner; its pid does not change
   across handoffs.

Measured on the Pro host on 2026-09-22 (the #4585 witness, live core, no restart): standby armed at
50 s; handoff to the session watcher 18 s after it started; a task answered 12 s later with the
notifier typing nothing; after the session watcher was killed, the standby was re-armed in 51 s under
the same supervisor pid; the second handoff took 12 s.

## What each party may do

- The **session** (the agent) starts its session watcher at boot and re-arms it when the loop finds
  the inbox unwatched. It never stops a standby, never stops the supervisor's session, and never
  starts a watcher on an inbox that already has one (`role-present` `yes`, or `standby-present` `yes`).
- The **supervisor** arms and stands down the standby, and only the standby.
- The **launcher** starts the supervisor; `--restart` recycles the core session and the supervisor
  session together.
- **Nothing** runs a host-wide `pkill` of watchers: on a pool host each instance owns one watcher on
  its own inbox, and the core's tools scope every question to its own inbox.

## Known issues and gaps (with their issues)

- **Two notifications per handoff, at most.** A core-bound task pending during a handoff can be
  announced by the standby's notifier and by the session watcher's sweep. Accepted as a known issue by
  the owner; the announce-once marker keyed to the core session (PR-B) is deferred.
- **The watcher's own startup check (#4602).** Before it touches anything, `watch-tasks-stream.sh`
  asks `watcher_identity.py inbox-holders --inbox <inbox>` for every watcher-shaped process naming its
  inbox, tagged or not, ready or not. A second watcher of the same kind exits 0 naming the holder; a
  session watcher over a standby proceeds (the supervisor stands the standby down once it proves
  ready); a standby over a session watcher exits 0; an unobservable `ps` refuses to start. Only
  `--force-restart` replaces the holder (TERM, then KILL, then its fswatch child), and only on the
  owner's word. An untagged start is warned about and treated as standby-kind for the check;
  refusing it outright waits for the remaining positional launches to be tagged. The supervisor's
  standby watcher is started `--role standby`. `restart.sh` no longer pattern-kills watchers.
- **Workers' watchers are unsupervised.** A pool worker's watcher is started by its session and nothing
  outside re-arms it; the pool supervisor is to give each worker inbox the same contract: #4600.
- **`Monitor` expiry, on some builds.** The skills pass `persistent: true`; a build whose `Monitor`
  exposes that argument keeps the watcher for the session. A build without it caps `timeout_ms` at
  30 minutes and ends the watcher at each expiry unless the session re-arms it, with the supervisor's
  standby covering the gap after 45 s. Measured on a bundled non-git install (engine `3ab5e26da`,
  2026-09-21) and on the Pro host's core the same day; builds that expose `persistent` are not
  affected: #4524.
- **Untagged is what pool workers run today** (`SUTANDO_WATCHER_CMD <inbox>` on hosts whose worker
  boot skill predates the tagged form), which is what #4600 closes. The rule going forward (owner,
  2026-09-22): every start carries an explicit `--role` (`session` or `standby`) and `--inbox`; the
  watcher warns on an untagged start today and will refuse it once every launch is tagged.

## Tests that pin this

- `tests/watch-tasks-stream-role-session-kills-standby.test.sh`: the handoff contract with the real
  supervisor and notifier (arm after grace, stand down on readiness, re-arm after a kill).
- `tests/watch-tasks-stream-readiness-window-honours-handler-config.test.py`: the readiness window,
  buffered replay before the sweep, holds and admissions.
- `tests/watcher-identity.test.py`: the verdicts, their inbox scoping and the ready gate.
- `tests/start-cli-claude-task-notifier.test.py`: the launcher starts the supervisor for the core.
