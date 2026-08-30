#!/usr/bin/env python3
"""In-process cover for notify_reviewers' ledger, actor-map and widen gate.

The sibling suite drives the script as a SUBPROCESS, which exercises the CLI
but is invisible to coverage — the gate saw 5.8% on this file while every
behaviour was tested. These call the functions directly.

Run: python3 tests/sci-notify-reviewers-inproc.test.py   (stdlib only)
"""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")


def _load():
    spec = importlib.util.spec_from_file_location("_nr_inproc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(minutes_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.isoformat().replace("+00:00", "Z")


class ActorMap(unittest.TestCase):
    """One human can hold several roster keys; the map must fold a whole
    connected component, not one hop."""

    def setUp(self):
        self.mod = _load()

    def test_a_chain_folds_to_one_actor(self):
        # a<->b and c->b: following one hop sends c to `b` and lists a human twice.
        roster = {"a": {"same_actor_as": "b"}, "b": {"same_actor_as": "a"},
                  "c": {"same_actor_as": "b"}}
        m = self.mod._actor_map(roster)
        self.assertEqual(len({m["a"], m["b"], m["c"]}), 1)

    def test_disjoint_pairs_stay_disjoint(self):
        roster = {"a": {"same_actor_as": "b"}, "b": {"same_actor_as": "a"},
                  "x": {"same_actor_as": "y"}, "y": {"same_actor_as": "x"}}
        m = self.mod._actor_map(roster)
        self.assertNotEqual(m["a"], m["x"])

    def test_a_dangling_reference_does_not_crash(self):
        m = self.mod._actor_map({"a": {"same_actor_as": "ghost"}})
        self.assertEqual(m["a"], m["ghost"])

    def test_underscore_keys_and_non_dicts_are_skipped(self):
        m = self.mod._actor_map({"_meta": {"same_actor_as": "a"}, "note": "text",
                                 "a": {}})
        self.assertNotIn("_meta", m)
        self.assertIn("a", m)

    def test_an_unlinked_key_is_its_own_actor(self):
        self.assertEqual(self.mod._actor_map({"solo": {}})["solo"], "solo")


class Ledger(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _at(self, root):
        return patch.object(self.mod, "ledger_path",
                            return_value=Path(root) / "state" / "review-asks.jsonl")

    def test_ledger_path_hangs_off_the_resolved_workspace(self):
        with patch.dict("sys.modules"):
            p = self.mod.ledger_path()
        self.assertEqual(p.name, "review-asks.jsonl")
        self.assertEqual(p.parent.name, "state")

    def test_an_ask_is_recorded_and_readable(self):
        with self._at(self.tmp.name):
            n = self.mod.record_asks(
                "see https://github.com/o/r/pull/42", "rui")
            self.assertEqual(n, 1)
            rows = [json.loads(x) for x in
                    self.mod.ledger_path().read_text().splitlines() if x.strip()]
        self.assertEqual(rows[0]["reviewer"], "rui")
        self.assertEqual(str(rows[0]["pr"]), "42")

    def test_a_message_naming_no_PR_records_nothing(self):
        with self._at(self.tmp.name):
            self.assertEqual(self.mod.record_asks("no link here", "rui"), 0)

    def test_zero_is_returned_ONLY_when_no_PR_URL_matched(self):
        # main() names this cause in its warning, so a future early `return 0`
        # would make that message assert something false while tests still pass.
        cases = [
            ("https://github.com/o/r/pull/1", 1),
            ("https://github.com/o/r/pull/1 https://github.com/o/r/pull/2", 2),
            ("https://github.com/o/r/pull/1/files", 1),
            ("[#1](https://github.com/o/r/pull/1)", 1),
            ("o/r#1", 0),
            ("#1", 0),
            ("https://api.github.com/repos/o/r/pulls/1", 0),
            ("no link here", 0),
            ("", 0),
        ]
        for msg, want in cases:
            with self._at(self.tmp.name):
                got = self.mod.record_asks(msg, "rui")
            matched = self.mod._PR_URL.search(msg) is not None
            self.assertEqual(got, want, msg)
            self.assertEqual(got == 0, not matched, f"0-iff-no-match broken for {msg!r}")

    def test_two_PRs_in_one_message_record_both(self):
        msg = ("https://github.com/o/r/pull/1 and "
               "https://github.com/o/r/pull/2")
        with self._at(self.tmp.name):
            self.assertEqual(self.mod.record_asks(msg, "rui"), 2)


class WidenGate(unittest.TestCase):
    """Refuse re-asking the SAME non-responders; never refuse a genuine widen."""

    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "review-asks.jsonl"
        self.roster = {"rui": {}, "mark": {}, "qingyun": {}}

    def _seed(self, rows):
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def _ask(self, targets, msg="https://github.com/o/r/pull/7"):
        with patch.object(self.mod, "ledger_path", return_value=self.ledger):
            return self.mod._stale_repeat_ask(msg, targets, self.roster)

    def test_no_PR_in_the_message_never_refuses(self):
        self.assertFalse(self._ask([{"name": "rui"}], "no link")[0])

    def test_an_absent_ledger_never_refuses(self):
        self.assertFalse(self._ask([{"name": "rui"}])[0])

    def test_a_fresh_ask_is_not_stale(self):
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(5)}])
        self.assertFalse(self._ask([{"name": "rui"}])[0])

    def test_an_old_ask_to_the_same_name_refuses(self):
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}])
        refuse, why = self._ask([{"name": "rui"}])
        self.assertTrue(refuse)
        self.assertIn("already asked", why)

    def test_the_refusal_does_not_assert_review_state(self):
        # It reads the ledger only; claiming "none has reviewed" was false while
        # an APPROVED review was in.
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}])
        why = self._ask([{"name": "rui"}])[1]
        self.assertNotIn("none has reviewed", why)
        self.assertIn("review state not", why)

    def test_one_NEW_name_makes_it_a_widen_not_a_repeat(self):
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}])
        self.assertFalse(self._ask([{"name": "rui"}, {"name": "mark"}])[0])

    def test_the_unasked_list_names_who_is_left(self):
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}])
        why = self._ask([{"name": "rui"}])[1]
        self.assertIn("mark", why)
        self.assertIn("qingyun", why)

    def test_a_different_PR_in_the_ledger_is_not_this_PR(self):
        self._seed([{"repo": "o/r", "pr": "999", "reviewer": "rui", "ts": _iso(90)}])
        self.assertFalse(self._ask([{"name": "rui"}])[0])

    def test_a_corrupt_ledger_line_is_skipped_not_fatal(self):
        self.ledger.write_text("not json\n" + json.dumps(
            {"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}) + "\n")
        self.assertTrue(self._ask([{"name": "rui"}])[0])

    def test_keweichen_is_never_offered_as_the_widen_target(self):
        self.roster["keweichen"] = {}
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}])
        self.assertNotIn("keweichen", self._ask([{"name": "rui"}])[1])

    def test_two_keys_for_one_human_are_offered_once(self):
        self.roster = {"rui": {}, "jsun": {"same_actor_as": "johnm"},
                       "johnm": {"same_actor_as": "jsun"}}
        self._seed([{"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": _iso(90)}])
        why = self._ask([{"name": "rui"}])[1]
        self.assertEqual(sum(w in why for w in ("jsun", "johnm")), 1)


