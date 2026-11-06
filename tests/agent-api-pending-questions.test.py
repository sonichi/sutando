#!/usr/bin/env python3
"""agent-api.py's pending-questions surface reads and resolves ONLY through
src/pending_questions_reader.py: rows are the reader's items in the triage shape,
dismissal and ranking still apply, and POST /answer closes through the reader's
`resolve` and writes the agent's answer task file. No file is read.

Run: python3 tests/agent-api-pending-questions.test.py
"""
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
NOW = time.time()


def _item(ask_id, title, asked_at, snippet="", in_room=True):
    return {"id": title[:40], "ask_id": ask_id, "title": title, "snippet": snippet, "body": snippet,
            "asked_at": asked_at, "priority": "medium", "in_room": in_room}


ITEMS = [
    _item("ask-alpha", "ALPHA, oldest and blocking nothing", NOW - 40 * 86400, "Prose only."),
    _item("ask-bravo", "BRAVO, blocked on sonichi/sutando#4242", NOW - 2 * 86400),
    _item("ask-held", "HELD, not yet in the room", None, in_room=False),
]


def _g(items, unavailable=False, reason=None):
    """The reader's gather shape."""
    return {"waiting": items, "done": None if unavailable else 0, "unavailable": unavailable, "reason": reason,
            "link": None, "notes": [], "store": None}


