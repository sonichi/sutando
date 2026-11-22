#!/usr/bin/env python3
"""core_crash_loop.py — a crash loop reaches the owner as one card per incident.

Two sources, both read by the core's monitor (core-input-watch.py) each tick:

- the backend supervisor's own verdict: a service in `state/supervisor-status.json` whose
  recovery budget is spent carries `terminal: {state: failed, reason, appSessionId}`, cleared
  only by a fresh app session. The card says which service, its last error, and that it stays
  stopped until the s.space app restarts; no button, since nothing on the card can clear it.
- the core itself, whatever its runtime: CRASH_LIMIT entries into `crashed` within
  CRASH_WINDOW_S. The card carries Restart core (hitl.recovery).

The card is the push: its own Matrix notification reaches the owner's devices. The macOS
notification, where on, stays an extra (core-supervisor-relay.py).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

SOURCE = "crash-loop"
STATUS_FILE = "supervisor-status.json"
#: The supervisor rewrites its heartbeat every <=5 s; an older one is no supervisor (its schema).
STATUS_FRESH_S = 60.0
CRASH_WINDOW_S = 600.0
CRASH_LIMIT = 4
_ERROR_MAX = 160


def terminal_services(workspace: Path, now: float) -> List[Dict]:
    """[{service, reason, error, session}] for each service the supervisor gave up on in its
    current app session, from a fresh status file; [] when the file is stale or unreadable."""
    try:
        doc = json.loads((Path(workspace) / "state" / STATUS_FILE).read_text())
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
        error = str(life.get("reason") or term.get("reason") or "")
        out.append({"service": str(name), "reason": str(term.get("reason") or "failed"),
                    "error": " ".join(error.split())[:_ERROR_MAX], "session": str(session)})
    return out


class CrashCounter:
    """Entries into `crashed` within the window: one entry per crash, however long it lasts.
    `incident_at` is when the count first reached CRASH_LIMIT; it holds while the loop does."""

    def __init__(self):
        self.entries: List[float] = []
        self.last = None
        self.incident_at: Optional[float] = None

    def observe(self, state: str, now: float) -> int:
        if state == "crashed" and self.last != "crashed":
            self.entries.append(now)
        self.last = state
        self.entries = [t for t in self.entries if now - t <= CRASH_WINDOW_S]
        if len(self.entries) >= CRASH_LIMIT:
            self.incident_at = self.incident_at or now
        else:
            self.incident_at = None
        return len(self.entries)


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


def step(manager, workspace: Path, session: str, runtime: Optional[str], counter: CrashCounter,
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
        lines = [f"{svc['service']} keeps crashing, so the app stopped restarting it ({svc['reason']}).",
                 "", f"Last error: {svc['error'] or 'not recorded'}", "",
                 "It stays stopped until the s.space app restarts on the Mac it runs on."]
        want[guard] = HumanRequirement(
            kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
            title=f"Crashing: {svc['service']}", guard=guard, device={"id": session, "name": session},
            subject={"session": session, "source": SOURCE, "crash_loop": svc["service"], "remind": False,
                     "incident": f"inc_{_digest(guard)}"})
    crashes = len(counter.entries)
    if counter.incident_at is not None:
        guard = f"{SOURCE}:core:{_digest(str(int(counter.incident_at)))}"
        error = _abnormal(last_pane)
        lines = [f"My core has crashed {crashes} times in the last {int(CRASH_WINDOW_S // 60)} minutes.",
                 "", f"Last error: {error or 'not recorded'}", "", "Tap Restart core to start it fresh."]
        want[guard] = HumanRequirement(
            kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
            title="Crashing: core", guard=guard, device={"id": session, "name": session},
            actions=[recovery_action(recovery.RESTART_CORE)],
            subject={"session": session, "source": SOURCE, "crash_loop": "core", "recovery": True,
                     "remind": False, "incident": f"inc_{_digest(guard)}"})
    import recovery_actions
    for r in manager.active():
        subject = r.subject or {}
        if subject.get("source") == SOURCE and subject.get("session") == session and r.guard not in want \
                and not recovery_actions.awaiting_executor(workspace, r):
            manager.resolve(r.id)
    return [manager.create(req) for req in want.values()]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]
