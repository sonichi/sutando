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
import stat as stat_module
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # src/
from delivery.readiness import read_ready_result
from task_archive import task_id_from_filename
from local_task_protocol import (find_archived_task, find_result,  # noqa: E402
                                 parse_task_headers_lenient)
sys.path.insert(0, str(_HERE.parent.parent / "packages" / "ag2-sparrow"))
from ag2_sparrow.task_archive import find_task_file  # noqa: E402

_WS_RE = re.compile(r"[\r\n]+")


def _one_line(text: str) -> str:
    return _WS_RE.sub(" ", str(text)).strip()


# HITL request type → the task state it parks the task in (owner taxonomy:
# running → waiting_for_* → running; who acts next is the discriminator).
_WAITING_STATE = {"elicitation": "waiting_for_input",
                  "approval": "waiting_for_approval",
                  "human_action": "waiting_for_human_action"}


# This channel OWNS `task-rtapi-` and nothing else. Other sources' tasks carry
# other users' private text, so the prefix is an ownership boundary, not a name.
TASK_PREFIX = "task-rtapi-"
_SAFE_TASK_ID = re.compile(r"\Atask-rtapi-[A-Za-z0-9._-]+\Z")


def _checked_task_id(task_id) -> "str | None":
    """Confine client-supplied ids to the ids this channel owns: no
    separators, no traversal, and no other source's task — a foreign or
    hostile id must read as absent, never as a path or as someone else's
    work."""
    tid = str(task_id or "")
    if ".." in tid or not _SAFE_TASK_ID.fullmatch(tid):
        return None
    return tid


