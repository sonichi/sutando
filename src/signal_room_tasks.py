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

import secrets
from datetime import datetime
from pathlib import Path

# Signal Room work runs at collaborator level. Sutando maps this to its own execution
# path for the tier; nothing about HOW it runs is decided here.
SIGNAL_ROOM_TIER = "team"

# The room's request text is untrusted (participant speech plus quoted article text),
# so cap it before it ever reaches a task file.
MAX_TASK_CHARS = 8000


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

    # Field order mirrors the owner writer: `task:` is LAST, so newlines in the
    # untrusted body extend the body rather than forging fields below it.
    lines = [
        f"id: {task_id}",
        f"timestamp: {datetime.now().isoformat()}",
        "source: signal-room",
        "interaction_type: tool_initiated",
        f"access_tier: {SIGNAL_ROOM_TIER}",
    ]
    if room_id:
        lines.append(f"source_room_id: {confine(room_id)}")
    if requested_by:
        lines.append(f"from: {confine(requested_by)}")
    else:
        lines.append("from: signal-room")
    lines.append(f"task: {confine(task_text[:MAX_TASK_CHARS])}")

    (task_dir / f"{task_id}.txt").write_text("\n".join(lines) + "\n")
    return task_id


def submission_status(task_dir) -> tuple[bool, str | None]:
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
        return True, None
    except Exception:
        return False, "task_dir_unwritable"
