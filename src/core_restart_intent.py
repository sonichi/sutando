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
replay the intent on the next poll). Intents older than STALE_SEC are consumed
and dropped — an ancient file left by a dead executor must not fire a surprise
restart when the app next boots.
"""
from __future__ import annotations

import json
import os
import time

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


def intent_path(workspace: str) -> str:
    return os.path.join(workspace, "state", INTENT_BASENAME)


def write_intent(workspace: str, action: str, source: str) -> str:
    """Atomically write the intent file; returns its path. Raises ValueError
    on an unknown action — callers never write arbitrary strings."""
    if action not in _ACTIONS:
        raise ValueError(f"unknown intent action: {action!r}")
    path = intent_path(workspace)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"action": action, "requested_at": time.time(), "source": source}, f)
    os.replace(tmp, path)
    return path


def consume_intent(workspace: str, now: float | None = None) -> str | None:
    """Read-and-DELETE the intent; return its action, or None.

    None when: no file, malformed JSON, unknown action, or stale
    (requested_at older than STALE_SEC). All of those consume (delete) the
    file too — a bad or ancient intent must never linger and re-fire.
    """
    path = intent_path(workspace)
    try:
        with open(path) as f:
            raw = f.read()
    except OSError:
        return None
    try:
        os.unlink(path)  # consume FIRST — crash mid-action must not replay
    except OSError:
        pass
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
