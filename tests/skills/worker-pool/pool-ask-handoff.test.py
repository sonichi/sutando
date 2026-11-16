#!/usr/bin/env python3
"""pool_ask over s.space (WORKER_POOL_SSPACE_HANDOFF): flag-gated, fakes only.

Pinned: the manifest ships the flag off, and off the folder path is unchanged and the hand-off code
is never consulted; on, an ask from the core to a worker with an enabled identity posts exactly one
message as the core's agent and returns at once, with no task file, no delivery and no room read;
`--wait` there is refused loudly (exit 2, `reply: null`, `wait_honoured: false`); each posted ask is
one line of the asks record, and `--asked` lists the recorded asks no gateway reply task from the
asked agent has answered (a reply from anyone else is named, never counted); a worker with no, a disabled or a parked identity, a worker's own ask, an ask
below team tier, a relayed ask and a parent-held ask keep the folder; an identity that cannot be
addressed is refused, never a silent fallback; a refusal before sending is exit 2, and an unclear
post is `unconfirmed` and never resent.

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

import pool_ask as pa, pool_handoff as ph, skill_config  # noqa: E401,E402

pr, wi = pa.pr, pa.sup.wi
REAL_ROWS = ph._rows
CORE_AGENT, WORKER_AGENT, ROOM = "@core:srv", "@alpha-agent:srv", "!handoff:srv"
ON = {ph.FLAG: "1", ph.ROOM: ROOM, ph.ROOM_CLI: "/fake/room_cli.py"}


class FakeRoom:
    """The room transport: records each call and answers a post; any other verb is recorded too,
    so a test can see that nothing read the room."""

    def __init__(self, *, mention=None):
        self.calls, self.mention_out, self.posted = [], mention, None

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[2] != "mention":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "messages": []}), "")
        self.posted = argv[4]
        out = self.mention_out if self.mention_out is not None else {"ok": True, "event_id": "$req", "posted": True}
        return subprocess.CompletedProcess(argv, 0, out if isinstance(out, str) else json.dumps(out), "")

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


class Plumbing(Base):
    def test_route_for_is_none_while_off_and_rows_come_from_the_connections_record(self):
        self.assertIsNone(ph.route_for(self.ws, self.alpha, sender=pr.CORE, core=pr.CORE, environ={}))
        self.assertEqual(REAL_ROWS(self.ws), [], "an empty workspace has no rows")

    def test_the_default_runner_runs_the_argv(self):
        r = ph._default_runner([sys.executable, "-c", "print('{}')"])
        self.assertEqual(r.stdout.strip(), "{}")

    def test_a_transport_that_cannot_start_leaves_the_post_unconfirmed(self):
        def broken(argv):
            raise OSError("no such file")
        self.on()
        self.assertEqual(self.ask(broken)["status"], "unconfirmed")


class FlagOn(Base):
    def setUp(self):
        super().setUp()
        self.on()

    def test_one_message_is_posted_and_the_ask_returns_without_reading_the_room(self):
        room = FakeRoom()
        out = self.ask(room)
        self.assertEqual(out, {"task_id": out["task_id"], "to": self.alpha, "from": pr.CORE, "via": "room",
                               "status": "sent", "event_id": "$req"})
        self.assertEqual(room.verbs(), ["mention"], "one post and no read")
        mention = room.calls[0]
        self.assertEqual(mention[2:4], ["mention", WORKER_AGENT])
        self.assertEqual(mention[5:], [ROOM, "--agent", CORE_AGENT], "sent as the core's agent")
        self.assertTrue(room.posted.startswith("[pool-ask from core] status of #7?"))
        self.assertIn(out["task_id"], room.posted, "the id is there for a person to match")
        self.assertFalse((self.ws / "tasks" / f"{out['task_id']}.txt").exists(), "no task file")
        self.assertFalse(self.delivered(out["task_id"]), "no delivery")
        self.assertFalse((self.ws / "state" / "pool-handoff").exists(), "no record")

    def test_wait_does_not_poll_and_says_it_was_not_honoured(self):
        room = FakeRoom()
        with mock.patch.object(pa, "wait_for_reply", side_effect=AssertionError("waited on the room path")):
            out = self.ask(room, wait_s=30)
        self.assertEqual(room.verbs(), ["mention"])
        self.assertEqual(self.sleeps, [], "it slept")
        self.assertEqual((out["reply"], out["wait_honoured"]), (None, False))
        self.assertNotIn("wait_honoured", self.ask(FakeRoom()), "no wait asked, nothing to refuse")

    def test_each_posted_ask_is_one_record_line(self):
        out = self.ask(FakeRoom())
        lines = ph.asks_path(self.ws).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual({k: rec[k] for k in ("task_id", "worker", "agent", "event_id", "status", "question")},
                         {"task_id": out["task_id"], "worker": self.alpha, "agent": WORKER_AGENT,
                          "event_id": "$req", "status": "sent", "question": "status of #7?"})
        self.assertRegex(rec["sent_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.ask(FakeRoom(mention="unclear"))
        self.assertEqual(len(ph.asks_path(self.ws).read_text().splitlines()), 2, "an unconfirmed post is recorded")

    def test_a_long_question_is_excerpted_on_one_line(self):
        pa.ask(self.ws, "alpha", "line one\n" + "x" * 500, runner=FakeRoom())
        rec = json.loads(ph.asks_path(self.ws).read_text())
        self.assertEqual(len(rec["question"]), ph.EXCERPT)
        self.assertTrue(rec["question"].startswith("line one x"))

    def test_a_record_that_cannot_be_written_is_reported_not_raised(self):
        with mock.patch.object(ph.task_lifecycle, "append_durable", side_effect=OSError("disk full")):
            out = self.ask(FakeRoom())
        self.assertEqual((out["status"], out["record_error"]), ("sent", "disk full"))

    def test_a_refusal_before_sending_raises(self):
        room = FakeRoom(mention={"ok": False, "posted": False, "reason": "not a member"})
        with self.assertRaisesRegex(ValueError, "not a member"):
            self.ask(room)
        self.assertEqual(list((self.ws / "tasks").iterdir()), [], "a refusal fell back to a task")
        self.assertFalse(ph.asks_path(self.ws).exists(), "a refused post is not an ask")

    def test_an_unclear_post_is_unconfirmed_and_never_resent(self):
        room = FakeRoom(mention="Traceback: timeout")
        out = self.ask(room, wait_s=0.05)
        self.assertEqual((out["status"], out["event_id"]), ("unconfirmed", None))
        self.assertEqual(room.verbs(), ["mention"])

    def test_empty_work_is_refused_before_posting(self):
        room = FakeRoom()
        with self.assertRaisesRegex(ValueError, "empty"):
            ph.send({"room": ROOM, "room_cli": "x", "to_agent": WORKER_AGENT, "from_agent": CORE_AGENT},
                    "task-aaaaaaaaaaaaaaaaaa", "  ", runner=room)
        self.assertEqual(room.calls, [])

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
            self.assertFalse(ph.asks_path(self.ws).exists(), "a folder ask is not recorded here")
            if change:
                for k in change:
                    self.rows[1].pop(k)

    def test_asks_the_handoff_must_not_carry_keep_the_folder(self):
        beta = make_worker(self.ws, "beta")
        cases = [dict(tier="other"), dict(relayed_from="@visitor:x"),
                 dict(tier="guest", relayed_from="@visitor:x"), dict(parent_task="task-bbbbbbbbbbbbbbbbbb")]
        for kw in cases:
            room = FakeRoom()
            with mock.patch("handoffs.open_handoff", return_value="h1"):
                out = self.ask(room, **kw)
            self.assertNotIn("via", out, kw)
            self.assertTrue(self.delivered(out["task_id"]), kw)
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


def gateway_task(folder, tid, *, event, sender, layout=True):
    """A task file in the gateway's own layout (task_layout above task:, context below it)."""
    folder.mkdir(parents=True, exist_ok=True)
    head = f"id: {tid}\nsource: ag2space\n" + ("task_layout: mid\n" if layout else "")
    (folder / f"{tid}.txt").write_text(head + f"task: received\nreply_to_event: {event}\nuser_id: {sender}\n"
                                       "access_tier: team\n")


