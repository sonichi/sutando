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
| Invocation | `/proactive-loop-pool` slash command passed at launch | pool-mode prompt passed as codex's `[PROMPT]` positional by `scripts/pool-core-wrapper.sh` |
| Task wake-up | `Monitor` tool streaming `src/watch-tasks-stream.sh` in-session | **none in-session** — only the wrapper's periodic nudge |
| Periodic sweep | `CronCreate` `*/5 * * * *` registered by the session | the wrapper's external nudge, every 300s |

Both followers are the same persistent shape: an interactive CLI in a tmux
session named `core-N`, restarted by launchd when the session ends. Only the
argv and the entry differ.

The last two rows are what changes behaviour rather than just plumbing. A Codex
follower cannot arm its own catch-up sweep (`src/agent/codex/README.md`: "Codex
has no session `CronCreate` surface") and has no in-session watcher, so the
wrapper drives the sweep from outside at the same 5-minute cadence the Claude
follower registers with `CronCreate`. That is the whole of a Codex follower's
wake-up: **assignment latency is up to 300s**, against sub-second for a Claude
follower. Nothing is dropped, but nothing is instant either.

The nudge is skipped while the pane shows `esc to interrupt` and retried on the
next poll — Codex's interactive input is not a durable queue, so typing into a
running turn can interleave with it.

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
Sutando pool task ready: <filename>. You are core-<n>. Do not read the task
file or write results/ directly — follow skills/proactive-loop-pool/CODEX.md:
acquire work first, and complete only through the finish helper.
```

The wrapper's session entry and its periodic nudge use the same shape without a
filename, since neither names a specific task — `POOL_CODEX_ENTRY` in
`scripts/pool-core-wrapper.sh`:

```
Sutando pool mode. You are core-<n>. Do not read task files or write results/
directly — follow skills/proactive-loop-pool/CODEX.md: acquire work first, and
complete only through the finish helper.
```

## Acquire

Identical to the Claude entry. Never claim an unassigned task while the lead is
alive — `acquire_work` decides:

**Both values come from the environment the wrapper set — never guess them.**
`POOL_WORKSPACE` and `SUTANDO_CORE_ID` are exported by this core's launchd
plist. Guessing the workspace is the failure this spells out: the documented
default is `<repo>/workspace`, but the cwd is the repo and the real workspace is
usually configured elsewhere, so a guessed path makes `tasks/` unreadable,
`acquire_work` return `None`, and the sweep report "no assigned work" while an
assignment addressed to this core sits in the real queue. That reads as an idle
pool, not as a broken one.

```python
import os, sys, subprocess
sys.path.insert(0, "src")
from pathlib import Path
from pool_follower import acquire_work

ws = os.environ.get("POOL_WORKSPACE") or subprocess.run(
    ["bash", "scripts/sutando-config.sh", "workspace"],
    capture_output=True, text=True, check=True).stdout.strip()
core = f"core-{os.environ['SUTANDO_CORE_ID']}"
WS = Path(ws)
got = acquire_work(WS / "tasks", WS / "state", core, "pool-lead")
```

`got` is the claimed path or `None`. `None` means another core holds it — walk
away, do not fall back to reading the unassigned file.

Sanity check before believing an idle result: `ls "$POOL_WORKSPACE/tasks"`. If
that path does not exist, the resolution above is wrong — fix it rather than
reporting an empty queue.

## Complete

Identical to the Claude entry. The `task: <id>` first line is a pairing check;
the helper refuses with exit 2 and writes nothing if it does not match the
claimed file:

```bash
python3 src/pool_follower.py finish tasks/task-<id>.claimed-core-<n>.txt core-<n> <<'EOF'
task: <id>
<result body>
EOF
```

End every user-facing body with `— core-<n>`, plain text, never bracketed.

## What a Codex follower must not do

Same prohibitions as the Claude entry, repeated because the Codex prompt path
does not carry them:

- Do not write `results/<file>` directly — always go through `finish`.
- Do not write `core-status.json`; that file belongs to the main core.
- Do not register the host cron set; the lead owns it.
- Do not start `core_heartbeat.py` — liveness is the wrapper's job.

## How a Codex follower is installed

```bash
bash scripts/install-core-pool.sh 3 --core-runtime=3:codex   # core-3 only
bash scripts/install-core-pool.sh 3 --runtime=codex          # the whole pool
```

`--runtime` (or `$SUTANDO_POOL_RUNTIME`) sets the default; `--core-runtime=<N>:<rt>`
overrides one core. Supported names match `src/agent/start-cli.sh`'s allowlist —
anything else exits 2 rather than falling back to Claude. The installer resolves
the runtime's absolute binary and the Codex config store
(`sutando-config.sh core-config-dir-{env-name,value} codex`) and injects
`POOL_RUNTIME`, `POOL_RUNTIME_BIN`, `POOL_RUNTIME_CONFIG_ENV` and
`POOL_RUNTIME_CONFIG_DIR` into that core's plist. A core with nothing specified
stays Claude with exactly the environment it had before the dimension existed.

## Wired / not wired

Stated so nobody reads this file as "Codex followers are at parity":

Wired:

- `scripts/install-core-pool.sh` has a per-core runtime dimension and installs a
  Codex follower (above).
- `scripts/pool-core-wrapper.sh` dispatches on `POOL_RUNTIME` and launches Codex
  with the flags `src/agent/codex/cli/start-cli.sh` uses, plus the pool entry.
- The catch-up sweep, driven externally by the wrapper at 300s.

Not wired:

- `task-notifier.sh` has no pool mode — it still emits the single-core prompt.
  A Codex follower therefore has no event-driven wake-up at all; the 300s nudge
  is its only one.
- `scripts/kick-pool.sh` reads Claude's REPL markers (`❯ /proactive-loop-pool`)
  and sends Claude's keystrokes, so the recovery watchdog cannot kick a hung
  Codex follower — it only sees the shared `esc to interrupt` busy marker and
  the launchd `kickstart` path for a dead session.
- No live install has been exercised: the plists and the launch argv are
  generated and tested, no Codex follower has been booted under launchd.

This file is the entry those pieces bind to, not a replacement for them.
