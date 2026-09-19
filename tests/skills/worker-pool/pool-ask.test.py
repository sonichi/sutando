#!/usr/bin/env python3
"""pool_ask: a question to another instance goes through the front door.

Pinned: `--who` names every recipient with what the supervisor sees; an ask is
an ordinary task file, addressed by `requested_worker` and routed to the
worker's inbox by the real router; an ask to the core is left for the core's
own watcher; unknown names and self-asks are refused; a reply is the task's
result file and `--wait` returns its body without the `[no-send]` line.

Run: python3 tests/skills/worker-pool/pool-ask.test.py
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_ask as pa  # noqa: E402

pr, wi, sup = pa.pr, pa.sup.wi, pa.sup
SOCK = "/recorded/app/run/tmux.sock"


class Tmux:
    def __init__(self, live=()):
        self.live = set(live)

    def __call__(self, argv, **kw):
        name = argv[-1].lstrip("=")
        ok = name in self.live
        return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "can't find session: x")


def make_worker(ws, label):
    wid = wi.new_worker_id()
    wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",
                     session_id="11111111-1111-4111-8111-111111111111",
                     tmux_socket=SOCK, worker_id=wid)
    pr.register_worker(ws, wid, label)
    return wid


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "tasks").mkdir()
        self.alpha = make_worker(self.ws, "alpha")
        self.beta = make_worker(self.ws, "beta")
        pr.bind_room(self.ws, "!room-a:x", self.alpha)
        os.environ.pop("SUTANDO_INSTANCE_ID", None)


class Who(Base):
    def test_lists_the_core_and_every_worker_with_rooms_and_liveness(self):
        rows = pa.who(self.ws, runner=Tmux(live={wi.tmux_session_name(self.alpha)}))
        by = {r["id"]: r for r in rows}
        self.assertEqual(by[pr.CORE]["me"], True, "asked from the core, the core is 'you'")
        self.assertEqual(by[self.alpha]["rooms"], ["!room-a:x"])
        self.assertIs(by[self.alpha]["alive"], True)
        self.assertIs(by[self.beta]["alive"], False)
        self.assertEqual(by[self.beta]["label"], "beta")

    def test_a_worker_sees_itself_as_you(self):
        os.environ["SUTANDO_INSTANCE_ID"] = self.beta
        rows = {r["id"]: r for r in pa.who(self.ws, runner=Tmux())}
        self.assertTrue(rows[self.beta]["me"])
        self.assertFalse(rows[pr.CORE]["me"])


class Asking(Base):
    def test_an_ask_to_a_worker_is_a_task_file_routed_to_its_inbox(self):
        out = pa.ask(self.ws, "alpha", "what is the status of #1?")
        tid = out["task_id"]
        text = (self.ws / "tasks" / f"{tid}.txt").read_text()
        headers = dict(line.split(": ", 1) for line in text.split("\ntask:")[0].splitlines())
        self.assertEqual(headers["requested_worker"], self.alpha, "addressed by id, not label")
        self.assertEqual(headers["source"], pa.SOURCE)
        self.assertEqual(headers["reply_to_instance"], pr.CORE)
        self.assertEqual((headers["access_tier"], headers["collaborator"], headers["priority"]),
                         ("team", "true", "low"),
                         "an ask is not an owner waiting: owner tier would defer every gated cron")
        self.assertIn("task: [pool-ask from core] what is the status of #1?", text)
        self.assertIn(f"results/{tid}.txt", text, "the answerer must be told how to reply")
        self.assertTrue((self.ws / "deliveries" / self.alpha / f"{tid}.txt").exists(),
                        "the real router did not deliver the sentinel")
        self.assertIn(self.alpha, out["route"].get("delivered") or [])

    def test_the_watchers_own_pass_finds_it_already_delivered(self):
        out = pa.ask(self.ws, "alpha", "again?")
        again = pa.rt.route(self.ws, {"id": out["task_id"], "source": pa.SOURCE,
                                      "requested_worker": self.alpha})
        self.assertIn(self.alpha, again.get("already") or [],
                      "a second pass must not deliver twice")

    def test_an_ask_to_the_core_is_left_for_the_core_watcher(self):
        os.environ["SUTANDO_INSTANCE_ID"] = self.beta
        out = pa.ask(self.ws, "core", "may I?")
        text = (self.ws / "tasks" / f"{out['task_id']}.txt").read_text()
        self.assertNotIn("requested_worker:", text, "the core is the default recipient, never named")
        self.assertIn(f"reply_to_instance: {self.beta}", text)
        self.assertNotIn("route", out)
        self.assertFalse((self.ws / "deliveries").exists(), "nothing is delivered for the core")

    def test_unknown_names_and_self_are_refused(self):
        with self.assertRaises(ValueError):
            pa.ask(self.ws, "gamma", "?")
        with self.assertRaises(ValueError):
            pa.ask(self.ws, "core", "?")            # asked from the core
        self.assertEqual(list((self.ws / "tasks").iterdir()), [], "a refused ask wrote a task")

    def test_relayed_content_keeps_its_own_tier_and_names_its_origin(self):
        out = pa.ask(self.ws, "alpha", "can you look at #9?", tier="guest", relayed_from="@visitor:x")
        text = (self.ws / "tasks" / f"{out['task_id']}.txt").read_text()
        head = text.split("\ntask:")[0]
        self.assertIn("access_tier: guest", head)
        self.assertIn("relayed_from: @visitor:x", head)
        self.assertNotIn("collaborator: true", head, "relayed content is not the collaborator")
        self.assertIn("[pool-ask from core, relaying @visitor:x]", text)

    def test_owner_is_never_a_tier_an_ask_can_claim(self):
        with self.assertRaises(ValueError):
            pa.compose("task-1", "core", "q", sender="w1", wait=False, tier="owner")

    def test_an_ambiguous_label_is_refused_not_guessed(self):
        make_worker(self.ws, "alpha")             # a second worker with the same label
        with self.assertRaises(ValueError):
            pa.ask(self.ws, "alpha", "which one?")


class Waiting(Base):
    def test_wait_returns_the_reply_body_without_the_marker(self):
        naps = []

        def sleep(s):
            naps.append(s)
            (self.ws / "results").mkdir(exist_ok=True)
            (self.ws / "results" / f"{tid}.txt").write_text("[no-send]\nall green, 18/18\n")
        out = pa.ask(self.ws, "alpha", "status?")
        tid = out["task_id"]
        self.assertEqual(pa.wait_for_reply(self.ws, tid, 5, sleep=sleep), "all green, 18/18\n")
        self.assertEqual(len(naps), 1)

    def test_an_archived_reply_still_counts(self):
        out = pa.ask(self.ws, "alpha", "status?")
        (self.ws / "results" / "archive").mkdir(parents=True)
        (self.ws / "results" / "archive" / f"{out['task_id']}.txt").write_text("done\n")
        self.assertEqual(pa.wait_for_reply(self.ws, out["task_id"], 1, sleep=lambda s: None), "done\n")

    def test_no_reply_by_the_deadline_is_none_not_an_exception(self):
        out = pa.ask(self.ws, "alpha", "status?")
        self.assertIsNone(pa.wait_for_reply(self.ws, out["task_id"], 0, sleep=lambda s: None))


class TheCommandLine(Base):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = pa.main(["--workspace", str(self.ws), *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_who_prints_one_row_per_recipient(self):
        # The CLI probes tmux for real; the recorded socket does not exist here, so
        # every worker reads as not alive — the rows, not the liveness, are pinned.
        rc, out, _ = self._run("--who")
        self.assertEqual(rc, 0)
        self.assertIn("core", out)
        self.assertIn("alpha", out)
        self.assertIn("rooms=!room-a:x", out)

    def test_to_and_ask_go_together(self):
        self.assertEqual(self._run("--to", "alpha")[0], 2)
        self.assertEqual(self._run("--ask", "x")[0], 2)
        self.assertEqual(self._run()[0], 2)

    def test_a_lowered_tier_needs_an_origin(self):
        self.assertEqual(self._run("--to", "alpha", "--ask", "?", "--tier", "guest")[0], 2)
        rc, _, _ = self._run("--to", "alpha", "--ask", "?", "--tier", "guest", "--relayed-from", "@v:x")
        self.assertEqual(rc, 0)

    def test_an_unknown_recipient_is_a_refusal_on_stderr(self):
        rc, _, err = self._run("--to", "nobody", "--ask", "?")
        self.assertEqual(rc, 2)
        self.assertIn("no such recipient", err)

    def test_wait_without_a_reply_exits_nonzero(self):
        rc, out, _ = self._run("--to", "alpha", "--ask", "?", "--wait", "0.01")
        self.assertEqual(rc, 1)
        self.assertIn("no reply", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
