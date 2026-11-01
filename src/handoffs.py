#!/usr/bin/env python3
"""Delegated work: which parts of a task are with someone else, and the parent's status while they are.

A handoff is recorded where the work is given away, as a `handoff` record in the correlation log
(src/correlation_log.py, the audit trail), with an index file per open handoff under
<workspace>/state/handoffs/ (what readers and closers touch, so no one parses the log on a hot path):

    path     opened by                                    closed when
    pool     pool_ask --parent-task                        the child task's result is ready (done)
    agent    the activity hook, on an Agent/Workflow call   its PostToolUse (done) or PostToolUseFailure
                                                           (failed); foreground only holds
    message  the activity hook, on a SendMessage ask         `handoffs.py close` (the reply's turn)
             carrying an Interaction-Id

A close is claimed by unlinking the index file: only the process whose unlink succeeds writes the
`handoff_closed` record, so concurrent readers never close twice and nobody waits on a lock. Any
handoff older than MAX_OPEN_S closes as a timeout, and `delegated_status` returns the deadline so a
parked parent's blocker can carry the same expiry. Only `holds: yes` handoffs keep the parent open.

Labels are never free text: a pool worker's own roster label, a known subagent type, or a generic
role; anything shaped like an address or account is refused before it reaches a record.

    python3 src/handoffs.py open-list --task <task id> [--workspace W]
    python3 src/handoffs.py close --handoff <ho_id> [--outcome done|failed] [--workspace W]
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import correlation_log as cl  # noqa: E402

MAX_OPEN_S = 24 * 3600
LINE_MAX = 60
INDEX = Path("state") / "handoffs"
# Subagent types a call may name; any other value is shown as the generic role.
SUBAGENT_TYPES = frozenset({"general-purpose", "Explore", "Plan", "claude", "web-fetch", "statusline-setup",
                            "claude-code-guide"})
_ADDRESSY = re.compile(r"[@!:/\\<>]|\.[A-Za-z]{2,}\b|\b\d{4,}\b")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._~-]", "_", value)[:120]


def label_for(path: str, given: "str | None", verified: "str | None" = None) -> str:
    """The delegate label a record may hold: a verified pool label, a known subagent type, or a role.
    Address- or account-shaped input is never normalised into a label: it becomes the role."""
    role = {"pool": "a worker", "agent": "a subagent", "message": "an agent"}[path]
    if path == "pool" and verified and not _ADDRESSY.search(verified) and cl.PATTERNS["delegate_label"].match(verified):
        return verified
    if path == "agent" and given in SUBAGENT_TYPES:
        return given
    return role


def _now() -> float:
    return time.time()


def _epoch(at: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def _index_dir(workspace, parent_task_id: str) -> Path:
    return Path(workspace) / INDEX / _safe_name(parent_task_id)


def _child_key(workspace, path: str, child_ref: str) -> Path:
    return Path(workspace) / INDEX / "by-child" / f"{path}-{_safe_name(child_ref)}"


def open_handoff(workspace, parent_task_id: str, path: str, child_ref: str, delegate_label: str, *,
                 holds: bool, parent_generation: "int | None" = None) -> "str | None":
    """Record a handoff and index it; its id, or None if it could not be recorded (never raises)."""
    try:
        hid = cl.mint_handoff_id()
        rec = cl.record("handoff", {"handoff_id": hid, "parent_task_id": parent_task_id, "path": path,
                                    "child_ref": child_ref, "delegate_label": delegate_label,
                                    "holds": "yes" if holds else "no",
                                    "parent_generation": str(parent_generation) if parent_generation is not None else None},
                        workspace=workspace)
        if not rec:
            return None
        d = _index_dir(workspace, parent_task_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{hid}.json").write_text(json.dumps(rec))
        key = _child_key(workspace, path, child_ref)
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps({"handoff_id": hid, "parent_task_id": parent_task_id}))
        return hid
    except Exception:  # noqa: BLE001 - bookkeeping beside the work
        return None


def _claim_close(workspace, rec: dict, outcome: str) -> bool:
    """Close one handoff once: the unlink of its index file is the claim; the winner records it."""
    try:
        os.unlink(_index_dir(workspace, rec["parent_task_id"]) / f"{rec['handoff_id']}.json")
    except OSError:
        return False  # another reader closed it first, or it was never indexed
    try:
        os.unlink(_child_key(workspace, rec["path"], rec["child_ref"]))
    except OSError:
        pass
    return cl.record("handoff_closed", {"handoff_id": rec["handoff_id"], "outcome": outcome},
                     workspace=workspace) is not None


def _indexed(workspace, parent_task_id: str) -> list:
    out = []
    d = _index_dir(workspace, parent_task_id)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        try:
            out.append(json.loads((d / n).read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("at", ""))


def close_handoff(workspace, handoff_id: str, outcome: str = "done") -> bool:
    """Close by id (the CLI's `close`): finds its index file under any parent."""
    root = Path(workspace) / INDEX
    try:
        parents = [p for p in root.iterdir() if p.is_dir() and p.name != "by-child"]
    except OSError:
        return False
    for p in parents:
        f = p / f"{handoff_id}.json"
        if f.exists():
            try:
                return _claim_close(workspace, json.loads(f.read_text()), outcome)
            except (OSError, ValueError):
                return False
    return False


def close_by_child(workspace, path: str, child_ref: str, outcome: str = "done") -> int:
    """Close the open handoff for one child (e.g. a subagent's tool_use_id); 1 when closed here."""
    try:
        ref = json.loads(_child_key(workspace, path, child_ref).read_text())
    except (OSError, ValueError):
        return 0  # nothing open for this child: the common case, a file lookup and no log read
    for rec in _indexed(workspace, ref["parent_task_id"]):
        if rec.get("handoff_id") == ref["handoff_id"]:
            return int(_claim_close(workspace, rec, outcome))
    return 0


def _child_ready(workspace, h: dict) -> bool:
    """A pool child's result is READY (a deliverable body in any candidate), not merely present."""
    if h["path"] != "pool":
        return False
    try:
        from local_task_protocol import iter_result_candidates
        from delivery.readiness import read_ready_result
        return any(read_ready_result(p) for p in iter_result_candidates(Path(workspace) / "results", h["child_ref"]))
    except Exception:  # noqa: BLE001 - unknown is open
        return False


def open_handoffs(workspace, task_id: str, *, holding_only: bool = False, now: "float | None" = None) -> list:
    """The task's handoffs still out, closing those whose work came back or timed out."""
    now = _now() if now is None else now
    out = []
    for h in _indexed(workspace, task_id):
        if _child_ready(workspace, h):
            _claim_close(workspace, h, "done")
            continue
        if now - _epoch(h.get("at")) > MAX_OPEN_S:
            _claim_close(workspace, h, "timeout")
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
    return (st or {}).get("line")


def delegated_status(workspace, task_id: str, *, now: "float | None" = None) -> "dict | None":
    """{phase: "Delegated", line, handoffs, holding, deadline} while any handoff is out, else None.
    `deadline` (epoch seconds) is when the oldest holding handoff times out: the expiry a parked
    parent's blocker should carry, so the parent can never stay parked past it."""
    out = open_handoffs(workspace, task_id, now=now)
    if not out:
        return None
    newest = out[-1]
    head = f"Delegated: {newest['delegate_label']}" if len(out) == 1 else f"Delegated: {len(out)} helpers"
    tail = _delegate_line(workspace, newest)
    line = f"{head}, {tail[0].lower() + tail[1:]}" if tail else head
    if len(line) > LINE_MAX:
        line = line[: LINE_MAX - 1].rstrip(" ,") + "…"
    holding = [h for h in out if h.get("holds") == "yes"]
    deadline = min(_epoch(h.get("at")) for h in holding) + MAX_OPEN_S if holding else None
    return {"phase": "Delegated", "line": line, "handoffs": len(out), "holding": len(holding), "deadline": deadline}


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
