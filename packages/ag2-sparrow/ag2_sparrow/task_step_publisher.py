"""Publish connection-owned working steps to the owner's task ledger through room Actions."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

SUMMARY_MAX = 140
INTERVAL = 10.0
TAIL_BYTES = 256 * 1024


class StepPublisher:
    def __init__(self, state_dir: Path, send, log=print, now=time.monotonic):
        self.path = Path(state_dir) / "agent-activity.jsonl"
        self.send, self.log, self.now = send, log, now
        self.sent = {}
        self.attempted = {}

    def publish(self, tasks: dict) -> None:
        """Tasks are live envelopes selected by this connection, keyed by their local run id."""
        self.sent = {k: v for k, v in self.sent.items() if k in tasks}
        self.attempted = {k: v for k, v in self.attempted.items() if k in tasks}
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
            latest[tid] = (" ".join(text.split())[:SUMMARY_MAX], row.get("ts"))
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
            payload = {"action": "room.task.update", "target_kind": "room", "room_id": room,
                       "task_id": wire, "execution_generation": gen, "operation_id": "step-" + key,
                       "arguments": {"status": "working", "summary": summary}}
            self.attempted[tid] = now
            try:
                response = self.send(payload)
                if not isinstance(response, dict) or response.get("status") != "completed":
                    raise ValueError("Action did not succeed")
            except Exception as exc:
                self.log(f"task step {tid}: Action failed ({type(exc).__name__}); continuing")
                continue
            self.sent[tid] = identity
