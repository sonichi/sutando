#!/usr/bin/env python3
"""Telegram replay/restart journeys through main(), real result delivery and fswatch.

Only urllib's Telegram boundary is synthetic; stdin controls provider polls.
Run: python3 tests/telegram-ingress-identity.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.process_group import popen_bound  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

CONFIG = tempfile.TemporaryDirectory(prefix="ccd-telegram-ingress-")
os.environ["CLAUDE_CONFIG_DIR"] = CONFIG.name
_channel = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_channel.mkdir(parents=True, exist_ok=True)
(_channel / "access.json").write_text(json.dumps({"allowFrom": ["101", "102"]}))
(_channel / ".env").write_text("TELEGRAM_BOT_TOKEN=synthetic-telegram-token\n")


def load_bridge():
    spec = importlib.util.spec_from_file_location("telegram_ingress_test", REPO / "src/telegram-bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


def bridge_child(events, hold_publication):
    def record(method, params):
        with events.open("a") as out:
            out.write(json.dumps({"method": method, "params": params,
                                  "at": datetime.now(timezone.utc).isoformat()}) + "\n")

    def receive():
        if not select.select([sys.stdin], [], [], 25)[0]:
            raise SystemExit("synthetic provider timed out waiting for its controller")
        line = sys.stdin.readline()
        if not line:
            raise SystemExit("synthetic provider controller closed stdin")
        return json.loads(line)

    def urlopen(req, **kwargs):
        prefix = "https://api.telegram.org/botsynthetic-telegram-token/"
        assert req.full_url.startswith(prefix), req.full_url
        method = req.full_url[len(prefix):]
        params = json.loads(req.data) if req.data else {}
        record(method, params)
        if method == "getMe":
            result = {"username": "synthetic_bot", "can_read_all_group_messages": True}
        elif method == "getUpdates":
            result = receive()
        elif method == "sendChatAction":
            if hold_publication:
                receive()
            result = True
        elif method == "sendMessage":
            result = {"message_id": 999}
        else:
            raise AssertionError(f"unexpected provider call: {method}")
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())

    with mock.patch.object(urllib.request, "urlopen", side_effect=urlopen):
        load_bridge().main()


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="telegram-ingress-")
        self.root = Path(self.tmp.name)
        self.env = os.environ.copy()
        supplied = self.env.get("SUTANDO_WORKSPACE") if self.env.get("SUTANDO_TEST_MODE") == "1" else None
        self.ws = Path(supplied) if supplied else self.root / "workspace"
        self.tasks, self.results = self.ws / "tasks", self.ws / "results"
        for path in (self.tasks, self.results, self.ws / "state"):
            path.mkdir(parents=True, exist_ok=True)
        diagnostics = self.ws.parent / "diagnostics"
        self.logs = Path(tempfile.mkdtemp(prefix="telegram-ingress-", dir=diagnostics)) if diagnostics.is_dir() else self.root
        self.env.update({
            "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
            "SUTANDO_WORKSPACE_DIR": str(self.ws), "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
            "SUTANDO_RESULTS_DIR": str(self.results), "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
            "CLAUDE_CONFIG_DIR": CONFIG.name, "TELEGRAM_BOT_TOKEN": "synthetic-telegram-token",
            "SUTANDO_TELEMETRY": "0", "SUTANDO_PROGRESS_STREAM": "0",
            "SUTANDO_STANDBY_STOP_TIMEOUT": "2", "SUTANDO_INSTANCE_ID": "",
            "SUTANDO_WATCHER_BEAT": "", "SUTANDO_WATCHER_TRANSITION_HOOK": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.chat = int(time.time_ns() % 1_000_000_000) + 1_000_000_000
        self.ids = set()
        self.processes = []
        self.witness = []

    def message(self, mid, *, chat=None, sender=101, text="Please sort the synthetic notes."):
        chat = self.chat if chat is None else chat
        self.ids.add(f"task-tg{chat}~{mid}")
        return {"update_id": mid + 1000, "message": {
            "message_id": mid, "from": {"id": sender, "username": "fixture_owner"},
            "chat": {"id": chat, "type": "private" if chat > 0 else "group"}, "text": text,
        }}

    def task_id(self, update):
        msg = update["message"]
        return f"task-tg{msg['chat']['id']}~{msg['message_id']}"

    def start(self, name, argv):
        out, err = self.logs / f"{name}.stdout", self.logs / f"{name}.stderr"
        with out.open("w") as stdout, err.open("w") as stderr:
            proc = popen_bound(argv, cwd=REPO, env=self.env, stdin=subprocess.PIPE,
                                    stdout=stdout, stderr=stderr, text=True)
        self.processes.append(proc)
        self.witness.append({"argv": argv, "pid": proc.pid, "started": datetime.now(timezone.utc).isoformat()})
        return proc

    def bridge(self, name, hold=False):
        events = self.logs / f"{name}.jsonl"
        argv = [sys.executable]
        if self.env.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
            argv += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]
        proc = self.start(name, argv + [str(Path(__file__).resolve()), "--bridge", str(events), str(int(hold))])
        self.wait(lambda: len(self.calls(events, "getUpdates")) == 1, proc)
        return proc, events

    @staticmethod
    def calls(events, method):
        if not events.exists():
            return []
        return [row for line in events.read_text().splitlines(keepends=True)
                if line.endswith("\n") for row in [json.loads(line)] if row["method"] == method]

    def wait(self, predicate, proc, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"process {proc.pid} exited {proc.returncode}; logs: {self.logs}")
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for process {proc.pid}; logs: {self.logs}")

    @staticmethod
    def feed(proc, updates):
        proc.stdin.write(json.dumps(updates) + "\n")
        proc.stdin.flush()

    def tick(self, proc, events, updates=()):
        polls = len(self.calls(events, "getUpdates"))
        self.feed(proc, updates)
        self.wait(lambda: len(self.calls(events, "getUpdates")) == polls + 1, proc)

    def watcher(self):
        proc = self.start("watcher", ["bash", "-x", "src/watch-tasks-stream.sh", "--role", "session", "--inbox", str(self.tasks)])
        trace = self.logs / "watcher.stderr"

        def ready():
            sentinels = list((self.ws / "state").glob("watch-tasks-stream*.pid"))
            sentinel = next((p for p in sentinels if p.read_text().strip() == str(proc.pid)), None)
            return sentinel is not None and "\n+ startup_sweep\n" in trace.read_text()

        self.wait(ready, proc, timeout=25)
        self.witness.append({"watcher_ready_after_standby_wait": True, "pid": proc.pid,
                             "at": datetime.now(timezone.utc).isoformat()})
        return proc

    def announcements(self):
        return [line.removeprefix("TASK_FILE: ") for line in (self.logs / "watcher.stdout").read_text().splitlines()
                if line.startswith("TASK_FILE: ")]

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
                raise AssertionError(f"process {proc.pid} required forced cleanup")
        proc.wait(timeout=1)
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        self.witness.append({"pid": proc.pid, "returncode": proc.returncode, "reaped": True,
                             "stopped": datetime.now(timezone.utc).isoformat()})

    def close(self):
        failures = []
        try:
            for proc in reversed(self.processes):
                try:
                    self.stop(proc)
                except Exception as exc:
                    failures.append(str(exc))
        finally:
            (self.logs / "lifecycle.json").write_text(json.dumps(self.witness, indent=2))
            print(f"  process/readiness evidence: {self.logs}", flush=True)
            for directory in (self.tasks, self.results):
                for tid in self.ids:
                    for path in directory.rglob(f"{tid}.*"):
                        if path.is_file():
                            path.unlink()
            self.tmp.cleanup()
        if failures:
            raise AssertionError("; ".join(failures))


class TelegramIngress(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_restart_replay_watcher_and_late_result(self):
        h = self.h
        update = h.message(401)
        update["message"]["forward_origin"] = {"type": "hidden_user", "sender_user_name": "fixture_author"}
        update["message"]["reply_to_message"] = {
            "message_id": 400, "from": {"username": "fixture_parent"}, "text": "Keep the order."}
        tid = h.task_id(update)
        task = h.tasks / f"{tid}.txt"
        existing_tasks = set(h.tasks.rglob("task-*.txt"))
        watcher = h.watcher()
        first, events1 = h.bridge("first", hold=True)
        h.feed(first, [update])
        h.wait(lambda: bool(h.calls(events1, "sendChatAction")), first)
        self.assertTrue(task.exists())
        self.assertEqual(set(h.tasks.rglob("task-*.txt")), existing_tasks | {task})
        original = task.read_bytes()
        original_mtime = task.stat().st_mtime_ns
        h.wait(lambda: h.announcements().count(task.name) == 1, watcher)
        h.stop(first)
        self.assertEqual(len(h.calls(events1, "getUpdates")), 1)

        second, events2 = h.bridge("second")
        h.tick(second, events2, [update])
        self.assertEqual(task.read_bytes(), original)
        self.assertEqual(task.stat().st_mtime_ns, original_mtime)
        self.assertEqual(list(h.tasks.glob(f"{tid}.*")), [task])
        self.assertEqual(set(h.tasks.rglob("task-*.txt")), existing_tasks | {task})
        self.assertEqual(h.calls(events2, "sendChatAction"), [])
        self.assertEqual(h.calls(events2, "sendMessage"), [])

        result = h.results / task.name
        result_bytes = b"The synthetic notes are sorted.\n"
        result.write_bytes(result_bytes)
        h.tick(second, events2)
        archived_tasks = list((h.tasks / "archive").rglob(task.name))
        archived_results = list((h.results / "archive").rglob(task.name))
        self.assertEqual(len(archived_tasks), 1)
        self.assertEqual(len(archived_results), 1)
        self.assertEqual(archived_tasks[0].read_bytes(), original)
        self.assertEqual(archived_results[0].read_bytes(), result_bytes)
        self.assertFalse(task.exists())
        self.assertFalse(result.exists())
        h.tick(second, events2, [update])
        h.tick(second, events2)
        self.assertEqual([r["params"] for r in h.calls(events2, "sendMessage")],
                         [{"chat_id": h.chat, "text": result_bytes.decode().strip()}])
        self.assertIsNone(watcher.poll())
        self.assertEqual(h.announcements().count(task.name), 1)
        self.assertEqual(archived_tasks[0].read_bytes(), original)
        self.assertFalse(task.exists())
        self.assertEqual(set(h.tasks.rglob("task-*.txt")), existing_tasks | set(archived_tasks))

        for expected in (f"id: {tid}\n", "source: telegram\n", "interaction_type: message\n",
                         f"chat_id: {h.chat}\n", "source_message_id: 401\n", "parent_message_id: 400\n",
                         "priority: normal\n", "Please sort the synthetic notes.",
                         "[forwarded from fixture_author]", "[Replying to @fixture_parent: Keep the order.]",
                         "CONTEXT-FIRST", f"write result to results/{tid}.txt"):
            self.assertIn(expected, original.decode())
        self.assertIn("timestamp: ", original.decode())
        self.assertIn("local_time: ", original.decode())
        h.stop(second)
        h.stop(watcher)

    def test_retained_records_negative_controls_and_legacy_result(self):
        h = self.h
        fixtures, updates = {}, []
        layouts = [h.tasks, h.tasks, h.results, h.tasks / "archive",
                   h.tasks / "processed", h.tasks / "archive/2001-02"]
        for mid, directory in enumerate(layouts, 501):
            update = h.message(mid)
            updates.append(update)
            tid = h.task_id(update)
            suffix = ".claimed-core-1.txt" if mid == 502 else ".txt"
            path = directory / f"{tid}{suffix}"
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(f"id: {tid}\nsource: telegram\nchat_id: {h.chat}\ntask: original retained bytes\n")
            fixtures[path] = (path.read_bytes(), path.stat().st_mtime_ns)

        legacy_id = f"task-{int(time.time() * 1000) - 60000}"
        h.ids.add(legacy_id)
        legacy = h.tasks / f"{legacy_id}.txt"
        legacy.write_text(f"id: {legacy_id}\nsource: telegram\nchat_id: {h.chat}\ntask: legacy request\n")
        legacy_bytes = legacy.read_bytes()
        original_records = set(h.tasks.rglob("task-*.txt"))
        proc, events = h.bridge("retained")
        h.tick(proc, events, updates)
        self.assertEqual(set(h.tasks.rglob("task-*.txt")), original_records)
        self.assertEqual(h.calls(events, "sendChatAction"), [])
        self.assertEqual(h.calls(events, "sendMessage"), [])

        fresh = h.message(601)
        same_text = h.message(602)
        other_chat = h.message(601, chat=-h.chat, sender=102)
        denied = h.message(603, sender=999)
        h.tick(proc, events, updates + [fresh, same_text, other_chat, denied])
        for path, snapshot in fixtures.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)
        for update in updates:
            tid = h.task_id(update)
            expected = [p for p in fixtures if p.parent == h.tasks and p.name.startswith(tid + ".")]
            self.assertEqual(list(h.tasks.glob(f"{tid}.*")), expected)
        for update in (fresh, same_text, other_chat):
            body = (h.tasks / f"{h.task_id(update)}.txt").read_text()
            self.assertIn(f"chat_id: {update['message']['chat']['id']}\n", body)
            self.assertIn(f"source_message_id: {update['message']['message_id']}\n", body)
            self.assertIn("priority: normal\n", body)
            self.assertIn(update["message"]["text"], body)
        self.assertFalse((h.tasks / f"{h.task_id(denied)}.txt").exists())
        self.assertEqual(len(h.calls(events, "sendChatAction")), 3)
        accepted = {h.tasks / f"{h.task_id(update)}.txt" for update in (fresh, same_text, other_chat)}
        self.assertEqual(set(h.tasks.rglob("task-*.txt")), original_records | accepted)

        (h.results / legacy.name).write_text("Legacy answer")
        h.tick(proc, events)
        h.tick(proc, events)
        self.assertEqual([r["params"] for r in h.calls(events, "sendMessage")],
                         [{"chat_id": h.chat, "text": "Legacy answer"}])
        self.assertEqual(next((h.tasks / "archive").rglob(legacy.name)).read_bytes(), legacy_bytes)
        self.assertEqual(next((h.results / "archive").rglob(legacy.name)).read_text(), "Legacy answer")
        self.assertFalse(legacy.exists())
        for path, snapshot in fixtures.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_progress_keeps_provider_and_legacy_task_age_and_tier_gates(self):
        h = self.h
        with mock.patch.dict(os.environ, h.env):
            os.environ["SUTANDO_PROGRESS_STREAM"] = "1"
            bridge = load_bridge()
            for tid, suffix in ((f"task-tg{h.chat}~701", ".txt"),
                                (f"task-tg{h.chat}~702", ".claimed-core-1.txt"),
                                ("task-1980000", ".txt")):
                with self.subTest(task_id=tid):
                    h.ids.add(tid)
                    path = h.tasks / f"{tid}{suffix}"
                    path.write_text(f"id: {tid}\nsource: telegram\ntask: waiting\n")
                    os.utime(path, (1980, 1980))
                    bridge.pending_task_tiers[tid] = "owner"
                    with mock.patch.object(bridge.time, "time", return_value=2000), \
                            mock.patch.object(bridge, "api", return_value={"result": {"message_id": 999}}) as api:
                        bridge.poll_progress({tid: h.chat})
                    self.assertEqual(api.call_args.args[0], "sendMessage")
                    self.assertEqual(bridge._progress_msgs[tid]["first"], 1980)
                    bridge._progress_msgs.clear()
                    bridge.pending_task_tiers.clear()

            tid = f"task-tg{h.chat}~703"
            for tier in (None, "team", "owner"):
                with self.subTest(tier=tier), mock.patch.object(bridge, "api") as api:
                    bridge.pending_task_tiers.clear()
                    if tier is not None:
                        bridge.pending_task_tiers[tid] = tier
                    bridge.poll_progress({tid: h.chat})
                    api.assert_not_called()
            with mock.patch.object(bridge, "find_task_file", return_value=mock.Mock(
                    stat=mock.Mock(side_effect=OSError("task moved")))), mock.patch.object(bridge, "api") as api:
                bridge.poll_progress({tid: h.chat})
                api.assert_not_called()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bridge":
        bridge_child(Path(sys.argv[2]), bool(int(sys.argv[3])))
    else:
        unittest.main(verbosity=2)