class Rows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentapi-pq-"))
        self.saved = api.WORKSPACE_DIR
        api.WORKSPACE_DIR = self.tmp
        os.environ.pop("SUTANDO_MEMORY_DIR", None)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)

    def tearDown(self):
        api.WORKSPACE_DIR = self.saved

    def test_rows_come_from_the_reader_in_the_triage_shape(self):
        with mock.patch.object(api.pending_questions_reader, "gather", return_value=_g(ITEMS)) as w:
            rows = api._pending_question_rows()
        w.assert_called_once_with(self.tmp, api.skill_roots.declared(api.pending_questions_reader.DECLARATION, self.tmp))
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(set(by_id), {"ask-alpha", "ask-bravo", "ask-held"})
        self.assertEqual(by_id["ask-alpha"]["text"], "ALPHA, oldest and blocking nothing")
        self.assertEqual(by_id["ask-alpha"]["detail"], "Prose only.")
        self.assertEqual(by_id["ask-bravo"]["detail"], by_id["ask-bravo"]["text"])
        self.assertEqual(by_id["ask-alpha"]["age_days"], 40)
        self.assertRegex(by_id["ask-alpha"]["asked"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIsNone(by_id["ask-held"]["asked"])
        self.assertIsNone(by_id["ask-held"]["age_days"])
        self.assertFalse(by_id["ask-held"]["in_room"])

    def test_rows_are_oldest_first_undated_sorts_last(self):
        with mock.patch.object(api.pending_questions_reader, "gather", return_value=_g(ITEMS)):
            rows = api._pending_question_rows()
        self.assertEqual([r["id"] for r in rows], ["ask-alpha", "ask-bravo", "ask-held"])

    def test_dismissed_rows_are_removed(self):
        api.pq_triage.dismiss(api._dismissed_questions_path(), "ask-alpha")
        with mock.patch.object(api.pending_questions_reader, "gather", return_value=_g(ITEMS)):
            rows = api._pending_question_rows()
        self.assertEqual([r["id"] for r in rows], ["ask-bravo", "ask-held"])

    def test_recheck_labels_a_merged_blocker_stale_and_keeps_it(self):
        with mock.patch.object(api.pending_questions_reader, "gather", return_value=_g(ITEMS)), \
                mock.patch.object(api, "_probe_ref_states", return_value={("sonichi/sutando", 4242): "MERGED"}):
            rows = api._pending_question_rows(recheck=True)
        stale = [r for r in rows if r.get("recheck")]
        self.assertEqual([r["id"] for r in stale], ["ask-bravo"])
        self.assertEqual(stale[0]["recheck"]["status"], "stale")

    def test_the_module_reads_no_file_for_questions(self):
        src = (REPO / "src" / "agent-api.py").read_text()
        for needle in ("parse_pending_questions", "answer_pending_question", "pending_questions_md",
                       "pending_questions_ledger", 'personal_path("pending-questions.md"'):
            self.assertNotIn(needle, src, needle)
        self.assertIn("pending_questions_reader.gather(", src)
        self.assertIn("pending_questions_reader.resolve(", src)
        self.assertNotIn("pending_questions_reader.waiting(", src, "waiting() hides `unavailable`; the payload needs it")

    def test_an_unreachable_room_is_flagged_in_the_payload_not_an_empty_queue(self):
        held = [i for i in ITEMS if not i["in_room"]]
        with mock.patch.object(api.pending_questions_reader, "gather",
                               return_value=_g(held, unavailable=True, reason="room down")):
            payload = api._active_tasks_payload(True, True)
            queue = api._questions_queue_payload()
        self.assertEqual(payload["questions_unavailable"], "room down")
        self.assertEqual([q["id"] for q in payload["questions"]], ["ask-held"])
        self.assertEqual(queue["questions_unavailable"], "room down")
        with mock.patch.object(api.pending_questions_reader, "gather", return_value=_g(ITEMS)):
            self.assertIsNone(api._active_tasks_payload(True, True)["questions_unavailable"])


def _floor_interpreter():
    """This host's 3.9 floor interpreter, or None; by name or $SUTANDO_PY39, never a literal path."""
    cand = os.environ.get("SUTANDO_PY39") or shutil.which("python3.9")
    if not cand or not Path(cand).exists():
        return None
    ver = subprocess.run([cand, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                         capture_output=True, text=True).stdout.strip()
    return Path(cand) if ver == "3.9" else None


class Python39(unittest.TestCase):
    """agent-api.py has no `from __future__ import annotations`: a PEP 604 union breaks 3.9."""

    def test_the_module_carries_no_39_breaking_annotations(self):
        lint = _load("py39_union_lint", REPO / "tests" / "python39-union-annotations.test.py")
        self.assertIsNone(lint.check_file(Path(api.__file__)))

    def test_the_real_module_imports_on_the_floor_interpreter(self):
        py39 = _floor_interpreter()
        if py39 is None:
            self.skipTest("no python3.9 on PATH and $SUTANDO_PY39 unset")
        prog = ("import importlib.util, sys\n"
                f"sys.path.insert(0, {str(REPO / 'src')!r})\n"
                f"spec = importlib.util.spec_from_file_location('agent_api_39', {str(REPO / 'src' / 'agent-api.py')!r})\n"
                "m = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(m)\n"
                "print(m._question_row({'ask_id': 'a', 'title': 't', 'asked_at': None})['id'])\n")
        r = subprocess.run([str(py39), "-c", prog], capture_output=True, text=True)
        self.assertEqual(0, r.returncode, f"3.9 rejected the module: {r.stderr[-300:]}")
        self.assertEqual("a", r.stdout.strip())


class AnswerRoute(unittest.TestCase):
    """POST /answer over a real server: closes through the reader and files the task."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agentapi-answer-"))
        (cls.tmp / "tasks").mkdir()
        cls.saved = (api.WORKSPACE_DIR, api.TASK_DIR, api.API_TOKEN)
        api.WORKSPACE_DIR, api.TASK_DIR, api.API_TOKEN = cls.tmp, cls.tmp / "tasks", "test-token-123"
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
        cls.server.timeout = 0.5
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()
        api.WORKSPACE_DIR, api.TASK_DIR, api.API_TOKEN = cls.saved

    def _raw(self, method, path, body=None):
        r = urllib.request.Request(f"{self.base}{path}", method=method,
                                   data=None if body is None else json.dumps(body).encode())
        r.add_header("Authorization", "Bearer test-token-123")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def req(self, method, path, body=None):
        out = {}
        t = threading.Thread(target=lambda: out.update(zip(("code", "data"), self._raw(method, path, body))),
                             daemon=True)
        t.start()
        while t.is_alive():
            self.server.handle_request()
        t.join()
        return out["code"], out["data"]

    def test_listing_answer_and_404s(self):
        closed = []

        def _resolve(ws, ask_id, status, store=None):
            if ask_id in [c[0] for c in closed] or ask_id not in {i["ask_id"] for i in ITEMS}:
                return False, f"no open row for {ask_id}"
            closed.append((ask_id, status))
            return True, "closed"

        def _gather(ws, store=None):
            return _g([i for i in ITEMS if i["ask_id"] not in [c[0] for c in closed]])

        with mock.patch.object(api.pending_questions_reader, "gather", _gather), \
                mock.patch.object(api.pending_questions_reader, "resolve", _resolve):
            code, data = self.req("GET", "/tasks/active")
            self.assertEqual(code, 200)
            ids = [q["id"] for q in data["questions"]]
            self.assertEqual(ids, ["ask-alpha", "ask-bravo", "ask-held"])
            code, data = self.req("POST", "/answer", {"id": "ask-alpha", "answer": "drop it"})
            self.assertEqual(code, 200, data)
            self.assertEqual(closed, [("ask-alpha", "Answered")])
            tasks = list((self.tmp / "tasks").glob("answer-ask-alpha-*.txt"))
            self.assertEqual(len(tasks), 1)
            self.assertIn("drop it", tasks[0].read_text())
            code, data = self.req("GET", "/tasks/active")
            self.assertNotIn("ask-alpha", [q["id"] for q in data["questions"]])
            code, data = self.req("POST", "/answer", {"id": "ask-alpha", "answer": "again"})
            self.assertEqual(code, 404)
            self.assertIn("is not waiting", data["error"])
            code, _ = self.req("POST", "/answer", {"id": "Q1", "answer": "stale"})
            self.assertEqual(code, 404)
            code, _ = self.req("POST", "/answer", {"id": "ask-bravo"})
            self.assertEqual(code, 400)
            self.assertEqual(len(list((self.tmp / "tasks").glob("answer-*.txt"))), 1)

    def test_an_answer_during_an_outage_is_kept_and_a_local_close_counts_as_closed(self):
        """The owner's typed answer never drops: a local close record (resolve True) is a 200
        with the task written; only UNRECORDED is a failure, and even then the task is written."""
        for why, code in (("recorded locally as Answered in x (room unreachable)", 200),
                          ("UNRECORDED: room unreachable; and the local close record failed", 503)):
            with self.subTest(why=why):
                for f in (self.tmp / "tasks").glob("answer-*.txt"):
                    f.unlink()
                with mock.patch.object(api.pending_questions_reader, "gather", return_value=_g(ITEMS)), \
                        mock.patch.object(api.pending_questions_reader, "resolve",
                                          return_value=(code == 200, why)):
                    got, data = self.req("POST", "/answer", {"id": "ask-bravo", "answer": "ship it"})
                self.assertEqual(got, code, data)
                [task] = list((self.tmp / "tasks").glob("answer-ask-bravo-*.txt"))
                self.assertIn("ship it", task.read_text())
                if code == 503:
                    self.assertIn("could not be closed", data["error"])
                    self.assertTrue(data["recorded"])
                task.unlink()  # the class shares one tasks/ dir


if __name__ == "__main__":
    unittest.main()
