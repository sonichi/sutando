#!/usr/bin/env python3
"""A real fswatch burst into the watcher's event FIFO, beside a sweep writer, under
a slow reader: every line arrives whole and fswatch survives the backpressure.

Uses the watcher's own launcher (src/inbox-events.sh `start_inbox_events`) and the
sweep's write shape (one printf per line into the same FIFO). `BURST_LAUNCH=direct`
runs fswatch straight into the FIFO, the launch this replaced, for comparison.

Run: python3 tests/inbox-events-burst.test.py
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
N_FILES = int(os.environ.get("BURST_FILES", "3000"))
N_SWEEP = int(os.environ.get("BURST_SWEEP", "3000"))
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    if shutil.which("fswatch") is None:
        print("SKIP: no fswatch on this host")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="inbox-burst-")).resolve()
    inbox, fifo = tmp / "inbox", tmp / "events"
    inbox.mkdir()
    os.mkfifo(fifo)
    rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    if os.environ.get("BURST_LAUNCH") == "direct":
        launch = (f'fswatch -l 0.5 --event Created --event Renamed --event Updated "{inbox}" '
                  f'> "{fifo}" 2>/dev/null & echo $!; wait')
    else:
        launch = (f'. "{REPO}/src/inbox-events.sh"; start_inbox_events "{sys.executable}" "{fifo}" "{inbox}"; '
                  f'echo $!; wait')
    host = subprocess.Popen(["bash", "-c", launch], stdout=subprocess.PIPE, text=True, start_new_session=True)
    fsw_pid = int(host.stdout.readline())
    time.sleep(1.5)
    data = bytearray()
    stop = threading.Event()

    def slow_reader() -> None:
        while not stop.is_set():
            if select.select([rfd], [], [], 0.2)[0]:
                chunk = os.read(rfd, 256)
                if chunk:
                    data.extend(chunk)
            time.sleep(0.002)

    reader = threading.Thread(target=slow_reader)
    reader.start()
    sweep = subprocess.Popen(
        ["bash", "-c", f'for i in $(seq 1 {N_SWEEP}); do printf "%s/task-sweep%06d.txt\\n" "{inbox}" "$i"; done > "{fifo}"'])
    started = time.time()
    for i in range(N_FILES):
        (inbox / f"task-burst{i:06d}.txt").write_text("")
    burst = {f"{inbox}/task-burst{i:06d}.txt" for i in range(N_FILES)}
    swept = {f"{inbox}/task-sweep{i:06d}.txt" for i in range(1, N_SWEEP + 1)}
    deadline = time.time() + 90
    while time.time() < deadline:
        seen = set(data.decode(errors="replace").split("\n"))
        if burst <= seen and swept <= seen:
            break
        if sweep.poll() is not None and not _alive(fsw_pid):
            time.sleep(2)
            break
        time.sleep(0.5)
    alive = _alive(fsw_pid)
    stop.set()
    reader.join()
    sweep.wait()
    try:
        os.killpg(host.pid, 15)
    except (ProcessLookupError, PermissionError):
        pass
    lines = [ln for ln in data.decode(errors="replace").split("\n") if ln]
    valid = burst | swept | {str(inbox)}
    malformed = [ln for ln in lines if ln not in valid]
    print(f"  launch={os.environ.get('BURST_LAUNCH', 'relay')} files={N_FILES} sweep_lines={N_SWEEP} "
          f"lines_read={len(lines)} malformed={len(malformed)} burst_missing={len(burst - set(lines))} "
          f"sweep_missing={len(swept - set(lines))} fswatch_alive={alive} took={time.time() - started:.1f}s")
    for ln in malformed[:3]:
        print("    malformed:", repr(ln[:200]))
    check("no malformed or merged line in the shared FIFO", not malformed)
    check("every burst event arrived", burst <= set(lines))
    check("every sweep line arrived", swept <= set(lines))
    check("fswatch survived the backpressure", alive)
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nPASS" if not FAILURES else f"\nFAIL — {len(FAILURES)} check(s) failed")
    return 1 if FAILURES else 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


if __name__ == "__main__":
    sys.exit(main())