class MainInProcess(unittest.TestCase):
    """main()'s refusal ORDER and exit codes, called directly — the subprocess
    suite proves the same behaviour but is invisible to the coverage gate."""

    GOOD = {"rui": {"human": "@rui:x", "stand": "@sutando-rui:x",
                    "room": "!triage:x", "allowlisted": True},
            "mark": {"human": "@mark:x", "stand": "@mark-stand:x",
                     "room": "!triage:x", "allowlisted": True}}

    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, argv, roster=None, members_ok=True):
        led = Path(self.tmp.name) / "l.jsonl"
        ok = json.dumps({"ok": True, "members": [
            {"user_id": "@sutando-rui:x"}, {"user_id": "@mark-stand:x"}]})
        sent = json.dumps({"ok": True, "event_id": "$e"})

        def fake_run(cmd, *a, **k):
            out = ok if "members" in cmd else sent
            return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()

        with patch.object(self.mod, "load_roster",
                          return_value=roster if roster is not None else self.GOOD), \
             patch.object(self.mod, "ledger_path", return_value=led), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch("sys.argv", ["notify_reviewers.py"] + argv):
            return self.mod.main()

    M = ["--message", "re-review https://github.com/o/r/pull/7"]

    def test_unknown_name_exits_2_not_the_count_code(self):
        # The specific cause must survive the >=2 gate: 5 sends the caller to
        # add a reviewer when the real problem is a typo.
        self.assertEqual(self._run(["--reviewers", "ghost", "--send"] + self.M), 2)

    def test_human_only_entry_exits_3(self):
        r = {"kim": {"human": "@kim:x", "room": "!r:x"}}
        self.assertEqual(self._run(["--reviewers", "kim", "--send"] + self.M, r), 3)

    def test_off_allowlist_exits_4(self):
        r = {"mini": {"stand": "@mini:x", "room": "!r:x", "allowlisted": False}}
        self.assertEqual(self._run(["--reviewers", "mini", "--send"] + self.M, r), 4)

    def test_one_reviewer_on_a_real_send_exits_5(self):
        self.assertEqual(self._run(["--reviewers", "rui", "--send"] + self.M), 5)

    def test_plan_mode_is_exempt_from_the_count_gate(self):
        # Nothing is sent, so one reviewer cannot strand a PR.
        self.assertEqual(self._run(["--reviewers", "rui"] + self.M), 0)

    def test_allow_single_permits_a_one_reviewer_send(self):
        self.assertEqual(
            self._run(["--reviewers", "rui", "--send",
                       "--allow-single", "reason"] + self.M), 0)

    def test_two_reviewers_send_cleanly(self):
        self.assertEqual(self._run(["--reviewers", "rui,mark", "--send"] + self.M), 0)

    def test_a_repeat_ask_after_the_window_exits_6(self):
        led = Path(self.tmp.name) / "l.jsonl"
        led.write_text("".join(json.dumps(
            {"repo": "o/r", "pr": "7", "reviewer": n, "ts": _iso(90)}) + "\n"
            for n in ("rui", "mark")))
        self.assertEqual(self._run(["--reviewers", "rui,mark", "--send"] + self.M), 6)

    def test_widen_override_defeats_the_repeat_gate(self):
        led = Path(self.tmp.name) / "l.jsonl"
        led.write_text("".join(json.dumps(
            {"repo": "o/r", "pr": "7", "reviewer": n, "ts": _iso(90)}) + "\n"
            for n in ("rui", "mark")))
        self.assertEqual(
            self._run(["--reviewers", "rui,mark", "--send",
                       "--widen-override", "reason"] + self.M), 0)


