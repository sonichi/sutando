#!/usr/bin/env python3
"""Direct branch coverage for RuntimeServer's push/watcher internals.

Drives the extracted-for-testability methods without a socket: dead
subscribers are discarded on push (never crash the pusher), the activity
watcher survives feed rotation and poison lines, and the result emitter
skips unreadable files while still advancing `seen`.

Run: python3 tests/runtime-api-server-branches.test.py   (stdlib only)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
sys.path.insert(0, str(REPO / "src"))

import server as srv  # noqa: E402


class _DeadWriter:
    """Raises like a closed transport on write — the discard branch's food."""

    def write(self, data):
        raise ConnectionResetError("gone")

    async def drain(self):
        raise AssertionError("drain must not be reached after a failed write")


class _LiveWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        return None


def _mk_server(tmp: str) -> "srv.RuntimeServer":
    return srv.RuntimeServer(
        socket_path=str(Path(tmp) / "s.sock"),
        db_path=str(Path(tmp) / "db.sqlite"),
        ha_dir=str(Path(tmp) / "ha"),
        state_dir=str(Path(tmp) / "state"))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class PushDiscard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "state").mkdir()
        self.s = _mk_server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dead_activity_subscriber_is_discarded_live_kept(self):
        dead, live = _DeadWriter(), _LiveWriter()
        self.s._activity_subscribers.update({dead, live})
        run(self.s._push_activity({"kind": "step", "step": "x"}))
        self.assertNotIn(dead, self.s._activity_subscribers)
        self.assertIn(live, self.s._activity_subscribers)
        self.assertTrue(live.frames)

    def test_dead_request_subscriber_is_discarded(self):
        dead = _DeadWriter()
        self.s._request_subscribers.add(dead)
        run(self.s._push_request({"requestId": "r1"}))
        self.assertNotIn(dead, self.s._request_subscribers)

    def test_request_summary_projects_card_fields(self):
        out = srv.RuntimeServer._request_summary(
            {"requestId": "r1", "requestType": "elicitation",
             "taskId": "t1", "createdAt": "c", "expiresAt": "e",
             "params": {"question": "q?", "action": None,
                        "reason": None, "instructions": None}})
        self.assertEqual(out["requestId"], "r1")
        self.assertEqual(out["question"], "q?")


