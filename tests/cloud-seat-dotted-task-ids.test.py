#!/usr/bin/env python3
r"""A legal dotted broker id must reach a cloud seat, and a claimed sibling must not.

keweichen's blocker 1 on #3803: both seats carried a private `^task-[^.]+\.txt$`,
so `_local_tid()`'s `task-cloud~task-a.b.txt` stayed pending forever while gateway
health remained fresh. The rule now lives once, in the task-protocol owner.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  ok  " if ok else "  FAIL ") + msg)
    if not ok:
        FAILS.append(msg)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    owner = _load(ROOT / "src" / "task_archive.py", "ta_owner")

    print("1. the owner's classifier accepts dots and rejects state suffixes")
    for name, want in [
        ("task-a.txt", True),
        ("task-a.b.txt", True),
        ("task-cloud~task-a.b.txt", True),      # the exact shape _local_tid mints
        ("task-a.b.claimed-core-2.txt", False),
        ("task-a.b.assigned-cloud.txt", False),
        ("task-a.txt.5", False),
    ]:
        check(owner.is_pending_task_file(name) is want,
              f"{name} -> {owner.is_pending_task_file(name)} (want {want})")

    print("2. NEITHER seat keeps a private filename rule (that is the defect)")
    for seat in ("seat-stub.py", "seat-ag2-assistant.py"):
        src = (ROOT / "deploy" / "cloud-worker" / seat).read_text()
        check("[^.]" not in src, f"{seat}: no `[^.]` id class remains")
        check("is_pending_task_file" in src, f"{seat}: uses the shared classifier")

    print("3. seat-stub's REAL scan loop answers a dotted id and skips claimed/assigned")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()
        dotted = "task-cloud~task-a.b.txt"
        (ws / "tasks" / dotted).write_text("task: dotted id\n")
        (ws / "tasks" / "task-x.b.claimed-core-2.txt").write_text("task: someone else's\n")
        (ws / "tasks" / "task-y.b.assigned-other.txt").write_text("task: assigned away\n")
        env = {**os.environ, "SUTANDO_CLOUD_WORKSPACE": str(ws),
               "SUTANDO_WORKER_ID": "cloud-t", "SUTANDO_STUB_SCAN_S": "0.1"}
        proc = subprocess.Popen([sys.executable, str(ROOT / "deploy" / "cloud-worker" / "seat-stub.py")],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 12
            while time.time() < deadline:
                if list((ws / "results").glob("*.txt")):
                    break
                time.sleep(0.2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        got = sorted(p.name for p in (ws / "results").glob("*.txt"))
        check(got == ["task-cloud~task-a.b.txt"],
              f"only the dotted PLAIN task was answered by the real loop: {got}")
        check(not any("claimed" in n or "assigned" in n for n in got),
              "no claimed/assigned sibling was consumed")

    print("\n" + (f"FAILED ({len(FAILS)})" if FAILS else "PASS — dotted broker ids reach a cloud seat"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