class FailurePaths(unittest.TestCase):
    """The error branches. rui's blocker on this PR was that one raising target
    must not drop the rest of the batch, and a lost ledger write must be loud."""

    GOOD = MainInProcess.GOOD

    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = Path(self.tmp.name) / "l.jsonl"

    def _run(self, argv, send_effect=None, record_effect=None):
        ok = json.dumps({"ok": True, "members": [
            {"user_id": "@sutando-rui:x"}, {"user_id": "@mark-stand:x"}]})
        sent = json.dumps({"ok": True, "event_id": "$e"})
        calls = {"n": 0}

        def fake_run(cmd, *a, **k):
            if "members" in cmd:
                return type("R", (), {"stdout": ok, "stderr": "", "returncode": 0})()
            calls["n"] += 1
            if send_effect and calls["n"] == 1:
                raise send_effect
            return type("R", (), {"stdout": sent, "stderr": "", "returncode": 0})()

        ctx = [patch.object(self.mod, "load_roster", return_value=self.GOOD),
               patch.object(self.mod, "ledger_path", return_value=self.led),
               patch.object(self.mod.subprocess, "run", side_effect=fake_run),
               patch("sys.argv", ["notify_reviewers.py"] + argv)]
        if record_effect:
            ctx.append(patch.object(self.mod, "record_asks", side_effect=record_effect))
        for c in ctx:
            c.start()
        try:
            return self.mod.main(), calls["n"]
        finally:
            for c in reversed(ctx):
                c.stop()

    M = ["--message", "re-review https://github.com/o/r/pull/7"]
    ARGS = ["--reviewers", "rui,mark", "--send"]

    def test_a_timeout_on_one_target_still_sends_the_other(self):
        rc, sends = self._run(self.ARGS + self.M,
                              send_effect=self.mod.subprocess.TimeoutExpired("x", 60))
        self.assertEqual(sends, 2)     # the second target was still attempted
        self.assertNotEqual(rc, 0)     # and the failure is not swallowed

    def test_an_OSError_on_one_target_still_sends_the_other(self):
        rc, sends = self._run(self.ARGS + self.M, send_effect=OSError("no exec"))
        self.assertEqual(sends, 2)
        self.assertNotEqual(rc, 0)

    def test_a_lost_ledger_write_is_loud_but_not_fatal_to_the_batch(self):
        # The ask is already delivered when record_asks raises; aborting would
        # neither un-send it nor record it.
        rc, sends = self._run(self.ARGS + self.M, record_effect=OSError("read-only"))
        self.assertEqual(sends, 2)
        self.assertNotEqual(rc, 0)

    def test_a_delivered_ask_that_records_NOTHING_is_loud(self):
        # Ask delivered, ledger empty -- the OSError case reached by a return
        # value. Silent before the fix; measured on ag2space-backend#872.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, sends = self._run(self.ARGS + self.M,
                                  record_effect=lambda *a, **k: 0)
        self.assertEqual(sends, 2)
        self.assertIn("nothing recorded", err.getvalue())
        self.assertEqual(rc, 0)                 # a PR-less ask is not a failure

    def test_an_unreadable_ledger_does_not_refuse_the_ask(self):
        with patch.object(self.mod, "ledger_path", return_value=self.led), \
             patch.object(Path, "read_text", side_effect=OSError("boom")):
            refuse, _ = self.mod._stale_repeat_ask(
                "https://github.com/o/r/pull/7", [{"name": "rui"}], {"rui": {}})
        self.assertFalse(refuse)       # fails OPEN: a notifier must not block on its own bug

    def test_an_unparseable_timestamp_does_not_refuse_the_ask(self):
        self.led.write_text(json.dumps(
            {"repo": "o/r", "pr": "7", "reviewer": "rui", "ts": "not-a-date"}) + "\n")
        with patch.object(self.mod, "ledger_path", return_value=self.led):
            refuse, _ = self.mod._stale_repeat_ask(
                "https://github.com/o/r/pull/7", [{"name": "rui"}], {"rui": {}})
        self.assertFalse(refuse)


if __name__ == "__main__":
    unittest.main(verbosity=1)
