#!/usr/bin/env python3
"""Run the routine or Codex adapter through fixed scenarios and print what it did, as JSON.

For each scenario the summary holds every call the adapter handed its provider (the routine's HTTP
requests, the Codex JSON-RPC messages), the bytes of every result it wrote, and its state files with
their real values (parsed JSON, full lists, file contents). Only what differs per run is replaced:
temp paths, ids, wall-clock times and durations. Only each adapter's command line is used, so the
same driver runs any revision of the package that `PYTHONPATH` names.
    python3 adapter_differential_driver.py routine|codex <unused dir>
"""
import contextlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TASKS = [  # (id, tier, channel, body, mtime)
    ("task-1700000000001", "owner", "!room:ag2.space", "first owner task", 1000),
    ("task-1700000000002", "guest", "!room:ag2.space", "a guest task", 1001),
    ("task-1700000000003", "owner", None, "no room here", 1002),
    ("task-1700000000004", "owner", "!other:ag2.space", "second owner task", 1003),
]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|UNTRUSTED_TASK_[0-9a-f]{8}")
TIMING_KEYS = ("detect_s", "turn_s", "file_to_file_s")
TEE = r"""
import subprocess, sys
log = open(sys.argv[1], "a")
child = subprocess.Popen([sys.executable, sys.argv[2]], stdin=subprocess.PIPE, text=True)
for line in sys.stdin:
    log.write(line)
    log.flush()
    child.stdin.write(line)
    child.stdin.flush()
child.stdin.close()
child.wait()
"""


