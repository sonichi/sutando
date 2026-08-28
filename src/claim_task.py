#!/usr/bin/env python3
"""Atomic-rename claim primitive for the multi-core agent pool (#880, #884).

A new task lands as `<workspace>/tasks/task-<id>.txt`. A core session claims
it by atomic-rename:

    tasks/task-<id>.txt -> tasks/task-<id>.claimed-core-<n>.txt

POSIX `rename()` on the same filesystem is atomic — no two callers both
succeed. The losing caller sees `FileNotFoundError` and walks away.

Within one filesystem this collapses #872's cross-Mac last-rename-wins to
first-rename-wins. Single-Mac strict subset of the same coordination
protocol; same primitive, no sync gap.

CLI:
    python3 src/claim_task.py <task-id> <core-id> [<channel-id>]

Exit codes:
    0 — claim succeeded; new path printed to stdout
    1 — claim failed (task already claimed by another core, or doesn't exist,
        OR this core is not the channel's current handler and the handler is
        still alive — i.e. channel-affinity is respected)
    2 — usage error

Library:
    from claim_task import claim_plain, claim_with_affinity
    claimed_path = claim_plain(task_id, core_id, workspace=None)
    claimed_path = claim_with_affinity(task_id, core_id, channel_id, workspace=None)

LEAD-FOLLOWER NOTE (L1+): `claim_with_affinity` is the LEGACY race-side
    policy, kept verbatim as the degraded/fallback mode when the lead beat is
    stale. Under a live lead, affinity is decided lead-side (pool_lead.py) and
    followers claim only their `.assigned-<inst>` files.

    Channel-affinity (#884):
    `claim_with_affinity()` adds sticky-handler semantics. The first core to
    claim a task from channel-X becomes the channel's handler; subsequent
    tasks from channel-X land on that core within IDLE_THRESHOLD (30 min by
    default, env: SUTANDO_CORE_IDLE_THRESHOLD_SEC). If the handler core's
    .alive heartbeat goes stale (>90s) — i.e. it crashed — any core may
    claim again, auto-rebalancing. After IDLE_THRESHOLD of channel
    inactivity, the handler entry expires and the next task races freely.

    `channel_id=None` is the no-affinity fallback (voice tasks, etc.),
    delegating to plain `claim_plain()`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402
from heartbeat_freshness import age_is_fresh  # noqa: E402


def _validate_id(name: str, kind: str) -> str:
    """Allow only alphanumerics + `-` + `_` + `.`. Reject path separators
    and traversal attempts so a hostile `task_id` can't escape `tasks/`."""
    if not name:
        raise ValueError(f"empty {kind}")
    bad = set(name) - set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if bad:
        raise ValueError(f"invalid {kind}: contains {sorted(bad)!r}")
    if name in (".", "..") or name.startswith("."):
        raise ValueError(f"invalid {kind}: dot-prefixed names not allowed")
    return name


def claim_plain(task_id: str, core_id: str, workspace: Path | None = None) -> Path | None:
    """Attempt to claim a task by atomic-rename.

    Returns the path to the claim file on success, or None if the claim
    failed (task already claimed or missing). Does NOT raise on race-loss —
    that's a normal outcome, not an error.

    Raises ValueError on invalid task_id / core_id input.
    """
    task_id = _validate_id(task_id, "task_id")
    core_id = _validate_id(core_id, "core_id")
    ws = workspace if workspace is not None else resolve_workspace()
    src = ws / "tasks" / f"task-{task_id}.txt"
    dst = ws / "tasks" / f"task-{task_id}.claimed-core-{core_id}.txt"
    try:
        # POSIX rename: atomic on same filesystem. If src disappeared
        # because another core already claimed it, we get FileNotFoundError.
        os.rename(src, dst)
        return dst
    except FileNotFoundError:
        return None
    except OSError as exc:
        # A lost race and a core that can NEVER claim both return None; the
        # errno is the only thing separating them, so say it before dropping it.
        print(f"claim_task: unexpected errno {exc.errno} claiming "
              f"{src.name}: {exc.strerror}", file=sys.stderr)
        return None


# Channel-affinity layer (#884): sticky-handler semantics over plain
# claim_plain(); only invoked when the caller passes channel_id.

DEFAULT_IDLE_THRESHOLD_SEC = 30 * 60   # 30 min — chat-pattern cohesion
ALIVE_THRESHOLD_SEC = 90               # 3 missed 30s heartbeats = dead

import json
import time


def _idle_threshold_sec() -> int:
    """Read SUTANDO_CORE_IDLE_THRESHOLD_SEC env var, fallback to default.
    Non-numeric / negative inputs fall back to the default."""
    raw = os.environ.get("SUTANDO_CORE_IDLE_THRESHOLD_SEC")
    if not raw:
        return DEFAULT_IDLE_THRESHOLD_SEC
    try:
        val = int(raw)
        if val <= 0:
            return DEFAULT_IDLE_THRESHOLD_SEC
        return val
    except ValueError:
        return DEFAULT_IDLE_THRESHOLD_SEC


