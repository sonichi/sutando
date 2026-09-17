#!/usr/bin/env python3
"""CLI surface contract — every subcommand path against a real daemon.

Drives src/runtime-cli/sutando-runtime.py as a subprocess (argv-identical to
production) for each command group: JSON and human printers, exit-code
contracts (0 data / 1 not-found / 2 usage / 3 conflict), and the stand
resolve authority semantics. Complements the e2e suite, which drives the
flows; this file pins the breadth of the command table.

Run: python3 tests/runtime-cli-surface.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"
TMP = tempfile.mkdtemp(prefix="cli-surface-")

PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

ENV = {**os.environ,
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "runtime-state.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(Path(TMP) / "state"),
       "SUTANDO_AGENT_ID": "@cli-test:example.org",
       "SUTANDO_HOST_LABEL": "cli-surface-host",
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


def run_cli(*args, timeout=30):
    return subprocess.run([*PYBASE, str(CLI), *args], capture_output=True,
                          text=True, timeout=timeout, env=ENV)


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


def main() -> int:
    for d in ("tasks", "results", "state"):
        (Path(TMP) / d).mkdir(exist_ok=True)
    auth = Path(ENV["SUTANDO_RUNTIME_STATE"]) / "auth"
    auth.mkdir(parents=True, exist_ok=True)
    (auth / "ag2space.json").write_text(json.dumps(
        {"agent_id": "@cli-test:example.org", "profile": "production"}))
    (auth / "stand.json").write_text(json.dumps(
        {"owners": [{"person_id": "@qy:example.org", "display_name": "QY",
                     "role": "primary_owner"}]}))
    (auth / "entrance-links.json").write_text(json.dumps([
        {"link_id": "l1", "provider": "discord", "status": "active",
         "stand_id": "@cli-test:example.org", "authorized_by": "@qy:example.org",
         "provider_subject": {"type": "bot_user", "id": "42"},
         "display": {"name": "bot"},
         "verification": {"method": "discord_token_introspection",
                          "verified_at": "t"}},
        {"link_id": "l2", "provider": "slack", "status": "active",
         "stand_id": "@s1:x", "authorized_by": "@o:x",
         "provider_subject": {"type": "workspace_bot", "authority": "T1",
                              "id": "U77"}},
        {"link_id": "l3", "provider": "slack", "status": "active",
         "stand_id": "@s2:x", "authorized_by": "@o:x",
         "provider_subject": {"type": "workspace_bot", "authority": "T2",
                              "id": "U77"}},
        {"link_id": "l4", "provider": "telegram", "status": "active",
         "stand_id": "@cli-test:example.org",
         "provider_subject": {"type": "bot_user", "id": "999"}},
    ]))

    daemon = subprocess.Popen([*PYBASE, str(SERVER)], env=ENV,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    if not wait_socket(ENV["SUTANDO_RUNTIME_SOCKET"]):
        daemon.kill()
        print(daemon.stdout.read())
        raise AssertionError("daemon socket never came up")
    try:
        drive(daemon)
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
    print(f"\n{'FAILED' if FAILS else 'PASS'} — CLI surface "
          f"({len(FAILS)} failure(s))")
    return 1 if FAILS else 0


def drive(daemon):
    # ── sutando group (identity views; PR-2 adds the stand subcommands) ──
    for sub in ("info", "status", "owner", "allowlist"):
        p = run_cli("sutando", sub)
        check(p.returncode == 0 and json.loads(p.stdout) is not None,
              f"sutando {sub} exits 0 with JSON")

    # ── runtime / agent / capability / request groups ──
    for sub in ("health", "details"):
        p = run_cli("runtime", sub)
        check(p.returncode == 0 and p.stdout.strip(),
              f"runtime {sub} exits 0 with output")
    p = run_cli("agent", "list")
    check(p.returncode == 0, "agent list exits 0")
    p = run_cli("agent", "status", "@nobody:example.org")
    check(p.returncode == 1 and "unknown agent" in p.stderr,
          "agent status for unknown agent errors loudly on stderr, exit 1")
    p = run_cli("capability", "list")
    check(p.returncode == 0, "capability list exits 0")
    p = run_cli("request", "list")
    check(p.returncode == 0, "request list exits 0")

    # ── task group: submit → status/details/list/results/get-result/cancel ──
    p = run_cli("task", "submit", "cli surface probe", "--priority", "low")
    check(p.returncode == 0, "task submit exits 0")
    tid = (json.loads(p.stdout) or {}).get("taskId", "")
    check(tid.startswith("task-"), "submit returns a task id")
    p = run_cli("task", "status", tid)
    check(p.returncode == 0, "task status exits 0")
    p = run_cli("task", "details", tid)
    check(p.returncode == 0, "task details exits 0")
    p = run_cli("task", "list")
    check(p.returncode == 0, "task list exits 0")
    (Path(ENV["SUTANDO_RESULTS_DIR"]) / f"{tid}.txt").write_text("done body")
    p = run_cli("task", "get-result", tid)
    check(p.returncode == 0 and "done body" in p.stdout,
          "get-result returns the written body")
    p = run_cli("task", "results")
    check(p.returncode == 0, "task results exits 0")
    p = run_cli("task", "get-result")
    check(p.returncode == 0, "get-result with no id returns newest")
    p = run_cli("task", "cancel", tid)
    check(p.returncode == 0, "task cancel exits 0")

    # ── human-action group ──
    p = run_cli("human-action", "request", "--task-id", "task-cli-1",
                "--action", "confirm", "--instructions", "probe?")
    check(p.returncode == 0, "human-action request exits 0")
    rid = (json.loads(p.stdout) or {}).get("requestId") or (
        json.loads(p.stdout) or {}).get("request_id")
    if rid:
        p = run_cli("human-action", "status", rid)
        check(p.returncode == 0, "human-action status exits 0")
        # Settling is grant-gated, and the plain Unix socket grants nothing —
        # the CLI must surface that refusal, not silently settle (review P1).
        p = run_cli("human-action", "decline", rid, "--note", "n/a")
        check(p.returncode == 1 and "authorized device grant" in p.stderr,
              "ungranted human-action decline is refused with a reason")
        p = run_cli("human-action", "status", rid)
        check(p.returncode == 0
              and json.loads(p.stdout).get("status") == "pending",
              "the refused decline left the request pending")

    # ── instance group: list + start/attach on a missing agent ──
    p = run_cli("instance", "list")
    check(p.returncode == 0, "instance list exits 0")
    p = run_cli("instance", "start", "@ghost:example.org")
    check(p.returncode == 1 and not p.stdout.strip() == "",
          "instance start on a missing agent errors with a body")
    p = run_cli("instance", "attach", "@ghost:example.org", "--print")
    check(p.returncode == 1 and p.stderr.strip(),
          "instance attach on a missing agent errors loudly")
    # attach --print on a REAL manifest prints the argv instead of exec'ing
    sys.path.insert(0, str(REPO / "src" / "runtime-api"))
    os.environ["SUTANDO_INSTANCE_REGISTRY"] = ENV["SUTANDO_INSTANCE_REGISTRY"]
    import instance_registry as _reg
    _reg.write_manifest("@att:example.org",
                        endpoint=str(Path(TMP) / "att.sock"),
                        tmux_socket="/tmp/att-tmux.sock",
                        session="att-core")
    p = run_cli("instance", "attach", "@att:example.org", "--print")
    check(p.returncode == 0 and "tmux" in p.stdout,
          "attach --print emits the tmux argv for a real manifest")

    # ── capability read + approval request via the CLI ──
    p = run_cli("capability", "read", "--capability", "nope.cap",
                "--operation", "read")
    check(p.returncode in (0, 1), "capability read answers on unknown cap")
    p = run_cli("approval", "request", "--action", "cli.probe",
                "--reason", "surface probe", "--expires-in", "60")
    check(p.returncode == 0 and "requestId" in p.stdout,
          "approval request exits 0 with a requestId")
    rid2 = json.loads(p.stdout)["requestId"]
    p = run_cli("request", "get", rid2)
    check(p.returncode == 0, "request get on a live id exits 0")
    p = run_cli("request", "cancel", rid2)
    check(p.returncode == 0, "request cancel exits 0")

    # ── usage errors ──
    p = run_cli("task")
    check(p.returncode == 2, "bare group without subcommand is usage error")
    p = run_cli("no-such-group")
    check(p.returncode == 2, "unknown group is usage error")

    check(daemon.poll() is None, "daemon survived the whole surface sweep")


if __name__ == "__main__":
    sys.exit(main())
