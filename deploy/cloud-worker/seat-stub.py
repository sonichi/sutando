#!/usr/bin/env python3
"""Stub seat runtime for a cloud worker container.

Answers every pending `tasks/task-<id>.txt` with `answered by <worker id>` in
`results/`, the same seat simulation tests/gateway-worker-queue-client.test.py
drives — so the container round trip is testable without an LLM. Not a seat
for real work: SUTANDO_WORKER_RUNTIME=claude|adapter is.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

# The task-protocol owner decides what a pending filename is; a private copy of
# that rule here is what dropped legal dotted broker ids.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from task_archive import is_pending_task_file  # noqa: E402

WS = Path(os.environ.get("SUTANDO_CLOUD_WORKSPACE") or "/workspace")
WORKER = os.environ.get("SUTANDO_WORKER_ID") or "cloud"
SCAN_S = float(os.environ.get("SUTANDO_STUB_SCAN_S") or "1.0")
_STOP = False


def _stop(*_a) -> None:
    global _STOP
    _STOP = True


def answer(task: Path, results: Path) -> bool:
    out = results / task.name
    if out.exists():
        return False
    tmp = out.with_name(out.name + f".{os.getpid()}.tmp")
    tmp.write_text(f"answered by {WORKER}\n", encoding="utf-8")
    os.replace(tmp, out)
    return True


def main() -> int:
    tasks, results = WS / "tasks", WS / "results"
    results.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    done: set[str] = set()
    print(f"seat-stub: worker={WORKER} watching {tasks}", flush=True)
    while not _STOP:
        for task in sorted(tasks.glob("task-*.txt")) if tasks.is_dir() else []:
            if not is_pending_task_file(task.name) or task.name in done:
                continue
            done.add(task.name)
            if answer(task, results):
                print(f"seat-stub: answered {task.stem}", flush=True)
        time.sleep(SCAN_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
