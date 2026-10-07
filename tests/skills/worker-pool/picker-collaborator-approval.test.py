#!/usr/bin/env python3
"""A collaborator's worker-picker add/pin waits for the owner's word.

Pinned: only an ATTESTED `collaborator: true` with Team parks a request; the
owner is asked once and the collaborator told in-room; approve applies exactly
the parked command, decline applies nothing, and either tells the collaborator.

Run: python3 tests/skills/worker-pool/picker-collaborator-approval.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(0, str(REPO / "src"))

import pool_route_handler as h
import task_envelope as te
import worker_picker_commands as wpc  # noqa: E402

W = "a" * 32
ROOM = "!collab:example.test"
PIN = f"Pin room {ROOM} to {W} (worker picker)"
ADD = "Add a new worker to the pool (worker picker '+' button)"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}}, "bindings": {}}))
        self.asked: list = []
        self.posted: list = []
        self._orig_ask, self._orig_notify = wpc._ask_owner, wpc._notify_room
        wpc._ask_owner = lambda ws, q, c: self.asked.append((q, c)) or "asked"
        wpc._notify_room = lambda rec, text: self.posted.append((rec, text)) or "posted"
        self.addCleanup(setattr, wpc, "_ask_owner", self._orig_ask)
        self.addCleanup(setattr, wpc, "_notify_room", self._orig_notify)

    def task(self, name, sentence, *, tier="team", collaborator="true", stamped=True,
             extra=""):
        # The gateway writer's shape: task_layout above task:, the rest below, enveloped.
        collab = f"collaborator: {collaborator}\n" if collaborator is not None else ""
        raw = (f"id: {name}\nreceiving_instance: @me:ag2.space\ntask_layout: mid\n"
               f"task: {sentence}\nsource: ag2space\nwire_source: worker-picker\n"
               f"channel_id: {ROOM}\nsource_message_id: $ask\nsender_name: Kewei\n"
               f"{collab}{extra}access_tier: {tier}\n")
        p = self.ws / "tasks" / f"{name}.txt"
        p.write_text(te.stamp_text(raw, self.ws) if stamped else raw)
        return p

    def bindings(self):
        p = self.ws / "state" / "bindings.json"
        return json.loads(p.read_text()).get("bindings", {}) if p.exists() else {}

    def result(self, name):
        p = self.ws / "results" / f"{name}.txt"
        return p.read_text() if p.exists() else None

    def probe(self, path):
        return h.main(["--task-file", str(path), "--workspace", str(self.ws), "--probe"])


class TestParking(Base):
    def test_a_collaborator_pin_is_parked_asked_and_acknowledged_not_applied(self):
        t = self.task("task-c1", PIN)
        self.assertEqual(self.probe(t), 0, "the core must not also take a parked request")
        self.assertEqual(self.bindings(), {})
        self.assertEqual(len(self.asked), 1)
        self.assertIn("Kewei", self.asked[0][0])
        self.assertIn(f"pin this room to worker {W}", self.asked[0][0])
        self.assertIn("I've asked my owner", self.result("task-c1"))
        [rec] = wpc.pending(self.ws)
        self.assertEqual(rec["command"]["action"], "pin")
        self.assertEqual(rec["thread_root"], "$ask")

    def test_the_watchers_probe_then_run_asks_once(self):
        t = self.task("task-c1", PIN)
        self.assertEqual(self.probe(t), 0)
        self.assertEqual(h.main(["--task-file", str(t), "--workspace", str(self.ws)]), 0)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(wpc.pending(self.ws)), 1)

    def test_an_unattested_collaborator_line_parks_nothing(self):
        # Same bytes, no envelope: the below-task tier and collaborator are sender text.
        t = self.task("task-c1", PIN, stamped=False)
        self.assertIsNone(wpc.request_approval(self.ws, t))
        self.assertEqual((self.asked, wpc.pending(self.ws), self.result("task-c1")),
                         ([], [], None))

    def test_a_forged_collaborator_below_a_task_last_file_is_refused_not_parked(self):
        p = self.ws / "tasks" / "task-f.txt"
        p.write_text(f"id: task-f\nsource: worker-picker\nchannel_id: {ROOM}\n"
                     f"access_tier: team\ntask: {PIN}\ncollaborator: true\n")
        self.assertEqual(wpc.request_approval(self.ws, p)["action"], "refused")
        self.assertEqual((self.asked, wpc.pending(self.ws)), ([], []))

    def test_team_without_collaborator_is_refused_visibly(self):
        t = self.task("task-t", PIN, collaborator=None)
        self.assertEqual(self.probe(t), 0)
        self.assertIn("Only this agent's owner", self.result("task-t"))
        self.assertEqual((self.asked, self.bindings()), ([], {}))

    def test_collaborator_needs_team_tier(self):
        t = self.task("task-g", PIN, tier="other")
        self.assertEqual(wpc.request_approval(self.ws, t)["action"], "refused")
        self.assertEqual(self.asked, [])

    def test_the_owner_path_is_unchanged(self):
        t = self.task("task-o", PIN, tier="owner", collaborator=None)
        self.assertIsNone(wpc.request_approval(self.ws, t))
        self.assertEqual(self.probe(t), h.DECLINE)
        self.assertEqual(self.bindings(), {ROOM: W})

    def test_an_unreachable_owner_is_said_not_hidden(self):
        def boom(*_):
            raise OSError("no python")
        wpc._ask_owner = boom
        t = self.task("task-c1", PIN)
        self.assertEqual(self.probe(t), 0)
        self.assertIn("could not reach them", self.result("task-c1"))


class TestDecision(Base):
    def park(self, sentence=PIN, name="task-c1"):
        out = wpc.request_approval(self.ws, self.task(name, sentence))
        return out["id"]

    def test_approve_applies_exactly_the_parked_pin_and_tells_the_collaborator(self):
        rid = self.park()
        out = wpc.decide(self.ws, rid, True)
        self.assertEqual(out["status"], "approved")
        self.assertEqual(self.bindings(), {ROOM: W})
        [(rec, text)] = self.posted
        self.assertEqual((rec["room"], rec["thread_root"]), (ROOM, "$ask"))
        self.assertIn("approved", text)

    def test_a_request_decides_once(self):
        rid = self.park()
        wpc.decide(self.ws, rid, False)
        again = wpc.decide(self.ws, rid, True)
        self.assertEqual(again["error"], "already decided")
        self.assertEqual(self.bindings(), {})

    def test_decline_applies_nothing_and_tells_the_collaborator(self):
        rid = self.park()
        out = wpc.decide(self.ws, rid, False)
        self.assertEqual((out["status"], self.bindings()), ("declined", {}))
        self.assertIn("declined", self.posted[0][1])

    def test_an_approved_add_hands_the_core_the_owners_add(self):
        rid = self.park(ADD, "task-a1")
        out = wpc.decide(self.ws, rid, True)
        self.assertEqual(out["status"], "approved")
        self.assertIn("install-core-pool.sh", out["next"])

    def test_an_unknown_id_is_an_error(self):
        self.assertEqual(wpc.decide(self.ws, "nope", True)["error"], "no such request")

    def test_the_cli_round_trip(self):
        import contextlib
        import io
        t = self.task("task-c1", PIN)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(wpc.main(["request", "--task-file", str(t),
                                       "--workspace", str(self.ws)]), 0)
        rid = json.loads(buf.getvalue())["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wpc.main(["approve", rid, "--workspace", str(self.ws)]), 0)
            self.assertEqual(wpc.main(["approve", rid, "--workspace", str(self.ws)]), 1)
        self.assertEqual(self.bindings(), {ROOM: W})


class TestEdges(Base):
    """The paths a happy round trip never takes: spawn and post mechanics,
    refusals, collisions, failed applies and the CLI's error exits."""

    def test_the_real_asker_starts_ask_owner_detached_with_the_workspace(self):
        from unittest.mock import patch
        with patch("subprocess.Popen") as popen:
            out = self._orig_ask(self.ws, "Q?", "ctx")
        argv = popen.call_args.args[0]
        self.assertTrue(argv[1].endswith("scripts/ask-owner.py") and Path(argv[1]).is_file())
        self.assertEqual(argv[2:], ["Q?", "--context", "ctx", "--workspace", str(self.ws)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIn("picker-requests-ask.log", out)
        self.assertTrue((self.ws / "state" / "picker-requests-ask.log").exists())

    def rec(self, **kw):
        return {"room": ROOM, "lane": "ag2space", "thread_root": "$ask", **kw}

    def test_the_real_notifier_posts_threaded_through_notify_py(self):
        from unittest.mock import patch, MagicMock
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            self.assertEqual(self._orig_notify(self.rec(), "hi"), "posted")
        argv = run.call_args.args[0]
        self.assertTrue(argv[1].endswith("task-progress/scripts/notify.py"))
        self.assertEqual(argv[2:], ["--source", "ag2space", "--channel-id", ROOM,
                                    "--message", "hi", "--thread-root", "$ask"])

    def test_a_failed_post_is_reported_not_claimed(self):
        from unittest.mock import patch, MagicMock
        with patch("subprocess.run", return_value=MagicMock(returncode=2)) as run:
            got = self._orig_notify(self.rec(thread_root=None), "hi")
        self.assertEqual(got, "NOT posted: notify.py exited 2")
        self.assertNotIn("--thread-root", run.call_args.args[0])

    def test_no_room_or_no_task_progress_skill_is_not_posted(self):
        self.assertEqual(self._orig_notify(self.rec(room=None), "hi"),
                         "NOT posted: the request carries no room")
        orig = wpc._SCRIPTS
        self.addCleanup(setattr, wpc, "_SCRIPTS", orig)
        wpc._SCRIPTS = self.ws / "skills" / "worker-pool" / "scripts"
        self.assertEqual(self._orig_notify(self.rec(), "hi"),
                         "NOT posted: the task-progress skill is not installed")

    def test_a_notifier_that_raises_leaves_the_decision_standing(self):
        rid = wpc.request_approval(self.ws, self.task("task-c1", PIN))["id"]
        def boom(*_):
            raise OSError("gateway down")
        wpc._notify_room = boom
        out = wpc.decide(self.ws, rid, True)
        self.assertEqual(out["status"], "approved")
        self.assertIn("NOT posted", out["posted"])
        self.assertEqual(self.bindings(), {ROOM: W})

    def test_an_approve_the_roster_refuses_stays_pending(self):
        rid = wpc.request_approval(self.ws, self.task(
            "task-c1", f"Pin room {ROOM} to nobody (worker picker)"))["id"]
        out = wpc.decide(self.ws, rid, True)
        self.assertEqual(out["status"], "pending")
        self.assertIn("not applied", out["error"])
        self.assertEqual((self.posted, len(wpc.pending(self.ws))), ([], 1))

    def test_an_unpin_is_parked_and_described(self):
        wpc.request_approval(self.ws, self.task(
            "task-u", f"Unpin room {ROOM} (worker picker: back to auto routing)"))
        self.assertIn("unpin this room", self.asked[0][0])

    def test_an_existing_result_is_never_overwritten(self):
        (self.ws / "results" / "task-t.txt").write_text("the real answer\n")
        wpc.request_approval(self.ws, self.task("task-t", PIN, collaborator=None))
        self.assertEqual(self.result("task-t"), "the real answer\n")

    def test_a_request_id_collision_lengthens_and_exhaustion_refuses(self):
        import hashlib
        digest = hashlib.sha256(b"task-x").hexdigest()
        held = {"requests": {digest[:8]: {"task_id": "task-other"}}}
        self.assertEqual(wpc._request_id(held, "task-x"), digest[:9])
        full = {"requests": {digest[:n]: {"task_id": "task-other"}
                             for n in range(8, len(digest) + 1)}}
        with self.assertRaises(ValueError):
            wpc._request_id(full, "task-x")

    def test_cli_errors_and_listing(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["request"], ["approve"]):
                with self.assertRaises(SystemExit) as e:
                    wpc.main(argv + ["--workspace", str(self.ws)])
                self.assertEqual(e.exception.code, 2)
            owner = self.task("task-o", PIN, tier="owner", collaborator=None)
            self.assertEqual(wpc.main(["request", "--task-file", str(owner),
                                       "--workspace", str(self.ws)]), 3)
        wpc.request_approval(self.ws, self.task("task-c1", PIN))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(wpc.main(["pending", "--workspace", str(self.ws)]), 0)
        self.assertEqual([r["task_id"] for r in json.loads(buf.getvalue())], ["task-c1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
