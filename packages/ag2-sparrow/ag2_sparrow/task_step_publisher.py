"""Publish connection-owned working steps to the owner's task ledger through room Actions."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .chat_secret_filter import filter_chat_secrets

SUMMARY_MAX = 140
LINE_MAX = 60
INTERVAL = 10.0
TAIL_BYTES = 256 * 1024
# room.task.progress's `phase` enum and `waiting` reasons (the backend's TaskProgressArguments).
PHASES = ("Understanding the request", "Thinking", "Reading files", "Searching code", "Searching the web",
          "Running a command", "Editing code", "Running tests", "Reviewing changes", "Writing a reply",
          "Asking someone", "Waiting", "Delegated")
WAITING = ("queued", "busy", "delegated")


def _code(value) -> str:
    """An error code or status as it may be logged: a short identifier, never payload text."""
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", text) else "unknown"


def progress_arguments(status) -> "dict | None":
    """room.task.progress arguments from a task's published status (phase_line.published_status):
    summary, phase, line when it says more than the phase, since as an aware timestamp, waiting and
    queue_ahead. None when there is nothing to publish (a finished or unknown task)."""
    if not isinstance(status, dict) or status.get("phase") not in PHASES:
        return None
    summary = " ".join(filter_chat_secrets(str(status.get("summary") or "")).text.split())[:SUMMARY_MAX]
    if not summary:
        return None
    args = {"summary": summary, "phase": status["phase"]}
    if summary != status["phase"] and len(summary) <= LINE_MAX:
        args["line"] = summary
    since = status.get("since")
    if isinstance(since, (int, float)) and not isinstance(since, bool) and math.isfinite(since) and since > 0:
        args["since"] = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
    waiting = status.get("waiting") if isinstance(status.get("waiting"), dict) else {}
    if waiting.get("reason") in WAITING:
        args["waiting"] = waiting["reason"]
        position = waiting.get("position")
        if waiting["reason"] == "queued" and type(position) is int and position >= 1:
            args["queue_ahead"] = position - 1
    return args


class StepPublisher:
    def __init__(self, state_dir: Path, send, log=print, now=time.monotonic, status_of=None):
        self.path = Path(state_dir) / "agent-activity.jsonl"
        self.send, self.log, self.now = send, log, now
        # (local task id) -> published status, from the host. Set, only its typed fields are sent;
        # unset (a standalone package), the latest activity row's text is the summary.
        self.status_of = status_of
        self.sent = {}
        self.attempted = {}

    def publish(self, tasks: dict, open_now=None) -> None:
        """Tasks are live envelopes selected by this connection, keyed by their local run id.
        `open_now(tid)`, when given, is asked right before each typed send: False skips the task."""
        self.sent = {k: v for k, v in self.sent.items() if k in tasks}
        self.attempted = {k: v for k, v in self.attempted.items() if k in tasks}
        if not tasks:
            return
        if self.status_of is not None:
            self._publish_typed(tasks, open_now)
            return
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, 2)
                start = max(0, stream.tell() - TAIL_BYTES)
                stream.seek(start)
                if start:
                    stream.readline()
                lines = stream.read().splitlines()
        except OSError:
            return
        latest, terminal = {}, set()
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or not isinstance(row.get("task"), dict):
                continue
            tid = row["task"].get("id")
            if not isinstance(tid, str) or tid not in tasks:
                continue
            if row.get("done") or row.get("kind") == "done":
                terminal.add(tid)
            if row.get("kind") not in ("working", "thinking") or row.get("projection") or row.get("audience"):
                continue
            text = row.get("line")
            if not row.get("room") or not isinstance(text, str) or not text.strip():
                continue
            if row["room"] != tasks[tid].get("room_id"):
                continue
            latest[tid] = (" ".join(filter_chat_secrets(text).text.split())[:SUMMARY_MAX], row.get("ts"))
        for tid, (summary, stamp) in latest.items():
            task = tasks[tid]
            gen, wire, room = (task.get(k) for k in ("execution_generation", "task_id", "room_id"))
            if tid in terminal or type(gen) is not int or gen < 1 or not wire or not room:
                continue
            identity = (wire, gen, summary)
            now = self.now()
            if self.sent.get(tid) == identity or now - self.attempted.get(tid, -float("inf")) < INTERVAL:
                continue
            key = hashlib.sha256(json.dumps([wire, gen, summary, stamp], ensure_ascii=False).encode()).hexdigest()
            payload = {"action": "room.task.progress", "target_kind": "room", "room_id": room,
                       "task_id": wire, "execution_generation": gen, "operation_id": "step-" + key,
                       "arguments": {"summary": summary}}
            self.attempted[tid] = now
            try:
                response = self.send(payload)
                if isinstance(response, dict) and response.get("status") == "not_deployed":
                    self.log(f"task step {tid}: room.task.progress not deployed; skipping")
                    continue
                if not isinstance(response, dict) or response.get("status") != "completed":
                    raise ValueError("Action did not succeed")
            except Exception as exc:
                self.log(f"task step {tid}: Action failed ({type(exc).__name__}); continuing")
                continue
            self.sent[tid] = identity

    def _publish_typed(self, tasks: dict, open_now=None) -> None:
        for tid, task in tasks.items():
            gen, wire, room = (task.get(k) for k in ("execution_generation", "task_id", "room_id"))
            if type(gen) is not int or gen < 1 or not wire or not room:
                continue
            if open_now is not None and not open_now(tid):
                continue
            try:
                args = progress_arguments(self.status_of(tid))
            except Exception:  # noqa: BLE001 - a status that cannot be read is nothing to publish
                args = None
            if args is None:
                continue
            if self._send(tid, wire, gen, room, args, json.dumps(args, sort_keys=True)):
                self.log(f"task step {tid}: published {args['phase']}"
                         + (f" ({args['waiting']})" if args.get("waiting") else ""))

    def _send(self, tid, wire, gen, room, args, stamp) -> bool:
        """Send one step unless it is unchanged or throttled; True when it was published now."""
        identity = (wire, gen, stamp)
        now = self.now()
        if self.sent.get(tid) == identity or now - self.attempted.get(tid, -float("inf")) < INTERVAL:
            return False
        key = hashlib.sha256(json.dumps([wire, gen, stamp], ensure_ascii=False).encode()).hexdigest()
        payload = {"action": "room.task.progress", "target_kind": "room", "room_id": room,
                   "task_id": wire, "execution_generation": gen, "operation_id": "step-" + key, "arguments": args}
        self.attempted[tid] = now
        try:
            response = self.send(payload)
            if isinstance(response, dict) and response.get("status") == "not_deployed":
                self.log(f"task step {tid}: room.task.progress not deployed; skipping")
                return False
            if isinstance(response, dict) and response.get("is_error"):
                self.log(f"task step {tid}: Action refused ({_code(response.get('code'))}); continuing")
                return False
            if not isinstance(response, dict) or response.get("status") != "completed":
                status = response.get("status") if isinstance(response, dict) else type(response).__name__
                self.log(f"task step {tid}: Action answered {_code(status)}, not completed; continuing")
                return False
        except Exception as exc:
            self.log(f"task step {tid}: Action failed ({type(exc).__name__}); continuing")
            return False
        self.sent[tid] = identity
        return True