def write_task(work, tid, tier, channel, body, mtime):
    lines = ["id: " + tid, "timestamp: 2026-10-16T00:00:00Z", "source: ag2space"]
    if channel:
        lines.append("channel_id: " + channel)
    lines += ["access_tier: " + tier, "priority: normal", "task: " + body]
    p = work / "tasks" / (tid + ".txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(p, (mtime, mtime))


def norm_text(base, text):
    text = text.replace(os.path.realpath(base), "<base>").replace(str(base), "<base>")
    text = re.sub(r"[^\s\"=\]]*codex-nonowner-[A-Za-z0-9_]+", "<tmp>", text)
    return UUID.sub("<id>", text)


def norm(base, value):
    """Real values, with only the per-run parts replaced."""
    if isinstance(value, dict):
        return {k: ("<n>" if k in TIMING_KEYS else norm(base, v)) for k, v in value.items()}
    if isinstance(value, list):
        return [norm(base, v) for v in value]
    if isinstance(value, float) and value > 1e9:
        return "<t>"
    if isinstance(value, str):
        return norm_text(base, value)
    return value


def read_state(base, work):
    out = {}
    for p in sorted((work / "state").rglob("*")):
        if not p.is_file() or p.name == "connector.pid":
            continue
        rel = str(p.relative_to(work / "state"))
        text = p.read_text()
        if p.suffix == ".json":
            value = norm(base, json.loads(text))
        elif p.suffix == ".jsonl":
            value = [norm(base, json.loads(line)) for line in text.splitlines()]
        else:
            value = norm_text(base, text)
        out[rel] = value
    return out


def drop_lock(work):
    """Remove the lock dir a holder_lock revision keeps outside the work root (older ones keep none)."""
    try:
        from ag2_sparrow import holder_lock
    except ImportError:
        return
    shutil.rmtree(holder_lock.lock_dir(str(work)), ignore_errors=True)


def summary(base, work, calls, adapter="codex"):
    results = {}
    for p in sorted((work / "results").rglob("*")):
        if p.is_file() and not p.name.startswith(".disposal"):
            text = norm_text(base, p.read_text())
            if adapter == "routine":
                text = text.replace("(timeout)", "(TimeoutError)")  # socket.timeout's name before 3.10
            results[str(p.relative_to(work / "results"))] = text
    return {"provider": norm(base, calls), "results": results, "state": read_state(base, work)}


class Broker:
    """The routine's fire endpoint: replies from a script, the last entry repeating."""

    def __init__(self, script):
        broker = self
        self.script, self.calls, self.n = script, [], 0

        class Fire(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                broker.calls.append({"path": self.path, "anthropic-version": self.headers.get("anthropic-version"),
                                     "content-type": self.headers.get("Content-Type"), "body": json.loads(body)})
                status, headers, delay = broker.script[min(broker.n, len(broker.script) - 1)]
                broker.n += 1
                time.sleep(delay)
                raw = json.dumps({"claude_code_session_id": "session_%d" % broker.n,
                                  "claude_code_session_url": "https://claude.ai/code/session_%d" % broker.n}
                                 if status == 200 else {"type": "error", "error": {"type": "x"}}).encode()
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fire)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def routine(work, broker, once, extra=()):
    from ag2_sparrow.adapters.claude_routine import routine_adapter as ra
    with contextlib.redirect_stderr(io.StringIO()):
        return ra.main(["--work", str(work), "--trigger-id", "trig_01DIFF", "--allow-insecure-api-base",
                        "--api-base", broker.base, "--once", str(once), "--poll", "0.01", *extra],
                       {"CLAUDE_ROUTINE_TOKEN": "t"})


OK = (200, {}, 0)


def routine_scenarios(base):
    out = {}

    def run(name, tasks, script, once, extra=(), before=None, again=None):
        work = base / name
        for t in tasks:
            write_task(work, *t)
        if before:
            before(work)
        broker = Broker(script)
        try:
            rc = [routine(work, broker, once, extra)]
            if again:
                again(work)
                rc.append(routine(work, broker, 1, extra))
        finally:
            broker.close()
        out[name] = {"rc": rc, **summary(base, work, broker.calls, "routine")}
        drop_lock(work)

    run("basic", TASKS, [OK], len(TASKS))
    run("restart", TASKS[:1], [OK], 1, again=lambda w: write_task(w, *TASKS[3]))

    def preexisting(work):
        (work / "results").mkdir(parents=True, exist_ok=True)
        (work / "results" / (TASKS[0][0] + ".txt")).write_text("answered elsewhere\n")
    run("pre-existing result", [TASKS[0], TASKS[3]], [OK], 1, before=preexisting)
    run("429 then ok", TASKS[:1], [(429, {"Retry-After": "1"}, 0), OK], 1)
    run("timeout", TASKS[:1], [(200, {}, 3)], 1, extra=("--timeout", "1"))
    run("provider error", TASKS[:1], [(401, {}, 0)], 1)
    work = base / "cap"
    for t in (TASKS[0], TASKS[3]):
        write_task(work, *t)
    (work / "state").mkdir(parents=True)
    (work / "state" / "fires.json").write_text(json.dumps([time.time() - 10]))
    broker = Broker([OK])
    try:
        # The cap holds the next fire for most of an hour: the run is cut off, and nothing may fire.
        child = subprocess.run([sys.executable, __file__, "routine-cap", str(work), broker.base], timeout=60)
    finally:
        broker.close()
    out["cap 1 per hour, full"] = {"rc": [child.returncode], **summary(base, work, broker.calls, "routine")}
    drop_lock(work)
    return out


def codex(work, calls, extra=()):
    from ag2_sparrow.adapters.codex import codex_connector as cc
    log, tee = work.parent / (work.name + ".rpc"), work.parent / (work.name + ".tee.py")
    tee.write_text(TEE)
    stub = os.path.join(os.path.dirname(cc.__file__), "stub_app_server.py")
    cc.server_argv = lambda server, codex="codex": [sys.executable, str(tee), str(log), stub]
    with contextlib.redirect_stderr(io.StringIO()):
        # --allow-non-owner restores the pre-gate serving set, so the recording still compares every
        # served path byte for byte; the gate itself is pinned in test_codex_connector.
        rc = cc.main(["--server", "stub", "--work", str(work), "--poll", "0.01", "--allow-non-owner", *extra])
    if log.exists():
        calls += [{"method": m["method"], "params": m.get("params")}
                  for m in map(json.loads, log.read_text().splitlines()) if m.get("method")]
        log.unlink()
    return rc


def codex_scenarios(base):
    out = {}

    def run(name, tasks, once, extra=(), before=None, again=None):
        work, calls = base / name, []
        for t in tasks:
            write_task(work, *t)
        if before:
            before(work)
        rc = [codex(work, calls, ("--once", str(once), *extra))]
        if again:
            again(work)
            rc.append(codex(work, calls, ("--once", "1", *extra)))
        out[name] = {"rc": rc, **summary(base, work, calls, "codex")}
        drop_lock(work)

    run("basic", TASKS, len(TASKS))
    run("restart", TASKS[:1], 1, again=lambda w: write_task(w, "task-1700000000005", "owner", "!room:ag2.space",
                                                            "a follow-up in the same room", 1005))

    def preexisting(work):
        (work / "results").mkdir(parents=True, exist_ok=True)
        (work / "results" / (TASKS[0][0] + ".txt")).write_text("answered elsewhere\n")
    run("pre-existing result", [TASKS[0], TASKS[3]], 1, before=preexisting)
    run("turn timeout", [("task-1700000000006", "owner", "!room:ag2.space", "[[stall]] slow", 1006)], 1,
        extra=("--turn-timeout", "1", "--interrupt-grace", "2"))
    return out


def main():
    if sys.argv[1] == "routine-cap":
        from ag2_sparrow.adapters.claude_routine import routine_adapter as ra
        threading.Timer(3, lambda: os._exit(0)).start()
        with contextlib.redirect_stderr(io.StringIO()):
            ra.main(["--work", sys.argv[2], "--trigger-id", "trig_01DIFF", "--allow-insecure-api-base",
                     "--api-base", sys.argv[3], "--once", "1", "--poll", "0.01", "--max-per-hour", "1"],
                    {"CLAUDE_ROUTINE_TOKEN": "t"})
        return
    adapter = sys.argv[1]
    # A base of one fixed length on every host: Codex's `chars` counts its own absolute paths.
    base = Path(tempfile.mkdtemp(prefix="sparrow-diff-", dir="/tmp"))
    try:
        out = {"routine": routine_scenarios, "codex": codex_scenarios}[adapter](base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