class TasksView:
    def __init__(self, tasks_dir: str | Path, results_dir: str | Path,
                 actor_id: str, hitl_lookup=None, instance: str | None = None):
        """`hitl_lookup(task_id) -> [requestType, ...]` lists the task's
        PENDING human-in-the-loop requests; injected by the composer (the
        daemon binds it to the request store) so this view stays store-free."""
        self.tasks_dir = Path(tasks_dir)
        self.results_dir = Path(results_dir)
        self.actor_id = actor_id
        self.hitl_lookup = hitl_lookup
        self.instance = instance

    # ── task.submit ─────────────────────────────────────────────────────────
    def submit(self, task_text: str, priority: str = "normal") -> dict:
        text = _one_line(task_text)
        if not text:
            raise ValueError("task text is required")
        if priority not in ("urgent", "normal", "low"):
            raise ValueError("priority must be urgent|normal|low")
        task_id = f"{TASK_PREFIX}{uuid.uuid4().hex[:12]}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content = (f"id: {task_id}\n"
                   f"timestamp: {stamp}\n"
                   f"task: {text}\n"
                   f"source: runtime-api\n"
                   f"channel_id: runtime-api\n"
                   f"user_id: {_one_line(self.actor_id)}\n"
                   f"access_tier: owner\n"
                   f"priority: {priority}\n"
                   + (f"instance_id: {_one_line(self.instance)}\n"
                      if self.instance else ""))
        # HMAC envelope (#3014 writer census): stamp at this writer's edge,
        # fail-open so a stamping error costs the stamp, never the submit.
        try:
            import sys as _sys
            # CODE-tree (src/) for imports — not workspace resolution
            _src = str(Path(__file__).resolve().parents[1])
            if _src not in _sys.path:
                _sys.path.insert(0, _src)
            from task_envelope import stamp_text
            content = stamp_text(content, self.tasks_dir.parent)
        except Exception:
            pass
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tasks_dir / f".{task_id}.tmp"
        tmp.write_text(content)
        os.replace(tmp, self.tasks_dir / f"{task_id}.txt")
        return {"taskId": task_id, "state": "pending"}

    # ── task.status ─────────────────────────────────────────────────────────
    def status(self, task_id: str) -> dict:
        if _checked_task_id(task_id) is None:
            return {"state": "not_found"}
        if self._ready_result(task_id) is not None:
            return {"taskId": task_id, "state": "done"}
        live = find_task_file(self.tasks_dir, task_id)
        if live is not None:
            waiting = self._waiting_state(task_id)
            if waiting:
                return {"taskId": task_id, "state": waiting["state"],
                        "waitingOn": waiting["requests"]}
            claimed = ".claimed-" in live.name
            return {"taskId": task_id,
                    "state": "in_progress" if claimed else "pending"}
        if find_archived_task(self.tasks_dir, task_id) is not None:
            # Archived task, result already delivered and rotated away.
            return {"taskId": task_id, "state": "done"}
        return {"taskId": task_id, "state": "unknown"}

    # ── task.get_result ─────────────────────────────────────────────────────
    def get_result(self, task_id: str | None = None) -> dict | None:
        # No id → the newest result, so a client can fetch "the last one"
        # without typing a full task id (the friction this removes).
        if not task_id:
            return self._latest_result()
        ready = self._ready_result(task_id)
        if ready is None:
            return None
        return {"taskId": task_id, "result": ready[1]}

    def _latest_result(self) -> dict | None:
        # Newest READY one: an unready newest file must not mask the answer
        # behind it, and must not be returned as an empty result either.
        for p, _ts in self._result_files():
            body = read_ready_result(p)
            if body is not None:
                return {"taskId": p.name.removesuffix(".txt"),
                        "result": body, "latest": True}
        return None

    def _result_files(self) -> "list[tuple[Path, int]]":
        """(path, mtime) newest first, for `task-rtapi-` results only — source
        isolation: other sources' results must not leak into this channel.

        Returns the mtime it already read rather than letting callers stat
        again: archival unlinks a live name, so a second stat can raise on a
        file that was present a moment earlier, and a caller that has already
        read the body would lose an answer it holds.
        """
        if not self.results_dir.is_dir():
            return []
        found = []
        for f in self.results_dir.glob(f"{TASK_PREFIX}*.txt"):
            try:
                st = f.stat()
            except OSError:
                continue  # archived mid-scan — absent, not an error
            if stat_module.S_ISREG(st.st_mode):
                found.append((f, int(st.st_mtime)))
        found.sort(key=lambda pair: pair[1], reverse=True)
        return found

    # ── task.list_results ────────────────────────────────────────────────────
    def list_results(self, limit: int = 50) -> dict:
        """Every available result (newest first) with a short preview, so a
        client can see what's there without knowing any id. Poll-state is not
        tracked yet — this lists ALL, not only un-fetched."""
        files = self._result_files()
        out = []
        truncated = False
        for f, ts in files:
            body = read_ready_result(f)
            if body is None:
                continue  # not an answer yet; listed on a later call
            if len(out) >= limit:
                truncated = True  # a READY result exists past the window
                break
            # `ts` comes from enumeration, so archiving between the body read
            # and here cannot turn a held answer into a FileNotFoundError.
            out.append({"taskId": f.name.removesuffix(".txt"),
                        "ts": ts,
                        "preview": body[:160]})
        return {"results": out,
                **({"truncated": True} if truncated else {})}

    # ── task.details ────────────────────────────────────────────────────────
    def details(self, task_id: str) -> dict | None:
        if _checked_task_id(task_id) is None:
            return None
        p = (find_task_file(self.tasks_dir, task_id)
             or find_archived_task(self.tasks_dir, task_id))
        if p is None:
            return None
        try:
            raw = p.read_text()
        except OSError:
            return None  # unreadable degrades to absent, same as every read here
        th = parse_task_headers_lenient(raw)
        out = {"taskId": task_id, "task": th.body,
               "state": self.status(task_id)["state"]}
        for k in ("source", "timestamp", "priority", "access_tier",
                  "instance_id"):
            v = th.get(k)
            if v:
                out[k] = v
        return out

    # ── task.list ───────────────────────────────────────────────────────────
    def list_tasks(self, limit: int = 200) -> dict:
        """Enumerate LIVE tasks (pending / claimed / waiting) so a fresh
        client can render "what's waiting" without knowing any taskId — the
        acceptance-test enumeration gap. Scoped to this channel's OWN tasks:
        enumeration is the same ownership boundary as a per-id read, so other
        sources' task text never appears here. Results/archive are
        deliberately not walked: done work is fetched per-id, not listed."""
        entries = []
        if self.tasks_dir.is_dir():
            files = [f for f in self.tasks_dir.glob(f"{TASK_PREFIX}*.txt")
                     if f.is_file()]
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            truncated = len(files) > limit
            for f in files[:limit]:
                task_id = task_id_from_filename(f.name) or f.name.removesuffix(".txt")
                entry = {"taskId": task_id,
                         "state": self.status(task_id)["state"]}
                try:
                    th = parse_task_headers_lenient(f.read_text())
                    for k in ("source", "priority", "timestamp", "instance_id"):
                        v = th.get(k)
                        if v:
                            entry[k] = v
                except OSError:
                    pass
                entries.append(entry)
        else:
            truncated = False
        return {"tasks": entries, **({"truncated": True} if truncated else {})}

    # ── task.cancel ─────────────────────────────────────────────────────────
    def cancel(self, task_id: str) -> dict:
        """Cancellation is a SIGNAL through the same pipeline (the documented
        CANCEL_INSTRUCTION mechanism) — the consumer decides whether the task
        is still cancellable; this never deletes files out from under it."""
        if _checked_task_id(task_id) is None:
            return {"ok": False, "error": "unknown task id"}
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
    def _waiting_state(self, task_id: str) -> dict | None:
        """A live task with pending HITL requests is parked, not running.
        With several pending requests the state reflects the FIRST by the
        input < approval < action order; all are listed in waitingOn."""
        if self.hitl_lookup is None:
            return None
        try:
            types = [t for t in self.hitl_lookup(task_id) if t in _WAITING_STATE]
        except Exception:  # noqa: BLE001 — a broken lookup ≠ broken task surface
            return None
        if not types:
            return None
        order = ("elicitation", "approval", "human_action")
        first = sorted(types, key=order.index)[0]
        return {"state": _WAITING_STATE[first],
                "requests": [_WAITING_STATE[t] for t in types]}

    def _result_path(self, task_id: str) -> Path | None:
        if _checked_task_id(task_id) is None:
            return None
        # Archive layouts (flat, monthly, epoch-suffixed) are owned by
        # local_task_protocol.find_result — never re-enumerated here.
        return find_result(self.results_dir, task_id)

    def _ready_result(self, task_id: str) -> "tuple[Path, str] | None":
        """The result file and its body, only once the body is deliverable.

        Readiness belongs to delivery.readiness, which the push path already
        uses: a result path exists before it holds an answer, so treating
        existence as done reports a task complete while its reply is still
        being written, and a client polling for a terminal state stops early.
        Unready is not an error — the file stays, and a later call sees it."""
        p = self._result_path(task_id)
        if p is None:
            return None
        body = read_ready_result(p)
        return None if body is None else (p, body)
