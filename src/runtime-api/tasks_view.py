#!/usr/bin/env python3
"""Task-pipeline surface for the Sutando Server: task.submit / task.status /
task.get_result / task.details / task.cancel.

This is a thin binding over the EXISTING durable task/result file pipeline —
lifecycle policy stays with its owners (src/local_task_protocol.py for
parsing + archive lookup, ag2_sparrow.task_archive.find_task_file for
live/claimed lookup); nothing here re-implements claim/recovery/collision
rules. Submit writes the same header shape the chat-task convention uses;
cancel writes a CANCEL_INSTRUCTION task, the documented cancel signal the
core already honors.

Submitted text is header-confined: newlines collapse to spaces so a body can
never inject header lines or in-band instruction fences (the bee-watcher P1
class). The socket is same-user local RPC, so submissions carry the
daemon-resolved actor and owner tier — same authority the user's own shell
already has.
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # src/
from local_task_protocol import (find_archived_task,  # noqa: E402
                                 parse_task_headers_lenient)
sys.path.insert(0, str(_HERE.parent.parent / "packages" / "ag2-sparrow"))
from ag2_sparrow.task_archive import find_task_file  # noqa: E402

_WS_RE = re.compile(r"[\r\n]+")


def _one_line(text: str) -> str:
    return _WS_RE.sub(" ", str(text)).strip()


class TasksView:
    def __init__(self, tasks_dir: str | Path, results_dir: str | Path,
                 actor_id: str):
        self.tasks_dir = Path(tasks_dir)
        self.results_dir = Path(results_dir)
        self.actor_id = actor_id

    # ── task.submit ─────────────────────────────────────────────────────────
    def submit(self, task_text: str, priority: str = "normal") -> dict:
        text = _one_line(task_text)
        if not text:
            raise ValueError("task text is required")
        if priority not in ("urgent", "normal", "low"):
            raise ValueError("priority must be urgent|normal|low")
        task_id = f"task-rtapi-{uuid.uuid4().hex[:12]}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content = (f"id: {task_id}\n"
                   f"timestamp: {stamp}\n"
                   f"task: {text}\n"
                   f"source: runtime-api\n"
                   f"channel_id: runtime-api\n"
                   f"user_id: {_one_line(self.actor_id)}\n"
                   f"access_tier: owner\n"
                   f"priority: {priority}\n")
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tasks_dir / f".{task_id}.tmp"
        tmp.write_text(content)
        os.replace(tmp, self.tasks_dir / f"{task_id}.txt")
        return {"taskId": task_id, "state": "pending"}

    # ── task.status ─────────────────────────────────────────────────────────
    def status(self, task_id: str) -> dict:
        result = self._result_path(task_id)
        if result is not None:
            return {"taskId": task_id, "state": "done"}
        live = find_task_file(self.tasks_dir, task_id)
        if live is not None:
            claimed = ".claimed-" in live.name
            return {"taskId": task_id,
                    "state": "in_progress" if claimed else "pending"}
        if find_archived_task(self.tasks_dir, task_id) is not None:
            # Archived task, result already delivered and rotated away.
            return {"taskId": task_id, "state": "done"}
        return {"taskId": task_id, "state": "unknown"}

    # ── task.get_result ─────────────────────────────────────────────────────
    def get_result(self, task_id: str) -> dict | None:
        p = self._result_path(task_id)
        if p is None:
            return None
        try:
            return {"taskId": task_id, "result": p.read_text()}
        except OSError:
            return None

    # ── task.details ────────────────────────────────────────────────────────
    def details(self, task_id: str) -> dict | None:
        p = (find_task_file(self.tasks_dir, task_id)
             or find_archived_task(self.tasks_dir, task_id))
        if p is None:
            return None
        th = parse_task_headers_lenient(p.read_text())
        out = {"taskId": task_id, "task": th.body,
               "state": self.status(task_id)["state"]}
        for k in ("source", "timestamp", "priority", "access_tier"):
            v = th.get(k)
            if v:
                out[k] = v
        return out

    # ── task.cancel ─────────────────────────────────────────────────────────
    def cancel(self, task_id: str) -> dict:
        """Cancellation is a SIGNAL through the same pipeline (the documented
        CANCEL_INSTRUCTION mechanism) — the consumer decides whether the task
        is still cancellable; this never deletes files out from under it."""
        st = self.status(task_id)["state"]
        if st == "unknown":
            raise ValueError(f"unknown task: {task_id}")
        if st == "done":
            return {"taskId": task_id, "state": "done", "cancelled": False,
                    "note": "already completed — nothing to cancel"}
        sub = self.submit(f"CANCEL_INSTRUCTION: {task_id}", priority="urgent")
        return {"taskId": task_id, "state": st, "cancelled": "requested",
                "cancelTaskId": sub["taskId"]}

    # ── internals ───────────────────────────────────────────────────────────
    def _result_path(self, task_id: str) -> Path | None:
        for p in (self.results_dir / f"{task_id}.txt",
                  self.results_dir / "archive" / f"{task_id}.txt"):
            if p.is_file():
                return p
        return None
