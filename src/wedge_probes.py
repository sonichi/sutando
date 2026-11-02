#!/usr/bin/env python3
"""Watcher and standby wedge signatures for the shared wedge detector: pure probes that publish nothing.

Each probe reads a signal other code already keeps fresh and returns a `Signature` or None:

    watcher-not-ready     the core is alive but no READY session watcher serves its inbox, and no
                          standby covers it (watcher_identity.role_present / standby_present)
    core-status-stale     core-status.json says "running" but has not advanced for the threshold
    pane-prompt-blocked   the core supervisor's fresh verdict is a prompt or a sign-in only a person
                          can answer (state/core-supervisor.json, written by core-input-watch.py)

None means "no wedge seen", including "could not look": a probe never claims a wedge it did not
observe. `since` is set only when a signal dates the wedge; the detector owns first-seen tracking,
debouncing, the typed status and the one owner notice. `report()` is the single hand-off to it.

    wedge_probes.py --workspace WS [--json]     print the signatures now (exit 1 when any)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher_identity  # noqa: E402

WATCHER_NOT_READY = "watcher-not-ready"
CORE_STATUS_STALE = "core-status-stale"
PANE_PROMPT_BLOCKED = "pane-prompt-blocked"

# core-status.json "running" whose ts has not advanced this long: a pass crashed and never re-armed.
CORE_STATUS_STALE_S = 600
# Supervisor states only a person can clear; health-check's core-supervisor row reads the same set.
NEEDS_USER = frozenset({"blocked-human", "logged-out"})
# The supervisor rewrites its verdict every tick; an older one says nothing about the pane now.
SUPERVISOR_FRESH_S = 60
# A verdict dated ahead of now (a clock step, a bad write) is not fresh: past this it is refused.
FUTURE_TOLERANCE_S = 5
ROOM, OWNER_DM = "room", "owner-dm"


@dataclass(frozen=True)
class Signature:
    """`reason`: one line the owner can read; `unblock`: what clears it; `subject`: the inbox, session or
    file it is about; `since`: the epoch the wedge began, when a signal dates it; `visibility`: where the
    detector may show it (OWNER_DM when the reason quotes the pane, which can carry paths)."""
    kind: str
    reason: str
    unblock: str
    subject: str
    since: Optional[float] = None
    task_id: Optional[str] = None
    visibility: str = ROOM

    def as_dict(self) -> dict:
        return asdict(self)


def _ps() -> Optional[str]:
    """One process snapshot, shared by every watcher question so they cannot disagree."""
    try:
        r = subprocess.run(["ps", "-Ao", "pid,ppid,args"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def watcher_not_ready(inbox: str, state_dir: str, core_alive: bool, ps_output: Optional[str] = None,
                      argv_vector: Optional[Callable] = None) -> Optional[Signature]:
    """A live core whose inbox has no ready session watcher and no standby covering it."""
    if not core_alive:
        return None
    ps_output = _ps() if ps_output is None else ps_output
    if ps_output is None:
        return None
    if watcher_identity.role_present("session", inbox, ps_output, argv_vector=argv_vector,
                                     ready=True, state_dir=state_dir) is not False:
        return None
    if watcher_identity.standby_present(inbox, ps_output, argv_vector=argv_vector) is not False:
        return None  # a standby announces the inbox's tasks meanwhile (or the table is undecidable)
    running = watcher_identity.role_present("session", inbox, ps_output, argv_vector=argv_vector)
    if running:
        reason = ("the core's task watcher is running but not ready (no watcher sentinel names it), "
                  "so new tasks are not reaching the core")
    else:
        reason = "the core is up but no task watcher is running, so new tasks are not reaching the core"
    return Signature(WATCHER_NOT_READY, reason, "re-arm the core's streaming task watcher", inbox)


def core_status_verdict(data, now: float, threshold_s: float = CORE_STATUS_STALE_S) -> Optional[dict]:
    """The one rule for a stuck proactive pass: "running" with a `ts` older than the threshold.
    Returns {age, step, ts} when stale, else None; health-check's core-proactive-loop row uses it."""
    if not isinstance(data, dict) or data.get("status") != "running":
        return None
    ts = data.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    age = int(now - ts)
    if age <= threshold_s:
        return None
    return {"age": age, "step": data.get("step", "?"), "ts": ts}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def core_status_stale(workspace: Path, now: Optional[float] = None,
                      threshold_s: float = CORE_STATUS_STALE_S) -> Optional[Signature]:
    path = workspace / "state" / "core-status.json"
    stale = core_status_verdict(_read_json(path), time.time() if now is None else now, threshold_s)
    if stale is None:
        return None
    return Signature(CORE_STATUS_STALE,
                     f"the core has been on '{stale['step']}' for {stale['age'] // 60} min without moving on",
                     "check the core's pane; restart the core if the step is not progressing",
                     str(path), since=float(stale["ts"]))


def pane_prompt_blocked(workspace: Path, now: Optional[float] = None,
                        fresh_s: float = SUPERVISOR_FRESH_S) -> Optional[Signature]:
    """The core supervisor's current verdict, when it says a person must act on the core's pane."""
    path = workspace / "state" / "core-supervisor.json"
    now = time.time() if now is None else now
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return None
    if not -FUTURE_TOLERANCE_S <= age <= fresh_s:
        return None
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("state") not in NEEDS_USER:
        return None
    prompt = str(data.get("prompt") or "").strip().splitlines()
    excerpt = prompt[0][:120] if prompt else str(data.get("detail") or "")[:120]
    session = str(data.get("session") or "the core")
    if data["state"] == "logged-out":
        return Signature(PANE_PROMPT_BLOCKED, f"{session} is signed out and cannot run tasks",
                         "sign the core in again (/login in its pane)", session, visibility=OWNER_DM)
    return Signature(PANE_PROMPT_BLOCKED, f"{session} is waiting on a prompt: {excerpt}",
                     "answer the prompt in the core's pane", session, visibility=OWNER_DM)


def _core_alive_default(workspace: Path) -> bool:
    """This host's fresh core heartbeat, by health-check's own rule (loaded, not copied)."""
    spec = importlib.util.spec_from_file_location("_wedge_health_check", Path(__file__).with_name("health-check.py"))
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc._fresh_local_core_record(workspace) is not None


def probe_all(workspace: Path, core_alive: Optional[bool] = None, now: Optional[float] = None,
              ps_output: Optional[str] = None) -> List[Signature]:
    """Every watcher and standby signature for this host's core, from one process snapshot."""
    if core_alive is None:
        core_alive = _core_alive_default(workspace)
    found = [watcher_not_ready(str(workspace / "tasks"), str(workspace / "state"), core_alive, ps_output),
             core_status_stale(workspace, now) if core_alive else None,
             pane_prompt_blocked(workspace, now)]
    return [s for s in found if s is not None]


def _detector_publisher() -> Optional[Callable]:
    """The shared wedge detector's entry point, once its module is installed beside this one."""
    try:
        import wedge_detector  # noqa: PLC0415 -- optional until the detector lands
    except ImportError:
        return None
    return getattr(wedge_detector, "publish_signature", None)


def report(signatures: List[Signature], publisher: Optional[Callable] = None) -> int:
    """The single hand-off to the detector, which alone publishes and notifies. Returns how many it took."""
    publish = publisher if publisher is not None else _detector_publisher()
    if publish is None:
        return 0
    for s in signatures:
        publish(s.as_dict())
    return len(signatures)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    found = probe_all(Path(a.workspace))
    for s in found:
        print(json.dumps(s.as_dict()) if a.json else f"{s.kind}: {s.reason} -- {s.unblock}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
