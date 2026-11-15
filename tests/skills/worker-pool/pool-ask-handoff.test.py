#!/usr/bin/env python3
"""pool_ask over s.space (WORKER_POOL_SSPACE_HANDOFF): flag-gated, fakes only.

Pinned: the manifest ships the flag off, and off the folder path is unchanged and the hand-off code
is never consulted; on, an ask from the core to a worker with an enabled identity posts one typed
hand-off through the room transport and collects the reply correlated by task id, with no task file
or delivery; a worker with no, a disabled or a parked identity, a worker's own ask, a relayed ask
and a parent-held ask keep the folder; an identity that cannot be addressed is refused, never a
silent fallback; a timeout is `None` and exit 1, a refusal before sending exit 2, an unconfirmed
post is never resent, a failed reply is exit 1, and an unreadable room is an error, not silence.

Run: python3 tests/skills/worker-pool/pool-ask-handoff.test.py
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
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(0, str(REPO / "src"))

import handoff_contract as hc, pool_ask as pa, pool_handoff as ph, skill_config  # noqa: E401,E402

pr, wi = pa.pr, pa.sup.wi
CORE_AGENT, WORKER_AGENT, ROOM = "@core:srv", "@alpha-agent:srv", "!handoff:srv"
ON = {ph.FLAG: "1", ph.ROOM: ROOM, ph.ROOM_CLI: "/fake/room_cli.py"}


class FakeRoom:
    """The room transport: records each call, posts the hand-off, answers reads from a script."""

    def __init__(self, *, mention=None, replies=None, read_ok=True):
        self.calls, self.mention_out, self.replies, self.read_ok = [], mention, replies, read_ok
        self.posted = None

    def __call__(self, argv):
        verb = argv[2]
        self.calls.append(argv)
        if verb == "mention":
            self.posted = argv[4]
            out = self.mention_out if self.mention_out is not None else {"ok": True, "event_id": "$req", "posted": True}
            stdout = out if isinstance(out, str) else json.dumps(out)
        else:
            tid = hc.parse_request(self.posted)["task_id"] if self.posted else "task-none"
            msgs = self.replies(tid) if callable(self.replies) else (self.replies or [])
            stdout = json.dumps({"ok": True, "messages": msgs}) if self.read_ok else "gateway down"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def verbs(self):
        return [a[2] for a in self.calls]


def make_worker(ws, label):
    wid = wi.new_worker_id()
    wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",
                     session_id="11111111-1111-4111-8111-111111111111",
                     tmux_socket="/recorded/app/run/tmux.sock", worker_id=wid)
    pr.register_worker(ws, wid, label)
    return wid


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "tasks").mkdir()
        self.alpha = make_worker(self.ws, "alpha")
        self.rows = [{"instance": "core", "server": "srv", "agent_mxid": CORE_AGENT},
                     {"instance": "alpha", "server": "srv", "agent_mxid": WORKER_AGENT, "worker": self.alpha}]
        patch_rows = mock.patch.object(ph, "_rows", lambda ws: self.rows)
        patch_rows.start()
        self.addCleanup(patch_rows.stop)
        clean = {k: v for k, v in os.environ.items() if k not in (ph.FLAG, ph.ROOM, ph.ROOM_CLI, "SUTANDO_INSTANCE_ID")}
        env = mock.patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.sleeps = []

    def on(self, **extra):
        os.environ.update({**ON, **extra})

    def sleep(self, s):
        self.sleeps.append(s)

    def ask(self, room, **kw):
        return pa.ask(self.ws, "alpha", "status of #7?", runner=room, sleep=self.sleep, **kw)

    def delivered(self, tid):
        return (self.ws / "deliveries" / self.alpha / f"{tid}.txt").exists()


class FlagOff(Base):
    def test_the_manifest_ships_it_off(self):
        cfg = skill_config.manifest_config(ph.MANIFEST)
        self.assertEqual(cfg.get(ph.FLAG), "0")
        self.assertFalse(ph.enabled({}))

    def test_off_the_folder_path_is_unchanged_and_the_handoff_is_never_consulted(self):
        room = FakeRoom()
        with mock.patch.object(ph, "route_for", side_effect=AssertionError("consulted while off")):
            out = self.ask(room)
        self.assertEqual(set(out), {"task_id", "to", "from", "task_file", "route"})
        self.assertTrue(self.delivered(out["task_id"]))
        self.assertEqual(room.calls, [])
        self.assertFalse((self.ws / "state" / "pool-handoff").exists())

    def test_off_the_task_file_matches_on_with_no_identity(self):
        fixed = "task-aaaaaaaaaaaaaaaaaa"
        texts = []
        for setup in (lambda: None, lambda: (self.on(), self.rows.pop())):
            setup()
            with mock.patch.object(pa.secrets, "token_hex", return_value=fixed[5:]), \
                    mock.patch.object(pa, "datetime") as dt:
                dt.now.return_value.strftime.return_value = "2026-11-15T00:00:00Z"
                pa.ask(self.ws, "alpha", "same?", runner=FakeRoom())
            texts.append((self.ws / "tasks" / f"{fixed}.txt").read_text())
        self.assertEqual(texts[0], texts[1])


class FlagOn(Base):
    def setUp(self):
        super().setUp()
        self.on()

    def test_a_typed_handoff_is_sent_and_the_correlated_reply_collected(self):
        room = FakeRoom(replies=lambda tid: [
            {"sender": WORKER_AGENT, "body": hc.build_reply(tid, "working", "on it"), "ts": 2, "event_id": "$w"},
            {"sender": WORKER_AGENT, "body": hc.build_reply(tid, "done", "#7 is green"), "ts": 3, "event_id": "$d",
             "in_reply_to": "$req"}])
        out = self.ask(room, wait_s=30)
        self.assertEqual((out["via"], out["status"], out["reply"], out["reply_status"]),
                         ("s.space", "sent", "#7 is green", "done"))
        mention = room.calls[0]
        self.assertEqual(mention[2:4], ["mention", WORKER_AGENT])
        self.assertEqual(mention[5:], [ROOM, "--agent", CORE_AGENT], "sent as the core's agent")
        req = hc.parse_request(room.posted)
        self.assertEqual(req["task_id"], out["task_id"])
        self.assertEqual(req["question"], "[pool-ask from core] status of #7?")
        self.assertFalse((self.ws / "tasks" / f"{out['task_id']}.txt").exists(), "no task file")
        self.assertFalse(self.delivered(out["task_id"]), "no delivery")
        rec = ph.read_record(self.ws, out["task_id"])
        self.assertEqual((rec["event_id"], rec["to_agent"], rec["worker"]), ("$req", WORKER_AGENT, self.alpha))

    def test_an_untyped_reply_to_the_request_is_the_answer(self):
        room = FakeRoom(replies=[{"sender": WORKER_AGENT, "body": "plain", "ts": 1, "event_id": "$p",
                                  "in_reply_to": "$req"}])
        self.assertEqual(self.ask(room, wait_s=5)["reply"], "plain")

    def test_no_wait_sends_and_collect_reads_it_later(self):
        room = FakeRoom()
        out = self.ask(room)
        self.assertNotIn("reply", out)
        self.assertEqual(room.verbs(), ["mention"])
        room.replies = [{"sender": WORKER_AGENT, "body": hc.build_reply(out["task_id"], "done", "later"),
                         "ts": 1, "event_id": "$l"}]
        got = pa.collect(self.ws, out["task_id"], 0, runner=room, sleep=self.sleep)
        self.assertEqual((got["reply"], got["reply_status"]), ("later", "done"))

    def test_a_timeout_is_none(self):
        room = FakeRoom(replies=[])
        out = self.ask(room, wait_s=0.05)
        self.assertIsNone(out["reply"])
        self.assertEqual(out["reply_status"], "waiting")
        self.assertGreaterEqual(room.verbs().count("read"), 1)

    def test_a_failed_reply_is_reported_as_failed(self):
        room = FakeRoom(replies=lambda tid: [{"sender": WORKER_AGENT, "body": hc.build_reply(tid, "failed", "no access"),
                                              "ts": 1, "event_id": "$f"}])
        out = self.ask(room, wait_s=5)
        self.assertEqual((out["reply_status"], out["reply"]), ("failed", "no access"))

    def test_a_refusal_before_sending_raises_and_records_nothing(self):
        room = FakeRoom(mention={"ok": False, "posted": False, "reason": "not a member"})
        with self.assertRaisesRegex(ValueError, "not a member"):
            self.ask(room)
        self.assertFalse((self.ws / "state" / "pool-handoff").exists())

    def test_an_unclear_post_is_unconfirmed_and_never_resent(self):
        room = FakeRoom(mention="Traceback: timeout", replies=[])
        out = self.ask(room, wait_s=0.05)
        self.assertEqual(out["status"], "unconfirmed")
        self.assertEqual(room.verbs().count("mention"), 1)

    def test_a_room_that_cannot_be_read_is_an_error_not_silence(self):
        room = FakeRoom(read_ok=False)
        with self.assertRaisesRegex(ValueError, "could not be read"):
            self.ask(room, wait_s=0.05)

    def test_without_an_enabled_identity_the_folder_is_kept(self):
        for change in ({"disabled": True}, {"parked": True}, None):
            if change is None:
                self.rows.pop()
            else:
                self.rows[1].update(change)
            room = FakeRoom()
            out = self.ask(room)
            self.assertNotIn("via", out)
            self.assertTrue(self.delivered(out["task_id"]))
            self.assertEqual(room.calls, [])
            if change:
                for k in change:
                    self.rows[1].pop(k)

    def test_asks_the_handoff_must_not_carry_keep_the_folder(self):
        beta = make_worker(self.ws, "beta")
        cases = [dict(tier="guest", relayed_from="@visitor:x"), dict(parent_task="task-bbbbbbbbbbbbbbbbbb")]
        for kw in cases:
            room = FakeRoom()
            with mock.patch("handoffs.open_handoff", return_value="h1"):
                out = self.ask(room, **kw)
            self.assertNotIn("via", out, kw)
            self.assertEqual(room.calls, [], kw)
        os.environ["SUTANDO_INSTANCE_ID"] = beta
        room = FakeRoom()
        self.assertNotIn("via", self.ask(room), "a worker's own ask keeps the folder")
        self.assertEqual(room.calls, [])

    def test_allow_handoff_false_keeps_the_folder(self):
        room = FakeRoom()
        out = self.ask(room, allow_handoff=False)
        self.assertTrue(self.delivered(out["task_id"]))
        self.assertEqual(room.calls, [])

    def test_an_identity_that_cannot_be_addressed_is_refused(self):
        for drop in (ph.ROOM, ph.ROOM_CLI):
            os.environ.pop(drop)
            with self.assertRaisesRegex(ValueError, drop):
                self.ask(FakeRoom())
            self.on()
        self.rows[0]["server"] = "elsewhere"
        with self.assertRaisesRegex(ValueError, "core lane"):
            self.ask(FakeRoom())
        self.assertEqual(list((self.ws / "tasks").iterdir()), [], "a refusal wrote a task")


class Cli(Base):
    def run_cli(self, *argv, room):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ph, "_default_runner", room), mock.patch.object(pa.time, "sleep", self.sleep), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pa.main(["--workspace", str(self.ws), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        self.on()
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", "--wait", "0.05", room=FakeRoom(replies=[]))
        self.assertEqual(rc, 1)
        self.assertIn("over s.space", out)
        self.assertIn("no reply", out)
        rc, _, err = self.run_cli("--to", "alpha", "--ask", "?",
                                  room=FakeRoom(mention={"ok": False, "posted": False, "reason": "denied"}))
        self.assertEqual(rc, 2)
        self.assertIn("denied", err)
        failing = FakeRoom(replies=lambda tid: [{"sender": WORKER_AGENT, "body": hc.build_reply(tid, "failed", "x"),
                                                 "ts": 1, "event_id": "$f"}])
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", "--wait", "5", room=failing)
        self.assertEqual(rc, 1)
        self.assertIn("reported failure", out)

    def test_collect_an_unknown_id_exits_2(self):
        rc, _, err = self.run_cli("--collect", "task-cccccccccccccccccc", room=FakeRoom())
        self.assertEqual(rc, 2)
        self.assertIn("no s.space hand-off", err)

    def test_off_the_cli_output_is_unchanged(self):
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", room=FakeRoom())
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^asked \w{8} as task-[0-9a-f]{18}\n$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
