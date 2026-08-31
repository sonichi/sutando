"""Signal Room → Sutando task submission. Sutando owns everything after this.

A Signal Room is a voice room the owner hosts and invites people into. When someone
in the room asks the host for research, the request arrives here — and this module's
entire job is to hand Sutando a task at the right tier. It does NOT choose an engine,
compose a command line, supply credentials, or provision a profile.

That boundary is the point, and it was learned the hard way: an earlier version let
the Signal Room lane pick the runtime (`claude -p …`) and pin it to an isolated
config home. Because the caller chose the engine, it also had to own the engine's
login — so a *Signal Room* feature failed with a *Claude CLI* "Not logged in", and a
whole credential-copying/profile-provisioning subsystem existed only to prop that up.
Sutando already runs an authenticated engine; a task submitted here rides it.

TIER: `team` (owner decision, 2026-08-31). The owner invites people into the room, so
room requests are treated as collaborator-level work — the same tier the chat bridges
use for non-owner senders, which Sutando already executes through its sandboxed
delegation path. A dedicated read-only `guest` execution tier is a named future task:
`ACCESS_TIERS` declares one, but nothing enforces it yet, so claiming it here would be
a label without a boundary.

WHAT SUTANDO ENFORCES (not this module):
  * the tier's execution restrictions — non-owner tasks are delegated to a sandboxed
    agent by the core, which is where engine choice and isolation belong;
  * `guard_result_for_tier` on the way out — every non-owner result is secret-scanned
    before it reaches the room;
  * `confine_user_content` on the way in — untrusted text cannot forge task headers.
"""
from __future__ import annotations

import os
import secrets
import threading
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Signal Room work runs at collaborator level. Sutando maps this to its own execution
# path for the tier; nothing about HOW it runs is decided here.
SIGNAL_ROOM_TIER = "team"
SIGNAL_TASK_PREFIX = "task-signal-"

# The room's request text is untrusted (participant speech plus quoted article text),
# so cap it before it ever reaches a task file.
MAX_TASK_CHARS = 8000

# Admission bound: an unbounded queue of untrusted room work would starve the
# owner. Mirrors the two concurrent workers the previous lane allowed.
MAX_OUTSTANDING = 2

# Slot reclaim window. Clears a real sandboxed deep dive (minutes) by a wide
# margin, while stopping orphaned task files from wedging the lane shut forever.
SLOT_TTL_SEC = 1800


# A core writes state/cores/<host>.alive every 30s (src/core_heartbeat.py).
# Four missed beats is comfortably past jitter without masking a real outage.
CORE_STALE_SEC = 120


# agent-api serves on a ThreadingHTTPServer, so counting and publishing must be
# one critical section or concurrent posts all read capacity and all admit.
_ADMISSION = threading.Lock()


class SignalRoomBusy(RuntimeError):
    """Raised when the Signal Room already has MAX_OUTSTANDING work in flight."""


