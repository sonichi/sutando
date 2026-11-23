#!/usr/bin/env python3
"""core_crash_loop.py — a crash loop reaches the owner as one card per incident.

Two sources, both read by the core's monitor (core-input-watch.py) each tick:

- the backend supervisor's own verdict: a service in `state/supervisor-status.json` whose
  recovery budget is spent carries `terminal: {state: failed, reason, appSessionId}`, cleared
  by a fresh app session. The card says which service and its last error. When the supervisor
  marks the terminal `resettable` and stamps it with an incident id, the card carries Start
  <service> again: the tap drops one request in state/control/services/, naming the card's own
  app session and incident (request_reset). The card closes on the supervisor's answer
  (reset_result: running, failed, or a timeout), never on the stop marker going away.
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
#: How long a reset request waits for the supervisor's own answer before it is a timeout.
RESET_TIMEOUT_S = 300.0
RUNNING, FAILED, TIMEOUT = "running", "failed", "timeout"
#: The supervisor's lifecycle words that contradict a stop marker on the same service.
_UP_VERDICTS = frozenset({"running", "adopted", "starting"})
_ID = re.compile(r"[A-Za-z0-9_-]{4,64}")
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
    """[{service, reason, error, session, incident, resettable}] for each service the supervisor
    gave up on in its current app session, from a fresh status file; [] when the file is stale or
    unreadable. A stop marker is used only when it is this session's, its trip time (when it has
    one) is a time the heartbeat carrying it could have seen, and the supervisor's own lifecycle
    word for the service does not say it is up. `resettable` also needs the marker's incident id."""
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
        at, incident = term.get("at"), term.get("incident")
        timed = isinstance(at, (int, float)) and not isinstance(at, bool)
        if life.get("verdict") in _UP_VERDICTS or (at is not None and not (timed and 0 < at <= beat)):
            continue
        marked = timed and isinstance(incident, str) and bool(_ID.fullmatch(incident))
        out.append({"service": _name(name), "reason": _word(str(term.get("reason") or "")),
                    "error": error_kind(str(life.get("reason") or term.get("reason") or "")),
                    "session": str(session), "incident": incident if marked else "",
                    "resettable": term.get("resettable") is True and marked})
    return out


def request_reset(workspace: Path, service: str, session: str, incident: str, now: float) -> Optional[str]:
    """Hand the supervisor one request to start `service` again, for the stop a card was raised on
    (`session`, `incident`); its id, or None, and nothing written, unless the fresh status still
    reports exactly that stop as resettable."""
    hit = any(s["resettable"] and (s["service"], s["session"], s["incident"]) == (service, session, incident)
              for s in terminal_services(workspace, now))
    if not hit or not incident:
        return None
    rid = uuid.uuid4().hex
    body = {"schemaVersion": 1, "id": rid, "kind": "reset-service", "service": service,
            "appSessionId": session, "incident": incident, "requestedAtMs": int(now * 1000)}
    folder = Path(workspace).joinpath(*RESET_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".req-", suffix=".tmp", dir=folder)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f)
    os.replace(tmp, folder / f"req-{rid}.json")
    return rid


def reset_result(workspace: Path, rid: str, service: str, session: str, incident: str,
                 requested_at: float, now: float) -> Optional[str]:
    """The supervisor's own answer to request `rid`: RUNNING, FAILED, TIMEOUT once RESET_TIMEOUT_S
    passed with none, else None (still waiting). Only an answer naming this request, service,
    session and incident, stamped after the request, counts."""
    try:
        doc = json.loads((Path(workspace).joinpath(*RESET_DIR) / f"res-{rid}.json").read_text()) \
            if _ID.fullmatch(rid) else None
    except (OSError, ValueError):
        doc = None
    at = doc.get("atMs") if isinstance(doc, dict) else None
    if (isinstance(at, (int, float)) and not isinstance(at, bool) and at >= int(requested_at * 1000)
            and doc.get("schemaVersion") == 1 and isinstance(doc.get("outcome"), str)
            and (doc.get("id"), doc.get("service"), doc.get("appSessionId"), doc.get("incident"))
            == (rid, service, session, incident)):
        return RUNNING if doc["outcome"] == RUNNING else FAILED
    return TIMEOUT if now - requested_at > RESET_TIMEOUT_S else None


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
        guard = f"{SOURCE}:{svc['service']}:{_digest(svc['session'])}" + (f":{svc['incident']}" if svc["incident"] else "")
        reset = recovery.parse(f"{recovery.RESET_SERVICE}:{svc['service']}") if svc["resettable"] else None
        lines = [f"{svc['service']} keeps crashing, so the app stopped restarting it ({svc['reason']}).",
                 "", f"Last error: {svc['error'] or 'not recorded'}", "",
                 f"Tap Start {svc['service']} again to have the app try it once more, or restart the app "
                 "on the Mac it runs on." if reset else
                 "It stays stopped until the s.space app restarts on the Mac it runs on."]
        want[guard] = HumanRequirement(
            kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
            title=f"Crashing: {svc['service']}", guard=guard, device={"id": session, "name": session},
            actions=[recovery_action(recovery.action_id(*reset))] if reset else [],
            subject={"session": session, "source": SOURCE, "crash_loop": svc["service"], "remind": False,
                     "incident": f"inc_{_digest(guard)}",
                     **({"recovery": True, "service_session": svc["session"],
                         "service_incident": svc["incident"]} if reset else {})})
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
            lines += ["The desktop app could not revive it after repeated tries and now retries only "
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
    recovery_actions.settle_service_resets(manager, workspace, session, now)
    for r in manager.active():
        subject = r.subject or {}
        if subject.get("source") == SOURCE and subject.get("session") == session and r.guard not in want \
                and not recovery_actions.awaiting_executor(workspace, r):
            manager.resolve(r.id)
    return [manager.create(req) for req in want.values()]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]