def _validate_channel_id(channel_id: str) -> str:
    """Same allowlist as task/core IDs — only alphanumerics + `-` + `_` + `.`.
    Reject path separators so a hostile channel_id can't escape state/cores/."""
    return _validate_id(channel_id, "channel_id")


def _handler_path(ws: Path, channel_id: str) -> Path:
    return ws / "state" / "cores" / f"channel-{channel_id}.handler"


def _alive_path(ws: Path, core_id: str) -> Path:
    return ws / "state" / "cores" / f"core-{core_id}.alive"


def _read_handler(path: Path) -> dict | None:
    """Read the handler JSON. Return None on any error (missing, malformed,
    permission). Treating any read failure as 'no handler' keeps the algorithm
    forward-progressing — at worst a transient read error causes a single
    extra race-claim, never a deadlock."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_handler(path: Path, core_id: str, when: float) -> None:
    """Write the handler entry atomically (temp file + rename) so a concurrent
    reader never sees a half-written JSON. Best-effort: errors swallowed —
    a missed handler-write doesn't break the claim, just means the next core
    might race-claim the next task on this channel. Trade-off in favor of
    progress over strict accounting."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps({"core_id": core_id, "last_handled_at": when}))
        os.rename(tmp, path)  # atomic on same FS
    except OSError:
        pass


def _is_alive(ws: Path, core_id: str, now: float) -> bool:
    """A core is alive if its .alive heartbeat age is inside
    [-tolerance, ALIVE_THRESHOLD_SEC). If the file is missing, treat as dead — newly-started
    cores write the heartbeat before claiming, so a missing file means
    'never started' or 'cleanly shut down,' both of which release the channel."""
    p = _alive_path(ws, core_id)
    try:
        mtime = p.stat().st_mtime
    except (FileNotFoundError, OSError):
        return False
    return age_is_fresh(now - mtime, ALIVE_THRESHOLD_SEC)


def claim_with_affinity(
    task_id: str,
    core_id: str,
    channel_id: str | None,
    workspace: Path | None = None,
) -> Path | None:
    """Claim a task, honoring per-channel sticky-handler affinity (#884).

    Semantics:
      - channel_id=None → delegate to plain claim_plain() (no affinity).
      - Channel has a fresh handler (within IDLE_THRESHOLD) AND that handler
        is alive (heartbeat < 90s) AND it's NOT this core: return None
        (respect the handler).
      - Channel has a fresh handler AND that handler is alive AND it IS this
        core: claim normally + update handler.last_handled_at on success.
      - Channel has a stale handler (idle expired OR handler core dead) OR
        no handler: race-claim; on success write/refresh the handler with
        this core_id + now.

    Returns the claim path on success, None on lost race / respect-handler.
    Raises ValueError on invalid inputs.
    """
    task_id = _validate_id(task_id, "task_id")
    core_id = _validate_id(core_id, "core_id")
    if channel_id is None:
        return claim_plain(task_id, core_id, workspace=workspace)
    channel_id = _validate_channel_id(channel_id)

    ws = workspace if workspace is not None else resolve_workspace()
    now = time.time()
    handler_p = _handler_path(ws, channel_id)
    handler = _read_handler(handler_p)
    idle_threshold = _idle_threshold_sec()

    handler_is_fresh = (
        handler is not None
        and isinstance(handler.get("last_handled_at"), (int, float))
        and age_is_fresh(now - handler["last_handled_at"], idle_threshold)
    )

    if handler_is_fresh:
        handler_core = str(handler.get("core_id", ""))
        if not handler_core:
            # Malformed handler — treat as no handler, race-claim.
            handler_is_fresh = False
        elif _is_alive(ws, handler_core, now):
            # Handler is fresh AND alive — respect it.
            if core_id != handler_core:
                return None
            # I am the handler — claim normally + refresh handler mtime.
            result = claim_plain(task_id, core_id, workspace=ws)
            if result is not None:
                _write_handler(handler_p, core_id, now)
            return result
        # Handler fresh but dead → fall through to race-claim path

    # Race-claim path: any core may attempt, kernel decides. Winner refreshes
    # the handler file with their core_id + now.
    result = claim_plain(task_id, core_id, workspace=ws)
    if result is not None:
        _write_handler(handler_p, core_id, now)
    return result


def _main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "usage: claim_task.py <task-id> <core-id> [<channel-id>]",
            file=sys.stderr,
        )
        return 2
    task_id, core_id = argv[1], argv[2]
    channel_id = argv[3] if len(argv) == 4 else None
    try:
        if channel_id:
            result = claim_with_affinity(task_id, core_id, channel_id)
        else:
            result = claim_plain(task_id, core_id)
    except ValueError as e:
        print(f"claim_task: {e}", file=sys.stderr)
        return 2
    if result is None:
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
