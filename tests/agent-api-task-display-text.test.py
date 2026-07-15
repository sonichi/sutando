#!/usr/bin/env python3
"""Regression test for Conversation task rows preserving original task text.

When /tasks/active rebuilds history from a result file after the task file has
already been archived, the row title must remain the original `task:` body, not
the first line of the result.

Also exercises the HTTP dispatch paths (`GET /tasks/active`, `POST /task-done`)
so the diff-coverage gate sees the handler branches. Per the delegation test's
note, the coverage tracer misses handler-THREAD execution — so the server runs
on the MAIN thread while requests are issued from a worker thread.
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

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


sys.path.insert(0, str(REPO / "src"))
api = _load("agent_api", REPO / "src" / "agent-api.py")


def test_result_only_history_uses_archived_task_text():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.task_history.clear()

        archive = api.TASK_DIR / "archive"
        archive.mkdir(parents=True)
        api.RESULT_DIR.mkdir(parents=True)

        (archive / "task-123.txt").write_text(
            "id: task-123\n"
            "timestamp: 2026-07-08T00:00:00Z\n"
            "source: api\n"
            "from: web\n"
            "task: original user request\n"
        )
        result_file = api.RESULT_DIR / "task-123.txt"
        result_file.write_text("Done - result summary\n\nDetails follow.\n")

        api._remember_done_result_file(result_file)

        row = api.task_history["task-123"]
        assert row["status"] == "done"
        assert row["text"] == "original user request"
        assert row["result"].startswith("Done - result summary")
        assert row["source"] == "api"


def test_result_only_history_falls_back_without_task_file():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.task_history.clear()
        api.TASK_DIR.mkdir(parents=True)
        api.RESULT_DIR.mkdir(parents=True)

        result_file = api.RESULT_DIR / "task-456.txt"
        result_file.write_text("Done - result summary\n\nDetails follow.\n")

        api._remember_done_result_file(result_file)

        assert api.task_history["task-456"]["text"] == "Done - result summary"


def test_display_fields_for_id_swallows_oserror():
    """A found archive path that can't be read (e.g. it's a directory) returns
    empty strings instead of raising."""
    orig = api.local_task_protocol.find_archived_task
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.TASK_DIR.mkdir(parents=True)
        bad = api.TASK_DIR / "task-oserr.txt"
        bad.mkdir()  # read_text() on a dir raises IsADirectoryError (OSError)
        api.local_task_protocol.find_archived_task = lambda d, tid: bad
        try:
            assert api._task_display_fields_for_id("task-oserr") == ("", "")
        finally:
            api.local_task_protocol.find_archived_task = orig


def test_remember_updates_existing_non_done_entry():
    """An existing WORKING row (no text/source yet) gets promoted to done and
    backfilled from the archived task file."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.task_history.clear()
        archive = api.TASK_DIR / "archive"
        archive.mkdir(parents=True)
        api.RESULT_DIR.mkdir(parents=True)

        (archive / "task-778.txt").write_text("source: voice\ntask: the original ask\n")
        api.task_history["task-778"] = {"status": "working", "text": "", "source": ""}
        result_file = api.RESULT_DIR / "task-778.txt"
        result_file.write_text("Done - summarized\n")

        api._remember_done_result_file(result_file)

        row = api.task_history["task-778"]
        assert row["status"] == "done"
        assert row["result"].startswith("Done - summarized")
        assert row["text"] == "the original ask"   # backfilled from archive
        assert row["source"] == "voice"             # backfilled from archive


def test_remember_repairs_done_fallback_on_later_poll():
    """Race: a done row created from a result file before the task was archived
    carries the fallback summary. Once the task file becomes readable on a later
    poll, the row must be repaired to the real `task:` text/source rather than
    caching the fallback until restart (#2034 review, qingyun-wu)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api.TASK_DIR = root / "tasks"
        api.RESULT_DIR = root / "results"
        api.task_history.clear()
        api.TASK_DIR.mkdir(parents=True)
        api.RESULT_DIR.mkdir(parents=True)

        result_file = api.RESULT_DIR / "task-999.txt"
        result_file.write_text("Done - fallback summary\n\nbody\n")

        # Poll 1: task not yet archived → fallback summary used.
        api._remember_done_result_file(result_file)
        assert api.task_history["task-999"]["status"] == "done"
        assert api.task_history["task-999"]["text"] == "Done - fallback summary"

        # Task gets archived after the first poll.
        archive = api.TASK_DIR / "archive"
        archive.mkdir(parents=True)
        (archive / "task-999.txt").write_text(
            "source: voice\ntask: the REAL user request\n"
        )

        # Poll 2: repair even though the row is already "done".
        api._remember_done_result_file(result_file)
        row = api.task_history["task-999"]
        assert row["text"] == "the REAL user request", row
        assert row["source"] == "voice", row


def test_http_dispatch_paths():
    """Drive the real handler so the coverage gate sees the /tasks/active disk
    scan and the /task-done unknown-tid branch. Server on the MAIN thread;
    requests from a worker thread (coverage tracer misses handler threads)."""
    tmp = Path(tempfile.mkdtemp(prefix="agentapi-taskdisp-http-"))
    api.TASK_DIR = tmp / "tasks"
    api.RESULT_DIR = tmp / "results"
    (api.TASK_DIR / "archive").mkdir(parents=True)
    api.RESULT_DIR.mkdir(parents=True)
    api.WORKSPACE_DIR = tmp        # hermetic pending-questions lookup (no file)
    api.API_TOKEN = ""             # /task-done check_auth passes without a token
    api.task_history.clear()

    # (a) live task file → GET /tasks/active disk scan preserves its `task:` text
    (api.TASK_DIR / "task-http-1.txt").write_text("source: discord\ntask: live task title\n")
    # (b) archived task for an unknown tid → POST /task-done pulls original text
    (api.TASK_DIR / "archive" / "task-http-2.txt").write_text(
        "source: telegram\ntask: archived task title\n"
    )

    server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
    server.timeout = 0.5
    base = f"http://127.0.0.1:{server.server_address[1]}"
    out: dict = {}
    done = threading.Event()

    def worker():
        try:
            r = urllib.request.urlopen(f"{base}/tasks/active", timeout=10)
            out["get"] = (r.status, json.loads(r.read().decode()))
            req = urllib.request.Request(
                f"{base}/task-done", method="POST",
                data=json.dumps({"taskId": "task-http-2", "result": "done body"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            r2 = urllib.request.urlopen(req, timeout=10)
            out["post"] = (r2.status, json.loads(r2.read().decode()))
        except Exception as e:  # pragma: no cover - surfaced via assert below
            out["error"] = repr(e)
        finally:
            done.set()

    t = threading.Thread(target=worker)
    t.start()
    while not done.is_set():
        server.handle_request()
    t.join(timeout=5)
    server.server_close()

    assert "error" not in out, out.get("error")
    assert out["get"][0] == 200, out["get"]
    titles = [row.get("text") for row in out["get"][1]["tasks"]]
    assert "live task title" in titles, titles
    assert out["post"][0] == 200, out["post"]
    assert api.task_history["task-http-2"]["text"] == "archived task title"
    assert api.task_history["task-http-2"]["source"] == "telegram"


if __name__ == "__main__":
    test_result_only_history_uses_archived_task_text()
    test_result_only_history_falls_back_without_task_file()
    test_display_fields_for_id_swallows_oserror()
    test_remember_updates_existing_non_done_entry()
    test_remember_repairs_done_fallback_on_later_poll()
    test_http_dispatch_paths()
    print("agent-api task display text tests passed.")
