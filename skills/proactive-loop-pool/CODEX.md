# Proactive Loop (Pool-Aware) — Codex entry

Codex counterpart of [`SKILL.md`](SKILL.md). **The claim → finish protocol is
runtime-neutral and identical for both runtimes** — it is filesystem state
(atomic renames plus a result file), not a Claude feature. Only the *entry* is
runtime-specific, which is why this is a separate file rather than a fork of the
skill.

Read `SKILL.md` for the protocol. This file states only what a Codex follower
does differently, and what it must not do.

## Why the entry cannot be shared

A Claude follower is launched with `/proactive-loop-pool` as a slash command and
then sets up its own in-session machinery. Codex has neither surface:

| | Claude follower | Codex follower |
|---|---|---|
| Invocation | `/proactive-loop-pool` slash command passed at launch | pool-mode prompt passed as codex's `[PROMPT]` positional by `scripts/pool-worker-wrapper.sh` |
| Task wake-up | `Monitor` tool streaming `src/watch-tasks-stream.sh` in-session | wrapper watches this worker's durable assigned-file state and submits after a positive idle read |
| Periodic sweep | `CronCreate` `*/5 * * * *` registered by the session | the wrapper's external nudge, every 300s |

Both followers are the same persistent shape: an interactive CLI in a tmux
session named `worker-N`, restarted by launchd when the session ends. Only the
argv and the entry differ.

The last two rows are what changes behaviour rather than just plumbing. A Codex
follower cannot arm its own catch-up sweep (`src/agent/codex/README.md`: "Codex
has no session `CronCreate` surface") and has no in-session watcher. Its wrapper
therefore treats `task-*.assigned-worker-N.txt` as a durable pending wake, checks
that state every second, and submits only after the pane is positively idle.
The 300-second sweep remains a leaderless/catch-up backstop.

The assignment wake stays pending while the pane shows `esc to interrupt` and
is retried on the next one-second poll. Codex's interactive input is not a
durable queue, so typing into a running turn can interleave with it. After a
successful send, the wrapper latches that exact assigned path until
`acquire_work` renames or moves it, preventing duplicate prompts.

## The injected prompt must be pool-aware

The single-core notifier prompt ends with `...write the result to
$RESULTS_DIR/$filename`. **A pool follower must not do that.** Writing
`results/` directly skips three things the pool depends on: the claim, the
done-flag, and archiving the claimed file under its canonical name. A result
that appears without a claim also leaves the assignment stranded for the lead to
reclaim.

Pool-mode prompt shape — short, and pointing here rather than restating the
protocol:

```
Sutando pool task ready: <filename>. You are worker-<n>. Do not read the task
file or write results/ directly — follow skills/proactive-loop-pool/CODEX.md:
acquire work first, and complete only through the finish helper.
```

The wrapper's session entry, assignment wake, and periodic nudge use the same
shape without a filename, since none names a specific task — `POOL_CODEX_ENTRY` in
`scripts/pool-worker-wrapper.sh`:

```
Sutando pool mode. You are worker-<n>. Do not read task files or write results/
directly — follow skills/proactive-loop-pool/CODEX.md: acquire work first, and
complete only through the finish helper.
```

## Acquire

Identical to the Claude entry. Never claim an unassigned task while the lead is
alive — `acquire_work` decides:

**Both values come from the environment the wrapper set — never guess them.**
`POOL_WORKSPACE` and `SUTANDO_WORKER_ID` are exported by this worker's launchd
plist. Guessing the workspace is the failure this spells out: the documented
default is `<repo>/workspace`, but the cwd is the repo and the real workspace is
usually configured elsewhere, so a guessed path makes `tasks/` unreadable,
`acquire_work` return `None`, and the sweep report "no assigned work" while an
assignment addressed to this worker sits in the real queue. That reads as an idle
pool, not as a broken one.

```python
import os, sys, subprocess
sys.path.insert(0, "src")
from pathlib import Path
from pool_follower import acquire_work

ws = os.environ.get("POOL_WORKSPACE") or subprocess.run(
    ["bash", "scripts/sutando-config.sh", "workspace"],
    capture_output=True, text=True, check=True).stdout.strip()
worker = os.environ["SUTANDO_WORKER_ID"]
WS = Path(ws)
got = acquire_work(WS / "tasks", WS / "state", worker, "pool-lead")
```

`got` is the claimed path or `None`. `None` means another worker holds it — walk
away, do not fall back to reading the unassigned file.

Sanity check before believing an idle result: `ls "$POOL_WORKSPACE/tasks"`. If
that path does not exist, the resolution above is wrong — fix it rather than
reporting an empty queue.

## Complete

Identical to the Claude entry. The `task: <id>` first line is a pairing check;
the helper refuses with exit 2 and writes nothing if it does not match the
claimed file:

```bash
python3 src/pool_follower.py finish tasks/task-<id>.claimed-worker-<n>.txt $SUTANDO_WORKER_ID <<'EOF'
task: <id>
<result body>
EOF
```

End every user-facing body with `— worker-<n>` (formerly core-<n>; owner renamed 2026-08-31), plain text, never bracketed.

## What a Codex follower must not do

Same prohibitions as the Claude entry, repeated because the Codex prompt path
does not carry them:

- Do not write `results/<file>` directly — always go through `finish`.
- Do not write `core-status.json`; that file belongs to the main core.
- Do not register the host cron set; the lead owns it.
- Do not start `core_heartbeat.py` — liveness is the wrapper's job.

## How a Codex follower is installed

```bash
bash scripts/install-worker-pool.sh 3 --worker-runtime=3:codex   # worker-3 only
bash scripts/install-worker-pool.sh 3 --runtime=codex            # the whole pool
```

`--runtime` (or `$SUTANDO_POOL_RUNTIME`) sets the default; `--worker-runtime=<N>:<rt>`
overrides one worker. Supported names match `src/agent/start-cli.sh`'s allowlist —
anything else exits 2 rather than falling back to Claude. The installer resolves
the runtime's absolute binary and the Codex config store
(`sutando-config.sh core-config-dir-{env-name,value} codex`) and injects
`POOL_RUNTIME`, `POOL_RUNTIME_BIN`, `POOL_RUNTIME_CONFIG_ENV` and
`POOL_RUNTIME_CONFIG_DIR` into that worker's plist. A worker with nothing specified
stays Claude with exactly the environment it had before the dimension existed.

## Wired / not wired

Stated so nobody reads this file as "Codex followers are at parity":

Wired:

- `scripts/install-worker-pool.sh` has a per-worker runtime dimension and installs a
  Codex follower (above).
- `scripts/pool-worker-wrapper.sh` dispatches on `POOL_RUNTIME` and launches Codex
  with the flags `src/agent/codex/cli/start-cli.sh` uses, plus the pool entry.
- Assignment-aware wake-up, driven by the wrapper from the durable assigned
  filename and held until the Codex pane is idle.
- The catch-up sweep, driven externally by the wrapper at 300s.

Not wired:

- `task-notifier.sh` has no pool mode — it still emits the single-core prompt.
  Pool wake-up is wrapper-owned and assignment-state-driven rather than using
  the single-core notifier.
- No live install has been exercised: the plists and the launch argv are
  generated and tested, no Codex follower has been booted under launchd.

This file is the entry those pieces bind to, not a replacement for them.
