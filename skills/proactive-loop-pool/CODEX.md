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
| Invocation | `/proactive-loop-pool` slash command passed at launch | prompt injected into the core pane by `src/agent/codex/cli/task-notifier.sh` |
| Task wake-up | `Monitor` tool streaming `src/watch-tasks-stream.sh` in-session | the notifier's external watcher (separate managed tmux session) |
| Periodic sweep | `CronCreate` `*/5 * * * *` registered by the session | **not available** — `src/agent/codex/README.md`: "Codex has no session `CronCreate` surface" |

The third row is the one that changes behaviour rather than just plumbing: a
Codex follower cannot arm its own catch-up sweep, so an assignment the watcher
misses is not retried from inside the session. The sweep has to be driven
externally (the OS-backed cron runner the Codex launcher already reconciles, or
the lead's reconcile loop). Until that is wired, treat a Codex follower as
watcher-driven only.

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

## Acquire

Identical to the Claude entry. Never claim an unassigned task while the lead is
alive — `acquire_work` decides:

```python
import sys; sys.path.insert(0, "src")
from pathlib import Path
from pool_follower import acquire_work
WS = Path(WORKSPACE)
got = acquire_work(WS / "tasks", WS / "state", f"core-{CORE_ID}", "pool-lead")
```

`got` is the claimed path or `None`. `None` means another core holds it — walk
away, do not fall back to reading the unassigned file.

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

## Not yet wired

Stated so nobody reads this file as "Codex followers work today":

- `scripts/install-core-pool.sh` resolves `claude` and injects `POOL_CLAUDE_BIN`;
  it has no runtime dimension, so it cannot install a Codex follower yet.
- `task-notifier.sh` has no pool mode — it still emits the single-core prompt.
- The external periodic sweep described above does not exist.

This file is the entry those three pieces bind to, not a replacement for them.