def submit_signal_room_task(task_text: str, task_dir, confine, *, room_id: str = "",
                            requested_by: str = "") -> str:
    """Write one Signal Room request as a normal Sutando task. Returns its id.

    The id is in the canonical ``task-*`` namespace because that is what the core
    picks up, and because the owner SHOULD see room-originated work in their task
    list — that visibility is the feature, not a leak.
    """
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"task-signal-{int(datetime.now().timestamp() * 1000)}-{secrets.token_hex(4)}"

    headers = [
        ("id", task_id),
        ("timestamp", datetime.now().isoformat()),
        ("source", "signal-room"),
        ("interaction_type", "tool_initiated"),
        ("access_tier", SIGNAL_ROOM_TIER),
        ("from", _one_line(requested_by, confine) if requested_by else "signal-room"),
    ]
    if room_id:
        headers.append(("source_room_id", _one_line(room_id, confine)))

    # The shared serializer owns header validation and puts `task:` last, so newlines
    # in untrusted room speech extend the body instead of forging fields.
    from local_task_protocol import serialize_task_last
    content = serialize_task_last(headers, confine(task_text[:MAX_TASK_CHARS]))

    # One critical section, so concurrent posts cannot all observe capacity and
    # all admit. Here, not at the routes, so no new caller can bypass the bound.
    with _ADMISSION:
        if outstanding_count(task_dir) >= MAX_OUTSTANDING:
            raise SignalRoomBusy(
                f"{MAX_OUTSTANDING} Signal Room tasks already in flight"
            )
        # Atomic: the watcher must never observe a partially written task.
        fd, tmp = tempfile.mkstemp(dir=str(task_dir), prefix=f".{task_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            os.replace(tmp, str(task_dir / f"{task_id}.txt"))
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    return task_id


def _one_line(value: str, confine) -> str:
    """Flatten an untrusted header value to a single line, then defang it.

    `room_id` and `requested_by` are identifiers: a newline in one is always an
    attempt to open a forged header, never real data. Flattening first keeps
    `serialize_task_last`'s no-newline invariant from raising out of the route
    as a 500 — the serializer stays as the backstop, not the first line of defense.
    """
    return confine(" ".join(str(value).splitlines()).strip())


def core_is_alive(workspace=None, now: float | None = None) -> bool | None:
    """Is some sutando-core around to consume a Signal Room task?

    True/False when the heartbeat facility is in use, None when it is not
    installed on this machine (no state/cores at all) — the caller must not
    read None as "dead", or an install that simply never runs
    src/core_heartbeat.py would advertise the feature as permanently off.
    """
    from workspace_default import resolve_workspace
    root = Path(workspace) if workspace is not None else resolve_workspace()
    cores = root / "state" / "cores"
    if not cores.is_dir():
        return None
    try:
        beats = list(cores.glob("*.alive"))
    except OSError:
        return False
    # A graceful core shutdown unlinks its .alive file, so an empty facility
    # directory means offline — not "the facility was never installed".
    if not beats:
        return False
    stamp = now if now is not None else time.time()
    for beat in beats:
        try:
            if stamp - os.stat(beat).st_mtime <= CORE_STALE_SEC:
                return True
        except OSError:
            continue
    return False


def outstanding_count(task_dir, now: float | None = None) -> int:
    """Signal Room tasks still plausibly in flight.

    Counts only files younger than SLOT_TTL_SEC: an abandoned task file is never
    cleaned up by anyone (task-bridge archives on RESULT delivery, which by
    definition never happens for an orphan), so counting them forever would
    turn two dead tasks into a permanently closed lane.
    """
    stamp = now if now is not None else time.time()
    live = 0
    # Fail CLOSED: an unreadable task dir must read as full, not empty, or a
    # scan error would silently lift the bound.
    for task in Path(task_dir).glob(f"{SIGNAL_TASK_PREFIX}*.txt"):
        try:
            if stamp - task.stat().st_mtime <= SLOT_TTL_SEC:
                live += 1
        except OSError:
            live += 1
    return live


def submission_status(task_dir, workspace=None) -> tuple[bool, str | None]:
    """`(available, reason)` for the capability signal.

    Submission is available when Sutando can accept a task at all — the tier's
    execution is the core's business, so there is nothing else to probe here. This
    deliberately does NOT claim anything about a runtime it does not own; the earlier
    version's biggest bug was reporting "ready" for an engine it could not actually
    run.
    """
    try:
        path = Path(task_dir)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".signal-room-probe-{secrets.token_hex(4)}"
        probe.write_text("")
        probe.unlink()
    except Exception:
        return False, "task_dir_unwritable"
    if core_is_alive(workspace) is False:
        # Every heartbeat is stale: advertising deep_dive now would promise the
        # room an answer that nothing is running to produce.
        return False, "core_offline"
    if outstanding_count(path) >= MAX_OUTSTANDING:
        # Claiming "ready" while nothing can start is the lie this signal exists
        # to avoid, so report busy instead.
        return False, "busy"
    return True, None
