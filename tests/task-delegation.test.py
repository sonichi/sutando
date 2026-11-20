#!/usr/bin/env python3
"""src/task_delegation.py: the background-delegation marker's writer and reader.

Run: python3 tests/task-delegation.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import task_delegation as m  # noqa: E402

TID = "task-cron-deal-sweep-1792841385408"


class Marker(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        self.task = self.task_file(TID)

    def task_file(self, tid, ago=3600.0):
        t = self.ws / "tasks" / f"{tid}.txt"
        t.parent.mkdir(exist_ok=True)
        t.write_text(f"id: {tid}\ntask: x\n")
        os.utime(t, (time.time() - ago,) * 2)
        return t

    def tearDown(self):
        self.td.cleanup()

    def test_mark_then_live_and_no_temp_left(self):
        p = m.mark(self.ws, TID, 600)
        self.assertTrue(m.is_delegated(self.ws, TID))
        d = json.loads(p.read_text())
        self.assertEqual((d["task_id"], d["task_file"], d["ttl_s"]), (TID, f"tasks/{TID}.txt", 600.0))
        self.assertEqual([x.name for x in p.parent.iterdir()], [p.name])

    def test_expired_is_not_delegated(self):
        m.mark(self.ws, TID, 60, now=time.time() - 61)
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_expiry_boundary_age_equal_to_ttl_is_expired(self):
        at = time.time() - 120
        m.mark(self.ws, TID, 60, now=at)
        self.assertTrue(m.is_delegated(self.ws, TID, now=at + 59.999))
        self.assertFalse(m.is_delegated(self.ws, TID, now=at + 60))

    def test_a_wake_turn_rewritten_after_the_marker_is_not_delegated(self):
        m.mark(self.ws, TID, 1800)
        self.assertTrue(m.is_delegated(self.ws, TID))
        later = time.time() + 1
        os.utime(self.task, (later, later))
        self.assertFalse(m.is_delegated(self.ws, TID))
        self.assertIsNone(m.holder(self.ws, TID))

    def test_the_task_file_given_is_the_one_compared(self):
        m.mark(self.ws, TID, 1800)
        other = self.ws / "inbox" / f"{TID}.txt"
        other.parent.mkdir()
        other.write_text("x")
        os.utime(other, (time.time() + 1,) * 2)
        self.assertTrue(m.is_delegated(self.ws, TID))
        self.assertFalse(m.is_delegated(self.ws, TID, task_file=other))

    def test_no_task_file_is_not_delegated(self):
        m.mark(self.ws, TID, 1800)
        self.task.unlink()
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_huge_integers_fail_closed_without_overflow(self):
        p = m.marker_path(self.ws, TID)
        p.parent.mkdir(parents=True)
        now = time.time()
        for at, ttl in ((now, 10 ** 400), (10 ** 400, 600)):
            p.write_text(json.dumps({"task_id": TID, "delegated_at": at, "ttl_s": ttl}))
            self.assertFalse(m.is_delegated(self.ws, TID))
        with self.assertRaises(ValueError):
            m.mark(self.ws, TID, 10 ** 400)
        self.assertEqual(self._main("mark", TID, "--ttl", "1" + "0" * 400)[0], 2)

    def test_future_dated_is_not_delegated(self):
        m.mark(self.ws, TID, 600, now=time.time() + 3600)
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_absent_is_not_delegated(self):
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_malformed_markers_are_not_delegated(self):
        p = m.marker_path(self.ws, TID)
        p.parent.mkdir(parents=True)
        now = time.time()
        for body in ("not json", "[]", "{}",
                     json.dumps({"task_id": "other", "delegated_at": now, "ttl_s": 600}),
                     json.dumps({"task_id": TID, "delegated_at": "now", "ttl_s": 600}),
                     json.dumps({"task_id": TID, "delegated_at": now, "ttl_s": True}),
                     json.dumps({"task_id": TID, "delegated_at": now, "ttl_s": 10 ** 9}),
                     '{"task_id": "%s", "delegated_at": NaN, "ttl_s": 600}' % TID,
                     json.dumps({"task_id": TID, "delegated_at": now, "ttl_s": 600, "pad": "x" * 5000})):
            p.write_text(body)
            self.assertFalse(m.is_delegated(self.ws, TID), body[:80])

    def test_bounds_are_refused_before_any_write(self):
        for ttl in (0, -1, m.MAX_TTL_S + 1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                m.mark(self.ws, TID, ttl)
        for bad in ("../x", "a/b", "..", ""):
            with self.assertRaises(ValueError):
                m.mark(self.ws, bad, 60)
        self.assertFalse((self.ws / m.SUBDIR).exists())

    def test_holder_worker_then_background_then_none(self):
        self.assertIsNone(m.holder(self.ws, TID, worker_holder=lambda ws, t: None))
        m.mark(self.ws, TID, 600)
        self.assertEqual(m.holder(self.ws, TID), m.BACKGROUND_HOLDER)
        self.assertEqual(m.holder(self.ws, TID, worker_holder=lambda ws, t: "w1"), "w1")
        self.assertIsNone(m.holder(self.ws, TID, include_background=False))

    def test_holder_propagates_an_unreadable_worker_store(self):
        def unreadable(ws, t):
            raise PermissionError("deliveries/")
        with self.assertRaises(PermissionError):
            m.holder(self.ws, TID, worker_holder=unreadable)

    def test_clear_removes_it(self):
        m.mark(self.ws, TID, 600)
        self.assertTrue(m.clear(self.ws, TID))
        self.assertFalse(m.is_delegated(self.ws, TID))
        self.assertFalse(m.clear(self.ws, TID))

    def test_long_expired_markers_are_pruned_on_write(self):
        old = m.mark(self.ws, "task-old-1", 60)
        t = time.time() - m.MAX_TTL_S - 86400 - 10
        os.utime(old, (t, t))
        m.mark(self.ws, TID, 60)
        self.assertFalse(old.exists())

    def _main(self, *a):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = m.main([*a, "--workspace", str(self.ws)])
        return rc, err.getvalue()

    def test_cli_rcs(self):
        task_file = f"/fixture/tasks/{TID}.txt"
        self.assertEqual(self._main("check", task_file)[0], 1)
        self.assertEqual(self._main("mark", task_file, "--ttl", "1800")[0], 0)
        self.assertEqual(self._main("check", TID)[0], 0)
        self.assertEqual(self._main("mark", TID, "--ttl", "0")[0], 2)
        self.assertEqual(self._main("bogus")[0], 2)
        self.assertEqual(self._main("clear", TID)[0], 0)
        self.assertEqual(self._main("check", TID)[0], 1)

    def test_cli_check_compares_the_given_task_file(self):
        self.assertEqual(self._main("mark", TID)[0], 0)
        self.assertEqual(self._main("check", TID, "--task-file", str(self.task))[0], 0)
        os.utime(self.task, (time.time() + 1,) * 2)
        self.assertEqual(self._main("check", TID, "--task-file", str(self.task))[0], 1)

    def test_cli_resolves_the_workspace_when_not_given(self):
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(["mark", TID]), 0)
        self.assertTrue(m.is_delegated(self.ws, TID))

    def _holder(self, *a):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.main(["holder", TID, *a, "--workspace", str(self.ws)])
        return rc, out.getvalue().strip(), err.getvalue()

    def test_cli_holder_none_background_worker(self):
        deliveries = self.ws / "deliveries"
        self.assertEqual(self._holder()[:2], (1, ""))
        self.assertEqual(self._holder("--deliveries", str(deliveries))[:2], (1, ""))
        m.mark(self.ws, TID, 60)
        self.assertEqual(self._holder("--task-file", str(self.task))[:2], (0, m.BACKGROUND_HOLDER))
        (deliveries / "w1").mkdir(parents=True)
        (deliveries / "w1" / f"{TID}.accepted").write_text("")
        self.assertEqual(self._holder("--deliveries", str(deliveries))[:2], (0, m.WORKER_HOLDER))
        m.clear(self.ws, TID)
        self.assertEqual(self._holder("--deliveries", str(deliveries))[:2], (0, m.WORKER_HOLDER))
        # Without --deliveries no worker lookup is injected: the sentinel is not consulted.
        self.assertEqual(self._holder()[:2], (1, ""))

    def test_cli_holder_marker_follows_the_given_task_file(self):
        m.mark(self.ws, TID, 60)
        os.utime(self.task, (time.time() + 1,) * 2)
        self.assertEqual(self._holder("--task-file", str(self.task))[0], 1)

    def test_cli_holder_unreadable_worker_store_is_rc_3(self):
        deliveries = self.ws / "deliveries"
        deliveries.write_text("a file where the dir goes")
        rc, out, err = self._holder("--deliveries", str(deliveries))
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("treat the task as held", err)

    def test_cli_holder_without_the_dispatcher_has_no_worker(self):
        deliveries = self.ws / "deliveries"
        (deliveries / "w1").mkdir(parents=True)
        (deliveries / "w1" / f"{TID}.txt").write_text("")
        with mock.patch.dict(sys.modules, {"delivery.task_dispatch": None}):
            self.assertEqual(self._holder("--deliveries", str(deliveries))[:2], (1, ""))

    def test_cli_holder_calls_the_shared_predicate(self):
        with mock.patch.object(m, "holder", return_value="w9") as h:
            self.assertEqual(self._holder("--task-file", str(self.task))[:2], (0, "w9"))
        h.assert_called_once()
        self.assertEqual(h.call_args.kwargs["task_file"], str(self.task))

    def test_cli_help_exits_0(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["--help"]), 0)

    def test_unwritable_store_fails_with_rc_1(self):
        (self.ws / "state").mkdir()
        (self.ws / "state" / "task-delegations").write_text("a file where the dir goes")
        rc, err = self._main("mark", TID)
        self.assertEqual(rc, 1, err)
        self.assertIn("keeps blocking", err)

    def test_a_failed_replace_leaves_no_temp_and_no_marker(self):
        with mock.patch.object(m.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                m.mark(self.ws, TID, 60)
        self.assertEqual(list((self.ws / m.SUBDIR).iterdir()), [])
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_a_failed_temp_cleanup_still_raises_the_write_error(self):
        with mock.patch.object(m.os, "replace", side_effect=OSError("disk full")), \
                mock.patch.object(m.os, "unlink", side_effect=OSError("gone")):
            with self.assertRaisesRegex(OSError, "disk full"):
                m.mark(self.ws, TID, 60)

    def test_a_marker_that_cannot_be_pruned_does_not_fail_the_write(self):
        old = m.mark(self.ws, "task-old-1", 60)
        t = time.time() - m.MAX_TTL_S - 86400 - 10
        os.utime(old, (t, t))
        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            m.mark(self.ws, TID, 60)
        self.assertTrue(m.is_delegated(self.ws, TID))


class WorkspaceResolversAgree(unittest.TestCase):
    """`mark` without --workspace and the Stop hook (scripts/sutando-config.sh) must name one workspace."""

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_WORKSPACE", "SUTANDO_TEST_MODE", "SUTANDO_DEFAULT_WORKSPACE")}
        env.update(extra)
        return env

    def _shell_ws(self, env) -> Path:
        out = subprocess.run(["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
                             env=env, capture_output=True, text=True, check=True).stdout.strip()
        return Path(out).resolve()

    def _python_ws(self, env) -> Path:
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from workspace_default import resolve_workspace;"
                " print(resolve_workspace(migrate=False))")
        out = subprocess.run([sys.executable, "-c", code, str(REPO / "src")], env=env,
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out).resolve()

    def test_default_resolution_agrees(self):
        env = self._env()
        self.assertEqual(self._python_ws(env), self._shell_ws(env))

    def test_an_override_agrees_and_mark_lands_where_the_hook_reads(self):
        with tempfile.TemporaryDirectory() as td:
            cases = {"test-mode": self._env(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=f"{td}/a")}
            for label, env in cases.items():
                with self.subTest(label):
                    hook_ws = self._shell_ws(env)
                    self.assertTrue(str(hook_ws).startswith(str(Path(td).resolve())), hook_ws)
                    self.assertEqual(self._python_ws(env), hook_ws)
                    subprocess.run([sys.executable, str(REPO / "src" / "task_delegation.py"), "mark", TID],
                                   env=env, capture_output=True, check=True)
                    self.assertTrue((hook_ws / m.SUBDIR / f"{TID}.json").is_file())


if __name__ == "__main__":
    unittest.main()
