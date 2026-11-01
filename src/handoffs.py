#!/usr/bin/env python3
"""Delegated work: which parts of a task are with someone else, and the parent's status while they are.

A handoff is recorded where the work is given away, as a `handoff` record in the correlation log
(src/correlation_log.py), and closed by what shows it came back:

    path     opened by                                   closed when
    pool     pool_ask --parent-task                       the child task's result exists
    agent    the activity hook, on an Agent/Workflow call  its PostToolUse (foreground only)
    message  the activity hook, on a SendMessage ask        `handoffs.py close` (the reply's turn)
             carrying an Interaction-Id

Any handoff older than MAX_OPEN_S closes as a timeout, so a dead worker never pins a task open.
Only `holds: yes` handoffs keep the parent open (pool, foreground agent); the rest are shown as
delegated but never hold it, because nothing deterministic says when they end.

The parent's own seat is the only writer of the parent's status: `delegated_status()` reads the
delegate's latest line locally and returns the parent's phase and line; nothing writes to the
delegate's task or the delegate to the parent's.

    python3 src/handoffs.py open-list --task <task id> [--workspace W]
    python3 src/handoffs.py close --handoff <ho_id> [--outcome done|failed] [--workspace W]
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import correlation_log as cl  # noqa: E402

MAX_OPEN_S = 24 * 3600
LINE_MAX = 60
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9 ._-]")


def safe_label(name: "str | None", default: str) -> str:
    """A delegate label the log accepts: the pool or tool name trimmed to its allowed characters."""
    label = _SAFE_LABEL.sub("", str(name or "")).strip()[:40]
    return label if cl.PATTERNS["delegate_label"].match(label or "") else default


def open_handoff(workspace, parent_task_id: str, path: str, child_ref: str, delegate_label: str, *,
                 holds: bool, parent_generation: "int | None" = None) -> "str | None":
    """Record a handoff; its id, or None if it could not be recorded (correlation never raises)."""
    hid = cl.mint_handoff_id()
    rec = cl.record("handoff", {"handoff_id": hid, "parent_task_id": parent_task_id, "path": path,
                                "child_ref": child_ref, "delegate_label": delegate_label,
                                "holds": "yes" if holds else "no",
                                "parent_generation": str(parent_generation) if parent_generation is not None else None},
                    workspace=workspace)
    return hid if rec else None


def close_handoff(workspace, handoff_id: str, outcome: str = "done") -> bool:
    return cl.record("handoff_closed", {"handoff_id": handoff_id, "outcome": outcome}, workspace=workspace) is not None


def close_by_child(workspace, path: str, child_ref: str, outcome: str = "done") -> int:
    """Close every open handoff for one child (e.g. a subagent's tool_use_id); the count closed."""
    n = 0
    for h in _all(workspace):
        if h["path"] == path and h["child_ref"] == child_ref and not h.get("closed"):
            n += close_handoff(workspace, h["handoff_id"], outcome)
    return n


def _epoch(at: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def _all(workspace) -> list:
    """Every handoff with its closing outcome, if any, oldest first."""
    recs = cl.read(workspace=workspace)
    closed = {r.get("handoff_id"): r.get("outcome") for r in recs if r.get("kind") == "handoff_closed"}
    out = []
    for r in recs:
        if r.get("kind") == "handoff":
            out.append(dict(r, closed=closed.get(r.get("handoff_id"))))
    return out


def _child_done(workspace, h: dict) -> bool:
    if h["path"] != "pool":
        return False
    try:
        from local_task_protocol import find_result
        return find_result(Path(workspace) / "results", h["child_ref"]) is not None
    except Exception:  # noqa: BLE001 - unknown is open
        return False


def open_handoffs(workspace, task_id: str, *, holding_only: bool = False, now: "float | None" = None) -> list:
    """The task's handoffs still out, closing (and recording) those whose work came back or timed out."""
    now = time.time() if now is None else now
    out = []
    for h in _all(workspace):
        if h.get("parent_task_id") != task_id or h.get("closed"):
            continue
        if _child_done(workspace, h):
            close_handoff(workspace, h["handoff_id"], "done")
            continue
        if now - _epoch(h.get("at")) > MAX_OPEN_S:
            close_handoff(workspace, h["handoff_id"], "timeout")
            continue
        if holding_only and h.get("holds") != "yes":
            continue
        out.append(h)
    return out


def _delegate_line(workspace, h: dict) -> "str | None":
    """The delegate's own latest status line, for a pool child that has one."""
    if h["path"] != "pool":
        return None
    try:
        import phase_line
        st = phase_line.latest(workspace, h["child_ref"])
    except Exception:  # noqa: BLE001 - the phase sidecar is optional
        st = None
    return (st or {}).get("line") or (st or {}).get("step")


def delegated_status(workspace, task_id: str, *, now: "float | None" = None) -> "dict | None":
    """{phase: "Delegated", line, handoffs} while any handoff is out, else None."""
    out = open_handoffs(workspace, task_id, now=now)
    if not out:
        return None
    newest = out[-1]
    if len(out) == 1:
        head = f"Delegated: {newest['delegate_label']}"
    else:
        head = f"Delegated: {len(out)} helpers"
    tail = _delegate_line(workspace, newest)
    line = f"{head}, {tail[0].lower() + tail[1:]}" if tail else head
    if len(line) > LINE_MAX:
        line = line[: LINE_MAX - 1].rstrip(" ,") + "…"
    return {"phase": "Delegated", "line": line, "handoffs": len(out),
            "holding": sum(1 for h in out if h.get("holds") == "yes")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Delegated work for a task.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open-list")
    o.add_argument("--task", required=True)
    o.add_argument("--workspace")
    c = sub.add_parser("close")
    c.add_argument("--handoff", required=True)
    c.add_argument("--outcome", choices=("done", "failed"), default="done")
    c.add_argument("--workspace")
    a = ap.parse_args(argv)
    ws = a.workspace
    if ws is None:
        from workspace_default import resolve_workspace
        ws = resolve_workspace(migrate=False)
    if a.cmd == "close":
        return 0 if close_handoff(ws, a.handoff, a.outcome) else 2
    print(json.dumps({"open": open_handoffs(ws, a.task), "status": delegated_status(ws, a.task)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