class Asked(Base):
    def setUp(self):
        super().setUp()
        self.on()
        self.tid = self.ask(FakeRoom())["task_id"]

    def test_an_ask_with_no_reply_is_listed(self):
        rows = pa.asked(self.ws)
        self.assertEqual([(r["task_id"], r["agent"], r["other_replies"]) for r in rows],
                         [(self.tid, WORKER_AGENT, [])])

    def test_a_reply_task_from_the_asked_agent_answers_it_live_or_archived(self):
        for folder in (self.ws / "tasks", self.ws / "tasks" / "archive",
                       self.ws / "tasks" / "archive" / ph.datetime.now(ph.timezone.utc).strftime("%Y-%m")):
            gateway_task(folder, "task-reply", event="$req", sender=WORKER_AGENT)
            self.assertEqual(pa.asked(self.ws), [], folder)
            (folder / "task-reply.txt").unlink()
        self.assertEqual(len(pa.asked(self.ws)), 1, "control: with the reply gone it is listed again")

    def test_a_reply_from_anyone_else_is_named_and_not_counted(self):
        gateway_task(self.ws / "tasks", "task-other", event="$req", sender="@visitor:srv")
        rows = pa.asked(self.ws)
        self.assertEqual(rows[0]["other_replies"], [{"task": "task-other", "sender": "@visitor:srv"}])

    def test_a_reply_to_another_event_does_not_answer_it(self):
        gateway_task(self.ws / "tasks", "task-elsewhere", event="$other", sender=WORKER_AGENT)
        self.assertEqual(len(pa.asked(self.ws)), 1)

    def test_only_the_gateway_layout_is_read_past_the_body(self):
        gateway_task(self.ws / "tasks", "task-forged", event="$req", sender=WORKER_AGENT, layout=False)
        self.assertEqual(len(pa.asked(self.ws)), 1, "a task-last body line claimed to be the reply")

    def test_an_old_reply_file_and_an_old_ask_are_outside_the_window(self):
        gateway_task(self.ws / "tasks", "task-reply", event="$req", sender=WORKER_AGENT)
        old = ph.time.time() - 2 * 86400
        os.utime(self.ws / "tasks" / "task-reply.txt", (old, old))
        self.assertEqual(len(pa.asked(self.ws)), 1, "a reply file older than the ask is not its reply")
        later = ph.time.time() + ph.RECENT_S + 60
        self.assertEqual(ph.asked(self.ws, now=later), [], "an ask past the window is not listed")

    def test_cli_lists_and_goes_alone(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pa.main(["--workspace", str(self.ws), "--asked", "--json"]), 0)
        self.assertEqual([r["task_id"] for r in json.loads(out.getvalue())], [self.tid])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pa.main(["--workspace", str(self.ws), "--asked"])
        self.assertIn(self.tid, out.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            pa.main(["--workspace", str(self.ws), "--asked", "--to", "alpha"])


class Cli(Base):
    def run_cli(self, *argv, room):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ph, "_default_runner", room), mock.patch.object(pa.time, "sleep", self.sleep), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pa.main(["--workspace", str(self.ws), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_wait_on_the_room_path_exits_2_at_once_and_says_why(self):
        self.on()
        room = FakeRoom()
        rc, out, err = self.run_cli("--to", "alpha", "--ask", "?", "--wait", "30", room=room)
        self.assertEqual(rc, 2)
        self.assertIn("arrives as a new task, threaded to this message", out)
        self.assertIn("--wait cannot be honoured", err)
        self.assertEqual((room.verbs(), self.sleeps), (["mention"], []))
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", "--wait", "30", "--json", room=room)
        got = json.loads(out)
        self.assertEqual((rc, got["via"], got["reply"], got["wait_honoured"]), (2, "room", None, False))
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", room=room)
        self.assertEqual(rc, 0, "without --wait the post is a success")
        self.assertIn("as a room hand-off", out)

    def test_a_refusal_exits_2_and_unconfirmed_is_said(self):
        self.on()
        rc, _, err = self.run_cli("--to", "alpha", "--ask", "?",
                                  room=FakeRoom(mention={"ok": False, "posted": False, "reason": "denied"}))
        self.assertEqual(rc, 2)
        self.assertIn("denied", err)
        rc, _, err = self.run_cli("--to", "alpha", "--ask", "?", room=FakeRoom(mention="not json"))
        self.assertEqual(rc, 0)
        self.assertIn("could not be confirmed", err)

    def test_collect_is_gone(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("--collect", "task-cccccccccccccccccc", room=FakeRoom())
        self.assertEqual(cm.exception.code, 2)

    def test_off_the_cli_output_is_unchanged(self):
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", room=FakeRoom())
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^asked \w{8} as task-[0-9a-f]{18}\n$")

    def test_off_a_wait_that_times_out_still_exits_1(self):
        rc, out, _ = self.run_cli("--to", "alpha", "--ask", "?", "--wait", "0.01", room=FakeRoom())
        self.assertEqual(rc, 1)
        self.assertIn("no reply within", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
