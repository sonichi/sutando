#!/usr/bin/env python3
"""core_crash_loop.py — a crash loop reaches the owner as one card per incident.

Two sources, both read by the core's monitor (core-input-watch.py) each tick:

- the backend supervisor's own verdict: a service in `state/supervisor-status.json` whose
  recovery budget is spent carries `terminal: {state: failed, reason, appSessionId}`, cleared
  by a fresh app session. The card says which service and its last error. When the supervisor
  marks the terminal `resettable`, the card carries Start <service> again: the tap drops one
  request in state/control/services/ that the supervisor reads on its watchdog tick, clears that
  service's terminal state and launches it once more (request_reset).
- the core itself, whatever its runtime: the recovery record (core_recovery_state, the one
  derivation of the core's state) says `crash_loop`. The card carries Restart core, and says so
  when the app's own revive loop has given up (`reason: revive_exhausted`).

The card is the push: its own Matrix notification reaches the owner's devices. The macOS
notification, where on, stays an extra (core-supervisor-relay.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from workspace_default import status_read_path

SOURCE = "crash-loop"
STATUS_FILE = "supervisor-status.json"
#: The supervisor rewrites its heartbeat every <=5 s; an older one is no supervisor (its schema).
STATUS_FRESH_S = 60.0
#: Where the supervisor picks up an owner's request to start a stopped service again.
RESET_DIR = ("state", "control", "services")
#: The kinds a service's last error is named by; its own text (paths, tokens, anything) never reaches a card.
_ERROR_KINDS = (
    ("out of memory", re.compile(r"out of memory|\bOOM\b|heap limit|ENOMEM|memory allocation", re.I)),
    ("port already in use", re.compile(r"EADDRINUSE|address already in use|port .{0,20}in use", re.I)),
    ("missing file or module", re.compile(r"ENOENT|no such file|cannot find module|module not found|"
                                          r"ModuleNotFoundError", re.I)),
    ("permission denied", re.compile(r"EACCES|EPERM|permission denied|operation not permitted", re.I)),
    ("timed out", re.compile(r"timed? ?out|ETIMEDOUT", re.I)),
    ("network error", re.compile(r"ECONNREFUSED|ECONNRESET|ENOTFOUND|network|socket hang up", re.I)),
    ("killed by a signal", re.compile(r"\bsignal\b|SIGKILL|SIGTERM|SIGSEGV|killed", re.I)),
    ("exited with an error", re.compile(r"\bexit(?:ed)?\b|code \d+|error", re.I)),
)


def terminal_services(workspace: Path, now: float) -> List[Dict]:
    """[{service, reason, error, session, resettable}] for each service the supervisor gave up on in its
    current app session, from a fresh status file; [] when the file is stale or unreadable."""
    try:
        doc = json.loads(status_read_path(STATUS_FILE, Path(workspace)).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    beat, session = doc.get("heartbeatAt"), doc.get("appSessionId")
    if not isinstance(beat, (int, float)) or now - beat / 1000.0 > STATUS_FRESH_S or not session:
        return []
    out = []
    for name, svc in sorted((doc.get("services") or {}).items()):
        term = svc.get("terminal") if isinstance(svc, dict) else None
        if not isinstance(term, dict) or term.get("state") != "failed" or term.get("appSessionId") != session:
            continue
        life = svc.get("lifecycle") if isinstance(svc.get("lifecycle"), dict) else {}
        out.append({"service": _name(name), "reason": _word(str(term.get("reason") or "")),
                    "error": error_kind(str(life.get("reason") or term.get("reason") or "")),
                    "session": str(session), "resettable": term.get("resettable") is True})
    return out


def request_reset(workspace: Path, service: str, now: float) -> bool:
    """Hand the supervisor one request to start `service` again; False, and nothing written, when
    its fresh status does not say that service is stopped and resettable in its current session."""
    svc = next((s for s in terminal_services(workspace, now) if s["service"] == service and s["resettable"]), None)
    if svc is None:
        return False
    rid = uuid.uuid4().hex
    body = {"schemaVersion": 1, "id": rid, "kind": "reset-service", "service": service,
            "appSessionId": svc["session"], "requestedAtMs": int(now * 1000)}
    folder = Path(workspace).joinpath(*RESET_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".req-", suffix=".tmp", dir=folder)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f)
    os.replace(tmp, folder / f"req-{rid}.json")
    return True


def error_kind(text: str) -> str:
    """The kind of a supervisor error, named from a fixed list, never quoted; "" when none fits."""
    return next((name for name, rx in _ERROR_KINDS if rx.search(text or "")), "")


def _word(reason: str) -> str:
    """The supervisor's verdict word ("crash-loop"), else its kind: a sentence never reaches a card."""
    if re.fullmatch(r"[a-z][a-z0-9-]{0,30}", reason):
        return reason
    return error_kind(reason) or "failed"


