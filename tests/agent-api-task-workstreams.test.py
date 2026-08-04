#!/usr/bin/env python3
"""HTTP coverage for archive history and additive workstream metadata."""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load_agent_api():
    spec = importlib.util.spec_from_file_location("agent_api_workstreams", REPO / "src" / "agent-api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


api = _load_agent_api()
API_SOURCE = (REPO / "src" / "agent-api.py").read_text()


def _write_task(path: Path, task_id: str, text: str, timestamp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"id: {task_id}\n"
        f"timestamp: {timestamp}\n"
        "source: discord\n"
        "access_tier: owner\n"
        f"task: {text}\n"
    )


def test_history_and_active_routes_expose_workstreams() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="agent-api-workstreams-"))
    api.WORKSPACE_DIR = workspace
    api.TASK_DIR = workspace / "tasks"
    api.RESULT_DIR = workspace / "results"
    api.TASK_DIR.mkdir(parents=True)
    api.RESULT_DIR.mkdir(parents=True)
    api.API_TOKEN = "test-secret"
    api.task_history.clear()

    _write_task(
        api.TASK_DIR / "archive" / "2026-08" / "task-old.txt",
        "task-old", "historical task", "2026-08-01T10:00:00Z",
    )
    _write_task(
        api.TASK_DIR / "task-live.txt",
        "task-live", "current task", "2026-08-03T10:00:00Z",
    )
    _write_task(
        api.TASK_DIR / "task-workstream-grouping-maintenance.txt",
        "task-workstream-grouping-maintenance", "internal classifier", "2026-08-03T10:01:00Z",
    )
    (api.RESULT_DIR / "archive" / "2026-08").mkdir(parents=True)
    (api.RESULT_DIR / "archive" / "2026-08" / "task-old.txt").write_text("done\n")
    data_dir = workspace / "data"
    data_dir.mkdir()
    data_dir.joinpath("task-workstreams.json").write_text(json.dumps({
        "schema_version": 1,
        "workstreams": {
            "workstream-sutando": {
                "title": "Sutando task management",
                "summary": "group related task history",
            },
        },
        "assignments": {
            "task-old": {"workstream_id": "workstream-sutando"},
            "task-live": {"workstream_id": "workstream-sutando"},
        },
    }))

    server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
    server.timeout = 0.5
    base = f"http://127.0.0.1:{server.server_address[1]}"
    output: dict = {}
    done = threading.Event()

    def worker() -> None:
        try:
            try:
                urllib.request.urlopen(base + "/tasks/history", timeout=10)
            except urllib.error.HTTPError as exc:
                output["unauthorized"] = {
                    "status": exc.code,
                    "cors": exc.headers.get("Access-Control-Allow-Origin"),
                }
            unauthorized_infer = urllib.request.Request(
                base + "/tasks/workstreams/infer", method="POST",
            )
            try:
                urllib.request.urlopen(unauthorized_infer, timeout=10)
            except urllib.error.HTTPError as exc:
                output["unauthorized_infer"] = exc.code
            auth_headers = {"Authorization": "Bearer test-secret"}
            history_req = urllib.request.Request(base + "/tasks/history", headers=auth_headers)
            response = urllib.request.urlopen(history_req, timeout=10)
            output["history"] = json.loads(response.read().decode())
            response = urllib.request.urlopen(history_req, timeout=10)
            output["history_fallback"] = json.loads(response.read().decode())
            infer_req = urllib.request.Request(
                base + "/tasks/workstreams/infer", method="POST", headers=auth_headers,
            )
            response = urllib.request.urlopen(infer_req, timeout=10)
            output["infer"] = json.loads(response.read().decode())
            try:
                urllib.request.urlopen(infer_req, timeout=10)
            except urllib.error.HTTPError as exc:
                output["infer_failure"] = exc.code
            api.API_TOKEN = ""
            response = urllib.request.urlopen(base + "/tasks/history", timeout=10)
            output["local_history"] = json.loads(response.read().decode())
            hostile_req = urllib.request.Request(
                base + "/tasks/history", headers={"Origin": "https://hostile.example"},
            )
            try:
                urllib.request.urlopen(hostile_req, timeout=10)
            except urllib.error.HTTPError as exc:
                output["hostile"] = {
                    "status": exc.code,
                    "cors": exc.headers.get("Access-Control-Allow-Origin"),
                }
            response = urllib.request.urlopen(base + "/tasks/active", timeout=10)
            output["active"] = json.loads(response.read().decode())
            api.API_TOKEN = "test-secret"
            submit_req = urllib.request.Request(
                base + "/task",
                method="POST",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({
                    "from": "web-reply:task-old",
                    "task": "continue the same workstream",
                }).encode(),
            )
            response = urllib.request.urlopen(submit_req, timeout=10)
            output["submitted"] = json.loads(response.read().decode())
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            output["error"] = repr(exc)
        finally:
            done.set()

    queue_result = api.task_workstreams.ClassifierQueueResult(
        pending=True,
        enqueued=False,
        reason="core-busy",
        snapshot_hash="snapshot-1",
    )

    def run_probe(args, **kwargs):
        if args[0] == "/usr/bin/pgrep":
            return type("Result", (), {"returncode": 0})()
        raise OSError("tmux probe failed")

    thread = threading.Thread(target=worker)
    with mock.patch.object(
             api.task_workstreams, "classifier_status",
             side_effect=[queue_result, RuntimeError("classifier failed"), RuntimeError("classifier failed")],
         ), \
         mock.patch.object(
             api.task_workstreams, "maybe_enqueue_classifier_task",
             side_effect=[queue_result, RuntimeError("classifier failed")],
         ), \
         mock.patch.object(api.shutil, "which", return_value="/usr/bin/tmux"), \
         mock.patch.object(api.subprocess, "run", side_effect=run_probe):
        thread.start()
        while not done.is_set():
            server.handle_request()
        thread.join(timeout=5)
    server.server_close()

    assert "error" not in output, output.get("error")
    assert output["unauthorized"] == {"status": 401, "cors": None}
    assert output["unauthorized_infer"] == 401
    assert output["hostile"] == {"status": 403, "cors": None}
    history = output["history"]
    assert [row["id"] for row in history["tasks"]] == ["task-live", "task-old"]
    assert all(row["workstream_name"] == "Sutando task management" for row in history["tasks"])
    assert history["inference"] == {
        "pending": True,
        "enqueued": False,
        "reason": "core-busy",
        "snapshot_hash": "snapshot-1",
    }
    assert output["history_fallback"]["inference"]["reason"] == "classifier-unavailable"
    assert output["local_history"]["inference"]["reason"] == "classifier-unavailable"
    assert output["infer"]["reason"] == "core-busy"
    assert output["infer_failure"] == 503

    active = output["active"]["tasks"]
    assert [row["id"] for row in active] == ["task-live"]
    assert active[0]["workstream_id"] == "workstream-sutando"
    submitted_id = output["submitted"]["task_id"]
    assignment = api.task_workstreams.load_workstream_store(workspace)["assignments"][submitted_id]
    assert assignment["workstream_id"] == "workstream-sutando"

    # Exercise the malformed-client-address denial branch without a real socket.
    api.API_TOKEN = ""
    fake_handler = api.Handler.__new__(api.Handler)
    fake_handler.client_address = ("not-an-ip", 0)
    fake_handler.headers = {}
    denied = []
    fake_handler.send_private_json = lambda status, body: denied.append((status, body))
    assert not fake_handler.check_private_history_auth()
    assert denied[0][0] == 403


def test_agent_api_starts_always_on_workstream_maintenance() -> None:
    main = API_SOURCE[API_SOURCE.index('if __name__ == "__main__":'):]
    assert "target=task_workstreams.run_classifier_maintenance" in main
    assert 'name="task-workstream-maintenance"' in main
    assert "workstream_maintenance.start()" in main
    assert "workstream_maintenance_stop.set()" in main
    assert "workstream_maintenance.join(timeout=1)" in main


if __name__ == "__main__":
    test_history_and_active_routes_expose_workstreams()
    test_agent_api_starts_always_on_workstream_maintenance()
    print("agent-api task workstream tests passed")