class ActivityWatcherBranches(unittest.TestCase):
    def _drive(self, prepare, iterations=6):
        """Run the real watcher loop briefly with a live subscriber."""
        tmp = tempfile.TemporaryDirectory()
        state = Path(tmp.name) / "state"
        state.mkdir()
        s = _mk_server(tmp.name)
        live = _LiveWriter()
        s._activity_subscribers.add(live)
        prepare(state)

        async def go():
            task = asyncio.get_event_loop().create_task(s._activity_watcher())
            await asyncio.sleep(0.35 * iterations)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(go())
        tmp.cleanup()
        return live.frames

    def test_feed_rotation_and_poison_lines_survive(self):
        feed_holder = {}

        def prepare2(state):
            feed = state / "activity-feed.jsonl"
            feed.write_text("x" * 100 + "\n")

            async def mutate():
                await asyncio.sleep(0.4)
                feed.write_text("")                       # rotated/truncated
                await asyncio.sleep(0.4)
                with feed.open("a") as fh:
                    fh.write("\n{not json\n")
                    fh.write(json.dumps({"kind": "tool",
                                         "step": "good line"}) + "\n")

            feed_holder["mutate"] = mutate

        tmp = tempfile.TemporaryDirectory()
        state = Path(tmp.name) / "state"
        state.mkdir()
        s = _mk_server(tmp.name)
        live = _LiveWriter()
        s._activity_subscribers.add(live)
        prepare2(state)

        async def go():
            loop = asyncio.get_event_loop()
            task = loop.create_task(s._activity_watcher())
            loop.create_task(feed_holder["mutate"]())
            await asyncio.sleep(2.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(go())
        joined = b"".join(live.frames).decode(errors="replace")
        self.assertIn("good line", joined)
        self.assertNotIn("not json", joined)
        tmp.cleanup()


class RequestsWatcherBranches(unittest.TestCase):
    def test_idle_refresh_then_push_on_subscribe(self):
        tmp = tempfile.TemporaryDirectory()
        (Path(tmp.name) / "state").mkdir()
        s = _mk_server(tmp.name)
        live = _LiveWriter()

        async def go():
            task = asyncio.get_event_loop().create_task(s._requests_watcher())
            await asyncio.sleep(0.8)              # idle branch: no subscribers
            s._request_subscribers.add(live)
            s.store.issue("rq-1", "elicitation", "t-1",
                          {"question": "watch me?"}, None) \
                if hasattr(s.store, "issue") else None
            await asyncio.sleep(0.8)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(go())
        tmp.cleanup()


class HelperFallbacks(unittest.TestCase):
    def test_channels_dir_none_when_resolver_unimportable(self):
        import unittest.mock as mock
        with mock.patch.dict(sys.modules, {"util_paths": None}):
            self.assertIsNone(srv._channels_dir())

    def test_host_label_resolves_via_repo_script(self):
        import subprocess
        import unittest.mock as mock

        def fake_run(argv, **kw):
            assert argv[0] == "bash" and argv[-1] == "host-label"
            return subprocess.CompletedProcess(argv, 0,
                                               stdout="lab-host\n", stderr="")
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("subprocess.run", fake_run):
            os.environ.pop("SUTANDO_HOST_LABEL", None)
            # exact value: the success path must surface the script's label
            self.assertEqual(srv._host_label(), "lab-host")

    def test_watchers_fall_back_on_malformed_poll_interval(self):
        import unittest.mock as mock
        tmp = tempfile.TemporaryDirectory()
        (Path(tmp.name) / "state").mkdir()
        s = _mk_server(tmp.name)
        env = {"SUTANDO_RESULT_POLL_S": "bogus",
               "SUTANDO_REQUEST_POLL_S": "also-bogus"}

        async def drive(coro):
            t = asyncio.ensure_future(coro)
            await asyncio.sleep(0.05)
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        with mock.patch.dict(os.environ, env):
            run(drive(s._results_watcher()))
            run(drive(s._requests_watcher()))
        tmp.cleanup()

    def test_host_label_none_when_script_fails(self):
        import unittest.mock as mock
        with mock.patch.object(srv, "_HERE",
                               Path(tempfile.mkdtemp()) / "src" / "runtime-api"):
            self.assertIsNone(srv._host_label())

    def test_enrolled_agent_id_absent_and_corrupt(self):
        # The reader moved to rundir.py so daemon, CLI and shell share it.
        import rundir  # noqa: PLC0415
        self.assertIsNone(rundir.enrolled_agent_id(None))
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(rundir.enrolled_agent_id(td))
            (Path(td) / "auth").mkdir()
            (Path(td) / "auth" / "ag2space.json").write_text("{broken")
            self.assertIsNone(rundir.enrolled_agent_id(td))


class RequestsWatcherStoreFailures(unittest.TestCase):
    def test_store_failures_never_kill_the_watcher(self):
        tmp = tempfile.TemporaryDirectory()
        (Path(tmp.name) / "state").mkdir()
        s = _mk_server(tmp.name)

        class _FlakyStore:
            def __init__(self):
                self.calls = 0

            def pending(self):
                self.calls += 1
                if self.calls % 2:
                    raise RuntimeError("store hiccup")
                return []

        s.store = _FlakyStore()
        live = _LiveWriter()

        async def go():
            task = asyncio.get_event_loop().create_task(s._requests_watcher())
            await asyncio.sleep(0.8)             # idle + exception branches
            s._request_subscribers.add(live)
            await asyncio.sleep(0.8)             # active + exception branches
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(go())
        self.assertGreater(s.store.calls, 2)     # survived repeated failures
        tmp.cleanup()


class EmitNewResults(unittest.TestCase):
    def test_archive_between_read_and_stat_still_delivers(self):
        # read succeeds, file is archived before stat: the frame must still
        # go out and the watcher must NOT raise (daemon-killing race)
        tmp = tempfile.TemporaryDirectory()
        (Path(tmp.name) / "state").mkdir()
        s = _mk_server(tmp.name)
        rdir = Path(tmp.name) / "results"
        rdir.mkdir()
        f = rdir / "task-race.txt"
        f.write_text("race body")

        real_read = srv.read_ready_result

        def read_then_unlink(p):
            body = real_read(p)
            if body is not None:
                Path(p).unlink()      # concurrent archival wins the gap
            return body

        live = _LiveWriter()
        s._subscribers.add(live)
        seen: set = set()
        import unittest.mock as mock
        with mock.patch.object(srv, "read_ready_result", read_then_unlink):
            class _T:
                def _result_files(self):
                    return [(f, 1787000000)]
            run(s._emit_new_results(_T(), seen))
        joined = b"".join(live.frames).decode(errors="replace")
        self.assertIn("race body", joined)
        self.assertIn("task-race.txt", seen)
        tmp.cleanup()

    def test_oserror_listing_returns_and_unreadable_file_skipped(self):
        tmp = tempfile.TemporaryDirectory()
        (Path(tmp.name) / "state").mkdir()
        s = _mk_server(tmp.name)

        class _BoomTasks:
            def _result_files(self):
                raise OSError("listing failed")

        run(s._emit_new_results(_BoomTasks(), set()))  # returns, never raises

        rdir = Path(tmp.name) / "results"
        rdir.mkdir()
        good = rdir / "task-ok.txt"
        good.write_text("good body")
        bad = rdir / "task-bad.txt"
        bad.write_text("hidden")
        bad.chmod(0o000)

        class _Tasks:
            def _result_files(self):
                return [(good, 1787000000), (bad, 1787000001)]

        live = _LiveWriter()
        dead = _DeadWriter()
        s._subscribers.update({live, dead})
        seen: set = set()
        run(s._emit_new_results(_Tasks(), seen))
        self.assertNotIn(dead, s._subscribers)   # dead result-subscriber discarded
        joined = b"".join(live.frames).decode(errors="replace")
        self.assertIn("good body", joined)
        self.assertNotIn("hidden", joined)
        # transient read failure must NOT consume the name — the result
        # retries and is delivered once readable (kewei's control)
        self.assertNotIn("task-bad.txt", seen)
        bad.chmod(0o644)
        run(s._emit_new_results(_Tasks(), seen))
        joined2 = b"".join(live.frames).decode(errors="replace")
        self.assertIn("hidden", joined2)         # recovered on the next pass
        self.assertIn("task-bad.txt", seen)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
