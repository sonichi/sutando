#!/usr/bin/env python3
"""core_restart_intent.py — the owner's easy-restart intent file (sonichi#2401).

The single hand-off between "the owner asked for a core restart/stop from
chat" and "the GUI-session executor performs it". A bridge (which survives
core death) writes the intent; Sutando.app's poller consumes it and runs the
action in the GUI login session — so the relaunch comes up authenticated with
no SSH, no Terminal, no keychain wall (the 2026-07-29 outage class).

Human-triggered ONLY, by owner decision on #2401: nothing in this module (or
anywhere else) writes an intent on its own. Stop means stop — a consumed
"stop" has no corresponding auto-restart anywhere.

File: <workspace>/state/core-restart-requested.json
Schema: {"action": "restart"|"stop", "requested_at": <epoch>, "source": "..."}

Consume semantics: read-and-delete BEFORE acting (a crash mid-action must not
replay the intent on the next poll), and the delete must SUCCEED before any
action — an undeletable file means the next poll would replay it, so the
consumer fails closed and does nothing. Intents older than STALE_SEC are
consumed and dropped — an ancient file left by a dead executor must not fire a surprise
restart when the app next boots.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time

# Canonical workspace resolution (state-paths adoption lint): callers may pass
# an explicit workspace (bridges already hold one; tests pass temp dirs), and
# omitting it falls back to the resolver — never a hand-rolled path.
from workspace_default import resolve_workspace

INTENT_BASENAME = "core-restart-requested.json"
STALE_SEC = 600
_ACTIONS = ("restart", "stop")

# Owner chat commands → action. Exact-match after lowercase/strip so ordinary
# prose mentioning a restart ("we should restart core tomorrow") never triggers.
_COMMANDS = {
    "restart core": "restart",
    "restart the core": "restart",
    "core restart": "restart",
    "stop core": "stop",
    "stop the core": "stop",
    "core stop": "stop",
}


def parse_restart_command(text) -> str | None:
    """Return "restart"/"stop" when ``text`` IS an owner restart command
    (exact match modulo case/whitespace/trailing punctuation), else None."""
    if not text:
        return None
    t = " ".join(text.lower().split()).rstrip(".!")
    return _COMMANDS.get(t)


def intent_path(workspace: str | None = None) -> str:
    ws = workspace if workspace is not None else str(resolve_workspace())
    return os.path.join(ws, "state", INTENT_BASENAME)


class IntentPending(Exception):
    """A different, still-unconsumed intent already occupies the file."""

    def __init__(self, action: str, requested_at: float):
        super().__init__(f"an unconsumed {action!r} request is already pending")
        self.action = action
        self.requested_at = requested_at


@contextlib.contextmanager
def _intent_lock(path: str):
    """Serialize every read-modify-delete of the intent, for writers AND
    consumers, across threads and processes. Any participant that deletes the
    pathname must hold this, or it can delete an intent it never read.
    Released by the kernel if the holder dies."""
    with open(path + ".lock", "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def peek_intent(workspace: str | None, now: float | None = None) -> dict | None:
    """Return the pending intent WITHOUT consuming it, or None if there is
    none / it is unreadable / it is stale. Read-only: never deletes."""
    try:
        with open(intent_path(workspace)) as f:
            d = json.loads(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("action") not in _ACTIONS:
        return None
    age = (now if now is not None else time.time()) - float(d.get("requested_at") or 0)
    return None if age > STALE_SEC else d


def write_intent(workspace: str | None, action: str, source: str) -> str:
    """Atomically write the intent file; returns its path. Raises ValueError
    on an unknown action, and IntentPending if a live intent already exists.

    Exclusive by construction (os.link fails when the target exists), because
    a waiter can only correlate consumption by the pathname disappearing: with
    two intents in flight, one supersedes the other on disk and BOTH waiters
    read the single deletion as their own (qingyun review, #3191). One pending
    intent at a time is what makes that inference sound.
    """
    if action not in _ACTIONS:
        raise ValueError(f"unknown intent action: {action!r}")
    path = intent_path(workspace)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # mkstemp, not pid: two threads of one bridge would share a pid-named temp,
    # so one could link the other's bytes or unlink the temp it is about to link.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=INTENT_BASENAME + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"action": action, "requested_at": time.time(), "source": source}, f)
        with _intent_lock(path):
            live = peek_intent(workspace)
            if live is not None:
                raise IntentPending(live["action"], float(live["requested_at"]))
            # Absent or stale. Both the remove and the claim happen under the
            # lock, so no writer can delete another's freshly-installed intent.
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                raise IntentPending(action, time.time())
            try:
                os.link(tmp, path)
            except FileExistsError:  # refilled outside the lock — theirs stands
                raise IntentPending(action, time.time())
            return path
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def await_consumption(
    workspace: str | None,
    timeout_sec: float = 12.0,
    poll_sec: float = 0.5,
    sleep=time.sleep,
    now=time.monotonic,
) -> bool:
    """Block until some executor consumes the intent; True if it did.

    Consumption is defined by the file being GONE, which is the only
    implementation-agnostic evidence available: `consume_intent` deletes
    before acting, so disappearance means an executor claimed it. That
    inference is only sound because `write_intent` is exclusive — otherwise a
    superseding write would let one deletion satisfy two different waiters.
    Probing for a specific consumer (a running Sutando.app, a named launchd
    label) answers "is THAT consumer here", not "will anything act" — and
    returns the same False for a host whose executor is simply a different one.

    Default timeout covers the documented 5s poll interval twice over.
    """
    path = intent_path(workspace)
    deadline = now() + timeout_sec
    while True:
        if not os.path.exists(path):
            return True
        if now() >= deadline:
            return False
        sleep(poll_sec)


def consume_intent(workspace: str | None, now: float | None = None) -> str | None:
    """Read-and-DELETE the intent; return its action, or None.

    None when: no file, malformed JSON, unknown action, or stale
    (requested_at older than STALE_SEC). All of those consume (delete) the
    file too — a bad or ancient intent must never linger and re-fire.
    """
    path = intent_path(workspace)
    # Nothing to delete means nothing to serialize, and the lock's own
    # directory may not exist yet on a workspace that has never been written.
    if not os.path.exists(path):
        return None
    with _intent_lock(path):
        try:
            with open(path) as f:
                raw = f.read()
        except OSError:
            return None
        try:
            os.unlink(path)  # consume FIRST — crash mid-action must not replay
        except OSError:
            # FAIL CLOSED (qingyun review, #2408): if the file can't be
            # removed, the next poll would see it again and replay the action.
            return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("action") not in _ACTIONS:
        return None
    age = (now if now is not None else time.time()) - float(d.get("requested_at") or 0)
    if age > STALE_SEC:
        return None
    return d["action"]
