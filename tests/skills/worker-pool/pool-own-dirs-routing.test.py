#!/usr/bin/env python3
"""A worker on its own task folders (`dirs: "own"`) is never routed to; pool_ask reaches it over s.space.

- The router (`route`, and the watcher handler's `classify`) drops an own-dir worker from a task's
  targets, so a room bound to it, or a task naming it, falls to the core and no sentinel is written; a
  shared-dir worker in the same set still gets it. A shared-dir worker routes exactly as before.
- pool_ask to an own-dir worker takes the s.space route even with the hand-off flag off; an ask that
  route cannot carry (a worker's own ask, a disabled identity) is refused, never sent to a folder;
  a supervision probe (no hand-off) is written into the worker's own inbox, never routed.

Run: python3 tests/skills/worker-pool/pool-own-dirs-routing.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "skills/worker-pool/scripts"), str(REPO / "src"), str(REPO / "packages/ag2-sparrow")]

import connections as C, pool_ask as pa, pool_handoff as ph, pool_route_handler as prh  # noqa: E401,E402

pr, rt, wi = pa.pr, pa.rt, pa.sup.wi
CORE_AGENT, WORKER_AGENT, ROOM = "@core:srv.example", "@alpha-agent:srv.example", "!bound:srv.example"


def make_worker(ws, label):
    wid = wi.new_worker_id()
    wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",
                     session_id="11111111-1111-4111-8111-111111111111",
                     tmux_socket="/recorded/app/run/tmux.sock", worker_id=wid)
    pr.register_worker(ws, wid, label)
    return wid


def row(instance, **extra):
    return {"server": "srv.example", "instance": instance, "gateway_url": "https://chat.srv.example",
            "token_ref": f"AG2_REMOTE_TOKEN_{instance.upper().replace('-', '_')}", "primary": False,
            "added_by": "owner", "added_at": "2026-11-16T00:00:00Z", "verified": True,
            "server_source": "gateway", **extra}


class FakeRoom:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "event_id": "$req", "posted": True}), "")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        (self.ws / "tasks").mkdir()
        self.alpha, self.beta = make_worker(self.ws, "alpha"), make_worker(self.ws, "beta")
        self.record(own=True)
        clean = {k: v for k, v in os.environ.items() if k not in (ph.FLAG, ph.ROOM, ph.ROOM_CLI, "SUTANDO_INSTANCE_ID")}
        env = mock.patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def record(self, own=True, **worker_extra):
        extra = {"dirs": "own"} if own else {}
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "connections": [
            dict(row("srv-example", agent_mxid=CORE_AGENT), primary=True),
            row("srv-example-alpha", worker=self.alpha, agent_mxid=WORKER_AGENT, **extra, **worker_extra)]}))

    def own_tasks(self):
        return C.worker_dirs(self.alpha, self.ws)[0]

    def task(self, tid, **headers):
        (self.ws / "tasks" / f"{tid}.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in {"id": tid, **headers}.items()) + "task: hi\n")
        return {"id": tid, **headers}

    def sentinels(self, wid):
        d = self.ws / "deliveries" / wid
        return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []


class Router(Base):
    def test_a_room_bound_to_an_own_dir_worker_falls_to_the_core(self):
        pr.bind_room(self.ws, ROOM, self.alpha)
        out = rt.route(self.ws, self.task("task-r1", channel_id=ROOM))
        self.assertEqual(out["targets"], [pr.CORE])
        self.assertEqual(self.sentinels(self.alpha), [])

    def test_a_task_naming_an_own_dir_worker_falls_to_the_core(self):
        out = rt.route(self.ws, self.task("task-r2", requested_worker=self.alpha))
        self.assertEqual(out["targets"], [pr.CORE])
        self.assertEqual(self.sentinels(self.alpha), [])

    def test_a_shared_dir_worker_routes_as_before(self):
        self.record(own=False)
        pr.bind_room(self.ws, ROOM, self.alpha)
        out = rt.route(self.ws, self.task("task-r3", channel_id=ROOM))
        self.assertEqual((out["targets"], out["delivered"]), ([self.alpha], [self.alpha]))
        self.assertEqual(self.sentinels(self.alpha), ["task-r3.txt"])

    def test_a_shared_worker_beside_an_own_dir_one_keeps_the_task(self):
        self.assertEqual(rt.without_own_dir_workers(self.ws, [self.alpha, self.beta]), [self.beta])
        self.assertEqual(rt.without_own_dir_workers(self.ws, [self.beta]), [self.beta])
        self.assertEqual(rt.without_own_dir_workers(self.ws, [pr.CORE]), [pr.CORE])
        self.assertEqual(rt.without_own_dir_workers(self.ws, [self.alpha]), [pr.CORE])

    def test_the_watcher_handler_declines_it(self):
        pr.bind_room(self.ws, ROOM, self.alpha)
        tid = "task-h1"
        self.task(tid, channel_id=ROOM)
        argv = ["--task-file", str(self.ws / "tasks" / f"{tid}.txt"), "--workspace", str(self.ws)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prh.main(argv), prh.DECLINE)
        self.assertEqual(self.sentinels(self.alpha), [])
        self.record(own=False)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prh.main(argv), 0)
        self.assertEqual(self.sentinels(self.alpha), [f"{tid}.txt"])


class Ask(Base):
    def room(self):
        os.environ.update({ph.ROOM: ROOM, ph.ROOM_CLI: "/fake/room_cli.py"})
        return FakeRoom()

    def test_the_core_asks_an_own_dir_worker_over_s_space_with_the_flag_off(self):
        fake = self.room()
        with mock.patch.object(pa, "whoami", return_value=pr.CORE):
            out = pa.ask(self.ws, self.alpha, "review this", runner=fake)
        self.assertEqual((out["via"], out["status"]), ("room", "sent"))
        self.assertEqual(sorted(p.name for p in (self.ws / "tasks").iterdir()), [])
        self.assertEqual(self.sentinels(self.alpha), [])

    def test_an_ask_the_room_cannot_carry_is_refused(self):
        self.room()
        with mock.patch.object(pa, "whoami", return_value=self.beta), self.assertRaises(ph.HandoffError):
            pa.ask(self.ws, self.alpha, "review this", runner=FakeRoom())
        self.record(own=True, disabled=True)
        with mock.patch.object(pa, "whoami", return_value=pr.CORE), self.assertRaises(ph.HandoffError):
            pa.ask(self.ws, self.alpha, "review this", runner=FakeRoom())
        self.assertEqual(sorted(p.name for p in (self.ws / "tasks").iterdir()), [])

    def test_a_supervision_probe_lands_in_its_own_inbox_unrouted(self):
        with mock.patch.object(pa, "whoami", return_value=pr.CORE):
            out = pa.ask(self.ws, self.alpha, "re-arm", allow_handoff=False)
        self.assertEqual(Path(out["task_file"]).parent, self.own_tasks())
        self.assertTrue(Path(out["task_file"]).is_file())
        self.assertNotIn("route", out)
        self.assertEqual(self.sentinels(self.alpha), [])

    def test_a_shared_dir_worker_with_the_flag_off_keeps_the_folder(self):
        self.record(own=False)
        with mock.patch.object(pa, "whoami", return_value=pr.CORE):
            out = pa.ask(self.ws, self.alpha, "review this")
        self.assertEqual(Path(out["task_file"]).parent, self.ws / "tasks")
        self.assertEqual(out["route"]["delivered"], [self.alpha])


if __name__ == "__main__":
    unittest.main()
