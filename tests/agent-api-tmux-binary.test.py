#!/usr/bin/env python3
"""GET /tasks/active probes the core with tmux_probe.tmux_binary(), not its own lookup.

A launchd/desktop process without Homebrew on PATH must still find tmux, and the
candidate list lives in one place. Server on the MAIN thread, requests from a
worker thread (the coverage tracer misses handler threads).
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


sys.path.insert(0, str(REPO / "src"))
api = _load("agent_api", REPO / "src" / "agent-api.py")


def _get_active(resolved: str, tmux_rc: int):
    tmp = Path(tempfile.mkdtemp(prefix="agentapi-tmuxbin-"))
    api.TASK_DIR = tmp / "tasks"
    api.RESULT_DIR = tmp / "results"
    (api.TASK_DIR / "archive").mkdir(parents=True)
    api.RESULT_DIR.mkdir(parents=True)
    api.WORKSPACE_DIR = tmp
    api.API_TOKEN = ""
    api.task_history.clear()

    server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
    server.timeout = 0.5
    base = f"http://127.0.0.1:{server.server_address[1]}"
    out: dict = {}
    tmux_argv: list = []
    done = threading.Event()

    def worker():
        try:
            r = urllib.request.urlopen(f"{base}/tasks/active", timeout=10)
            out["get"] = (r.status, json.loads(r.read().decode()))
        except Exception as e:  # pragma: no cover - surfaced via assert below
            out["error"] = repr(e)
        finally:
            done.set()

    def run_probe(args, **kwargs):
        if args[0] == "/usr/bin/pgrep":
            return type("Result", (), {"returncode": 0})()
        if "has-session" not in args:
            raise OSError("unrelated probe")
        tmux_argv.append(list(args))
        return type("Result", (), {"returncode": tmux_rc})()

    t = threading.Thread(target=worker)
    with mock.patch.object(api.tmux_probe, "tmux_binary", return_value=resolved), \
         mock.patch.object(api.shutil, "which", return_value=None), \
         mock.patch.object(api.subprocess, "run", side_effect=run_probe):
        t.start()
        while not done.is_set():
            server.handle_request()
        t.join(timeout=5)
    server.server_close()
    assert "error" not in out, out.get("error")
    assert out["get"][0] == 200, out["get"]
    return out["get"][1], tmux_argv


def test_active_tasks_probe_uses_shared_resolver():
    payload, argv = _get_active("/fake/candidate/tmux", 0)
    assert argv and argv[0][0] == "/fake/candidate/tmux", argv
    assert argv[0][-2:] == ["-t", "=sutando-core"], argv
    assert payload["claude"] is True, payload


def test_active_tasks_absent_session_reads_not_running():
    payload, argv = _get_active("/fake/candidate/tmux", 1)
    assert argv[0][0] == "/fake/candidate/tmux", argv
    assert payload["claude"] is False, payload


if __name__ == "__main__":
    test_active_tasks_probe_uses_shared_resolver()
    test_active_tasks_absent_session_reads_not_running()
    print("agent-api tmux binary tests passed.")
