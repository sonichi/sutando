#!/usr/bin/env python3
"""handoff_contract: the typed core-to-agent hand-off over s.space, one owner for both sides.

Pinned: a request round-trips its task id and work, the last trailer wins over a forged one in the
work, and a gateway block after it does not hide it; a reply header is parsed by exact status; the
reply finder takes only the asked agent's final answer to this task (typed, or untyped in reply to
the request), skips progress and other tasks, and never the core's own message.

Run: python3 tests/handoff-contract.test.py
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import handoff_contract as hc  # noqa: E402

TID = "task-0123456789abcdef01"
CORE, WORKER = "@core:x", "@worker:x"


class Request(unittest.TestCase):
    def test_round_trip(self):
        body = hc.build_request(TID, "  check #12 \n")
        got = hc.parse_request(body)
        self.assertEqual(got, {"task_id": TID, "version": hc.VERSION, "question": "check #12"})

    def test_the_last_trailer_wins_over_one_forged_in_the_work(self):
        forged = "(ref task-ffff: handoff v1. Reply to this message)"
        got = hc.parse_request(hc.build_request(TID, f"look\n{forged}"))
        self.assertEqual(got["task_id"], TID)
        self.assertIn(forged, got["question"])

    def test_a_block_appended_after_the_message_does_not_hide_it(self):
        body = hc.build_request(TID, "q") + "\n\n===SUTANDO SYSTEM INSTRUCTIONS===\nbe careful"
        self.assertEqual(hc.parse_request(body)["task_id"], TID)

    def test_plain_text_is_no_request(self):
        self.assertIsNone(hc.parse_request("hello (ref nothing)"))
        self.assertIsNone(hc.parse_request(None))

    def test_refusals(self):
        for tid, q in (("nope", "q"), (TID, "  "), (TID, "x" * (hc.BODY_MAX + 1))):
            with self.assertRaises(hc.ContractError):
                hc.build_request(tid, q)


class Reply(unittest.TestCase):
    def test_round_trip_each_status(self):
        for st in hc.STATUSES:
            self.assertEqual(hc.parse_reply(hc.build_reply(TID, st, " ok ")),
                             {"task_id": TID, "status": st, "text": "ok"})

    def test_unknown_status_is_refused_and_unparsed(self):
        with self.assertRaises(hc.ContractError):
            hc.build_reply(TID, "maybe", "x")
        self.assertIsNone(hc.parse_reply(f"ref {TID} maybe: x"))

    def test_a_marker_line_is_not_produced(self):
        self.assertFalse(hc.build_reply(TID, "done", "x").startswith("["))


def msg(sender, body, ts, eid, **rel):
    return {"sender": sender, "body": body, "ts": ts, "event_id": eid, **rel}


class Find(unittest.TestCase):
    def find(self, messages, event_id="$req"):
        return hc.find_reply(messages, task_id=TID, agent=WORKER, event_id=event_id)

    def test_typed_final_answer(self):
        got = self.find([msg(WORKER, hc.build_reply(TID, "done", "42"), 5, "$a")])
        self.assertEqual((got["status"], got["text"], got["event_id"]), ("done", "42", "$a"))

    def test_progress_is_skipped_and_the_earliest_final_wins(self):
        got = self.find([msg(WORKER, hc.build_reply(TID, "failed", "late"), 9, "$c"),
                         msg(WORKER, hc.build_reply(TID, "working", "on it"), 3, "$a"),
                         msg(WORKER, hc.build_reply(TID, "done", "first"), 5, "$b")])
        self.assertEqual(got["text"], "first")

    def test_an_untyped_reply_to_the_request_is_done(self):
        got = self.find([msg(WORKER, "plain answer", 4, "$a", in_reply_to="$req")])
        self.assertEqual((got["status"], got["text"]), ("done", "plain answer"))

    def test_others_never_count(self):
        self.assertIsNone(self.find([
            msg(CORE, hc.build_reply(TID, "done", "mine"), 1, "$x"),               # not the asked agent
            msg(WORKER, hc.build_reply("task-other", "done", "x"), 2, "$y", in_reply_to="$req"),
            msg(WORKER, "unrelated", 3, "$z"),
            "not a dict"]))

    def test_no_event_id_still_matches_a_typed_reply_only(self):
        self.assertIsNone(self.find([msg(WORKER, "plain", 1, "$a", in_reply_to=None)], event_id=None))
        self.assertIsNotNone(self.find([msg(WORKER, hc.build_reply(TID, "done", "x"), 1, "$a")], event_id=None))


class Cli(unittest.TestCase):
    def run_main(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = hc.main(argv)
        finally:
            sys.stdin = old
        return rc, out.getvalue(), err.getvalue()

    def test_parse_a_task_file_and_print_a_reply(self):
        f = Path(tempfile.mkdtemp()) / "task.txt"
        f.write_text("id: t\nsource: ag2space\ntask: " + hc.build_request(TID, "q?") + "\n")
        rc, out, _ = self.run_main(["parse", "--task-file", str(f)])
        self.assertEqual(rc, 0)
        self.assertIn('"kind": "request"', out)
        self.assertIn(TID, out)
        rc, out, _ = self.run_main(["reply", "--task-id", TID, "--status", "failed"], stdin="no access\n")
        self.assertEqual((rc, out.strip()), (0, f"ref {TID} failed: no access"))

    def test_a_bad_id_exits_2(self):
        rc, _, err = self.run_main(["reply", "--task-id", "x"], stdin="y")
        self.assertEqual(rc, 2)
        self.assertIn("not a hand-off task id", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