def _name(service) -> str:
    """A service's name as the card shows it: the supervisor's own short id, else "a service"."""
    text = str(service)
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", text) else "a service"


def _abnormal(pane: Optional[str]) -> str:
    """The kind of trouble the pane showed before the crash, named, never quoted: a pane can
    hold anything, a card must not."""
    if not pane:
        return ""
    import cli_wedge
    for name, rx in cli_wedge.ABNORMAL_PATTERNS:
        if rx.search(pane):
            return name.replace("-", " ")
    return ""


def step(manager, workspace: Path, session: str, runtime: Optional[str],
         last_pane: Optional[str], now: float) -> List:
    """Raise one card per crash-loop incident and close each once its cause is gone.
    Returns the open crash-loop requirements."""
    if manager is None:
        return []
    from hitl import recovery
    from hitl.schema import HumanRequirement
    from hitl.tui_gate import recovery_action
    want: Dict[str, HumanRequirement] = {}
    for svc in terminal_services(workspace, now):
        guard = f"{SOURCE}:{svc['service']}:{_digest(svc['session'])}"
        reset = recovery.parse(f"{recovery.RESET_SERVICE}:{svc['service']}") if svc["resettable"] else None
        lines = [f"{svc['service']} keeps crashing, so the app stopped restarting it ({svc['reason']}).",
                 "", f"Last error: {svc['error'] or 'not recorded'}", "",
                 f"Tap Start {svc['service']} again to have the app try it once more, or restart the s.space "
                 "app on the Mac it runs on." if reset else
                 "It stays stopped until the s.space app restarts on the Mac it runs on."]
        want[guard] = HumanRequirement(
            kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
            title=f"Crashing: {svc['service']}", guard=guard, device={"id": session, "name": session},
            actions=[recovery_action(recovery.action_id(*reset))] if reset else [],
            subject={"session": session, "source": SOURCE, "crash_loop": svc["service"], "remind": False,
                     "incident": f"inc_{_digest(guard)}", **({"recovery": True} if reset else {})})
    import core_recovery_state
    record = core_recovery_state.read(workspace, now) or {}
    if record.get("state") == "crash_loop":
        incident = str(record.get("incident") or "")
        gave_up = record.get("reason") == core_recovery_state.REVIVE_EXHAUSTED
        # Giving up is news: its own guard replaces the earlier card with one that notifies again.
        guard = f"{SOURCE}:core:{incident}" + (":gave-up" if gave_up else "")
        error = _abnormal(last_pane)
        window = int(core_recovery_state.RESTART_WINDOW_S // 60)
        count = record.get("restarts_10m")
        lines = [f"My core has started {count} times in the last {window} minutes and is still not up."
                 if isinstance(count, int) else "My core keeps crashing.",
                 "", f"Last error: {error or 'not recorded'}", ""]
        if gave_up:
            lines += ["The s.space app could not revive it after repeated tries and now retries only "
                      "every few minutes.", ""]
        lines.append("Tap Restart core to start it fresh.")
        want[guard] = HumanRequirement(
            kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
            title="Can't recover: core" if gave_up else "Crashing: core", guard=guard,
            device={"id": session, "name": session},
            actions=[recovery_action(recovery.RESTART_CORE)],
            subject={"session": session, "source": SOURCE, "crash_loop": "core", "recovery": True,
                     "remind": False, "incident": incident})
    import recovery_actions
    for r in manager.active():
        subject = r.subject or {}
        if subject.get("source") == SOURCE and subject.get("session") == session and r.guard not in want \
                and not recovery_actions.awaiting_executor(workspace, r):
            manager.resolve(r.id)
    return [manager.create(req) for req in want.values()]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]
