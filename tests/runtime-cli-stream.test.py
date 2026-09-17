#!/usr/bin/env python3
"""CLI streaming modes against a real daemon: task watch + task chat (pipe).

`task watch` must print the subscribe banner, then stream a task.result push
when a result lands. `task chat` in non-tty mode (the _chat_line twin built
for scripts and tests) must accept a stdin line, submit it, and stream the
result inline. Both are driven as production subprocesses; SIGTERM ends the
watch (coverage's sigterm handler flushes instrumentation).

Run: python3 tests/runtime-cli-stream.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"
TMP = tempfile.mkdtemp(prefix="cli-stream-")

PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "runtime-state.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(Path(TMP) / "state"),
       "SUTANDO_AGENT_ID": "@stream-test:example.org",
       "SUTANDO_HOST_LABEL": "cli-stream-host",
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TASKS_DIR": str(Path(TMP) / "tasks"),
       "SUTANDO_RESULTS_DIR": str(Path(TMP) / "results"),
       "REMOTE_TASK_URL": "",
       "REMOTE_TASK_TOKEN": "test-bearer"}

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def wait_socket(path, timeout=10):
    import socket as _s
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        try:
            s.connect(path)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def read_lines_until(proc, want, timeout=20):
    """Collect stdout lines until `want(line)` is true or timeout."""
    got: list = []
    hit = threading.Event()

    def pump():
        for line in proc.stdout:
            got.append(line)
            if want(line):
                hit.set()
                return

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    hit.wait(timeout)
    return got, hit.is_set()


def main() -> int:
    for d in ("tasks", "results", "state"):
        (Path(TMP) / d).mkdir(exist_ok=True)
    daemon = subprocess.Popen([*PYBASE, str(SERVER)], env=ENV,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    if not wait_socket(ENV["SUTANDO_RUNTIME_SOCKET"]):
        daemon.kill()
        print(daemon.stdout.read())
        raise AssertionError("daemon socket never came up")
    try:
        drive()
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
    print(f"\n{'FAILED' if FAILS else 'PASS'} — CLI streaming "
          f"({len(FAILS)} failure(s))")
    return 1 if FAILS else 0


def submit(text):
    p = subprocess.run([*PYBASE, str(CLI), "task", "submit", text],
                       capture_output=True, text=True, timeout=30, env=ENV)
    return (json.loads(p.stdout) or {}).get("taskId", "")


def drive():
    # ── task watch: banner, then a streamed result push ──
    w = subprocess.Popen([*PYBASE, str(CLI), "task", "watch", "--activity"],
                         env=ENV, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    got, ok = read_lines_until(w, lambda ln: '"watching": true' in ln)
    check(ok, "watch prints the subscribe banner")
    tid = submit("stream probe one")
    check(tid.startswith("task-"), "submit under watch returns a task id")
    (Path(ENV["SUTANDO_RESULTS_DIR"]) / f"{tid}.txt").write_text(
        "streamed result body")
    got, ok = read_lines_until(
        w, lambda ln: "streamed result body" in ln, timeout=25)
    check(ok, "watch streams the task.result push when the result lands")
    # activity frame: a core-status step change must reach the subscriber
    (Path(ENV["SUTANDO_RUNTIME_STATE"]) / "core-status.json").write_text(
        json.dumps({"status": "running", "step": "probing the stream",
                    "ts": int(time.time())}))
    got, ok = read_lines_until(
        w, lambda ln: "probing the stream" in ln, timeout=25)
    check(ok, "watch --activity streams the core-status step change")
    # tool-feed frame: an activity-feed.jsonl line reaches the subscriber too
    with (Path(ENV["SUTANDO_RUNTIME_STATE"]) / "activity-feed.jsonl").open("a") as fh:
        fh.write(json.dumps({"kind": "tool", "step": "feed probe line",
                             "ts": int(time.time())}) + "\n")
    got, ok = read_lines_until(
        w, lambda ln: "feed probe line" in ln, timeout=25)
    check(ok, "watch --activity streams the tool feed line")
    # SIGINT, not SIGTERM: the watch loop's Ctrl-C path must close and exit 0
    w.send_signal(signal.SIGINT)
    try:
        w.wait(timeout=10)
    except subprocess.TimeoutExpired:
        w.kill()

    # ── request wait <id>: blocks until the request reaches a terminal state ──
    p = subprocess.run([*PYBASE, str(CLI), "elicitation", "request",
                        "--question", "stream wait probe?",
                        "--type", "single_select",
                        "--options", '["yes","no"]', "--expires-in", "60"],
                       capture_output=True, text=True, timeout=30, env=ENV)
    rid = (json.loads(p.stdout) or {}).get("requestId", "")
    check(bool(rid), "elicitation request returns a requestId")
    rw = subprocess.Popen([*PYBASE, str(CLI), "request", "wait", rid,
                           "--timeout", "20"],
                          env=ENV, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, bufsize=1)
    time.sleep(1.5)  # waiter must be blocking before the cancel lands
    subprocess.run([*PYBASE, str(CLI), "request", "cancel", rid],
                   capture_output=True, text=True, timeout=30, env=ENV)
    got, ok = read_lines_until(
        rw, lambda ln: "cancel" in ln, timeout=25)
    check(ok, "request wait unblocks when the request is cancelled")
    try:
        rw.wait(timeout=10)
    except subprocess.TimeoutExpired:
        rw.kill()

    # ── requests stream: raw subscribe gets the request.pending push ──
    import socket as _s
    rs = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    rs.connect(ENV["SUTANDO_RUNTIME_SOCKET"])
    rs.sendall((json.dumps({"jsonrpc": "2.0", "id": "sub",
                            "method": "task.subscribe",
                            "params": {"requests": True}}) + "\n").encode())
    rs.settimeout(25)
    buf = b""
    while b"\n" not in buf:
        buf += rs.recv(65536)
    subprocess.run([*PYBASE, str(CLI), "elicitation", "request",
                    "--question", "push probe?", "--type", "single_select",
                    "--options", '["a","b"]', "--expires-in", "60"],
                   capture_output=True, text=True, timeout=30, env=ENV)
    got_push = False
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            buf += rs.recv(65536)
            if b"request.pending" in buf and b"push probe" in buf:
                got_push = True
                break
    except OSError:
        pass
    check(got_push, "requests subscriber receives the request.pending push")
    # crash the subscriber WITHOUT unsubscribe: the daemon must discard the
    # dead writer on the next push and keep serving
    rs.close()
    (Path(ENV["SUTANDO_RUNTIME_STATE"]) / "core-status.json").write_text(
        json.dumps({"status": "running", "step": "post-crash step",
                    "ts": int(time.time())}))
    subprocess.run([*PYBASE, str(CLI), "elicitation", "request",
                    "--question", "after crash?", "--type", "single_select",
                    "--options", '["a","b"]', "--expires-in", "60"],
                   capture_output=True, text=True, timeout=30, env=ENV)
    # dwell past a few watcher polls: the no-subscriber idle branches run
    time.sleep(2.5)
    p = subprocess.run([*PYBASE, str(CLI), "sutando", "info"],
                       capture_output=True, text=True, timeout=30, env=ENV)
    check(p.returncode == 0,
          "daemon survives a crashed subscriber and keeps serving")

    # ── task chat (non-tty): stdin line → submit → inline result ──
    c = subprocess.Popen([*PYBASE, str(CLI), "task", "chat"],
                         env=ENV, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    got, ok = read_lines_until(c, lambda ln: "sutando chat" in ln)
    check(ok, "chat (pipe mode) prints its banner")
    before = set(Path(ENV["SUTANDO_TASKS_DIR"]).glob("task-*.txt"))
    c.stdin.write("chat probe task\n")
    c.stdin.flush()
    got, ok = read_lines_until(c, lambda ln: "chat probe task" in ln)
    check(ok, "chat echoes the submitted line in a you-block")

    def answer_new_task():
        # the daemon assigns the id; answer the task the chat submit CREATED
        deadline = time.time() + 15
        while time.time() < deadline:
            new = set(Path(ENV["SUTANDO_TASKS_DIR"]).glob("task-*.txt")) - before
            if new:
                tid2 = sorted(new)[-1].stem
                (Path(ENV["SUTANDO_RESULTS_DIR"]) / f"{tid2}.txt").write_text(
                    "chat reply body")
                return
            time.sleep(0.3)

    threading.Thread(target=answer_new_task, daemon=True).start()
    got, ok = read_lines_until(
        c, lambda ln: "chat reply body" in ln, timeout=25)
    check(ok, "chat streams the agent reply inline")
    c.stdin.close()
    try:
        c.wait(timeout=10)
    except subprocess.TimeoutExpired:
        c.kill()
    check(c.returncode is not None, "chat exits after stdin EOF")


if __name__ == "__main__":
    sys.exit(main())
