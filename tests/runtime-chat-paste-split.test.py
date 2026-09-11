#!/usr/bin/env python3
"""Chat TUI unmarked-paste guard: a multi-line paste WITHOUT bracketed-paste
markers must not fragment into one task per line.

Live regression 2026-08-09: the owner pasted a spec into `sutando task chat`
through a path that never sent \\x1b[200~, so every newline hit Enter and ONE
message became 17 tasks. The fix classifies a read burst with INTERIOR newlines
(or a continuation burst within 150ms) as a paste and treats its newlines as
separators, never submits.

Boots the REAL daemon + the REAL chat TUI in a pty, then:
  1. feeds a 5-line paste as one unmarked burst → asserts ZERO tasks created;
  2. sends a real Enter (a lone newline, after the window) → asserts exactly ONE
     task whose body is the joined paste;
  3. control: typing then a lone Enter still submits normally.

Run: python3 tests/runtime-chat-paste-split.test.py
"""
from __future__ import annotations

import json
import os
import pty
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def wait_socket(path, timeout=10):
    dl = time.time() + timeout
    while time.time() < dl:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path); s.close(); return True
        except OSError:
            time.sleep(0.1)
    return False


TMP = tempfile.mkdtemp(prefix="chat-paste-")
TASKS = Path(TMP) / "tasks"
ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_INSTANCE_ID": "chat-paste-test",
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "rt.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(Path(TMP) / "state"),
       "SUTANDO_AGENT_ID": "@paste-test:example.org",
       "SUTANDO_HOST_LABEL": "paste-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/paste-tmux.sock",
       "SUTANDO_TMUX_SESSION": "paste-core",
       "SUTANDO_LAUNCHER_EXECUTABLE": str(REPO / "bin" / "sutando"),
       "SUTANDO_LAUNCHER_ARGS": '["serve"]',
       "TERM": "xterm-256color"}


def live_tasks():
    return sorted(TASKS.glob("task-rtapi-*.txt")) if TASKS.is_dir() else []


def main() -> int:
    # Daemon output to DEVNULL — a PIPE nobody reads can fill and block, and a
    # surviving grandchild holding a captured pipe wedges outer test runners.
    daemon = subprocess.Popen([sys.executable, str(SERVER)], env=ENV,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    master, slave = pty.openpty()
    # Drain the master continuously: the TUI redraws on every keystroke, and an
    # undrained pty buffer fills → the TUI's stdout write blocks → its event
    import select
    import threading
    stop = threading.Event()

    def _drain():
        while not stop.is_set():
            try:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    if not os.read(master, 65536):
                        break
            except OSError:
                break
    drain_t = threading.Thread(target=_drain, daemon=True)
    drain_t.start()
    tui = None
    try:
        if not wait_socket(ENV["SUTANDO_RUNTIME_SOCKET"]):
            raise AssertionError("daemon socket never came up")
        tui = subprocess.Popen([sys.executable, str(CLI), "task", "chat"],
                               env=ENV, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)  # child holds its own copies; ours would block EOF
        time.sleep(2.0)  # let the TUI boot + subscribe

        # 1. an unmarked multi-line paste in ONE burst → no submits
        paste = b"line one of the spec\rline two here\rline three\rline four\rline five"
        os.write(master, paste)
        time.sleep(1.0)
        check(len(live_tasks()) == 0,
              "unmarked multi-line paste creates NO tasks (was: one per line)")

        # 2. a real Enter after the paste window submits ONE joined task
        time.sleep(0.3)  # step past the 150ms continuation window
        os.write(master, b"\r")
        time.sleep(1.2)
        t = live_tasks()
        body = t[0].read_text() if t else ""
        check(len(t) == 1 and "line one of the spec line two here" in body
              and "line five" in body,
              f"Enter after the paste submits ONE task with the joined text ({len(t)} tasks)")

        # 3. control: normal typing + Enter still submits
        os.write(master, b"hello normal typing")
        time.sleep(0.4)
        os.write(master, b"\r")
        time.sleep(1.2)
        t2 = live_tasks()
        check(len(t2) == 2 and any("hello normal typing" in f.read_text() for f in t2),
              "normal typed message + Enter still submits (control)")
    finally:
        if tui:
            try:
                os.write(master, b"\x03")  # Ctrl-C → clean exit
                tui.wait(timeout=5)
            except Exception:
                tui.kill()
        stop.set()
        drain_t.join(timeout=2)
        try:
            os.close(master)
        except OSError:
            pass
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
    print(f"\n{'PASS — unmarked-paste guard green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
