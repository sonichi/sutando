#!/usr/bin/env python3
"""An ask naming a PR in shorthand must be refused BEFORE it is sent.

`record_asks()` logs only full github.com/<owner>/<repo>/pull/<n> URLs. Every
other shape -- `#3760`, `sonichi/sutando#3622` -- delivers cleanly and records
nothing, so pr-unattended then reads a correctly-asked PR as NOBODY_EVER_ASKED.
#3562 made that loud, but the warning prints AFTER the send: it names a loss it
cannot prevent. The same note was filed twice in three days by an agent who did
not remember filing it the first time, which is the point-of-use guard failing
at its own job rather than a discipline problem.

Both polarities are asserted. Refusing shorthand is worthless if it also refuses
the legitimate ask that names no PR at all, because a guard a caller cannot
satisfy gets routed around and then there is neither guard nor warning.

Run: python3 tests/sci-notify-reviewers-shorthand-refusal.test.py   (stdlib only)
"""
import importlib.util
import io
import json
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
URL = "https://github.com/sonichi/sutando/pull/4242"


def _load():
    spec = importlib.util.spec_from_file_location("_nr_short", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Sent:
    returncode = 0
    stdout = json.dumps({"ok": True, "event_id": "$stub"})
    stderr = ""


class Shorthand(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.ledger = d / "review-asks.jsonl"
        roster = {k: {"stand": f"@{k}-stand:x", "room": "!r:x", "human": f"@{k}:x"}
                  for k in ("alice", "bob")}
        self.roster_file = d / "roster.json"
        self.roster_file.write_text(json.dumps(roster))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, message, extra=None):
        def fake_run(args, **kw):
            self.calls.append(args)
            return _Sent()
        argv = ["--reviewers", "alice,bob", "--message", message, "--send"] + (extra or [])
        err = io.StringIO()
        with patch.object(self.mod, "ledger_path", lambda: self.ledger), \
             patch.object(self.mod, "roster_path", lambda: self.roster_file), \
             patch.object(self.mod.subprocess, "run", fake_run), \
             patch.object(self.mod.sys, "argv", ["notify_reviewers.py"] + argv), \
             contextlib.redirect_stderr(err):
            rc = self.mod.main()
        return rc, err.getvalue()

    # --- the helper, in isolation ----------------------------------------

    def test_a_bare_hash_number_is_unrecordable(self):
        out = self.mod.unrecordable_pr_refs("please review #3760 today")
        self.assertEqual([t for t, _ in out], ["#3760"])
        self.assertEqual(out[0][1], "https://github.com/<owner>/<repo>/pull/3760")

    def test_owner_repo_shorthand_yields_the_EXACT_url(self):
        out = self.mod.unrecordable_pr_refs("see sonichi/sutando#3622")
        self.assertEqual(out[0][1], "https://github.com/sonichi/sutando/pull/3622")

    def test_a_full_url_is_recordable(self):
        self.assertEqual(self.mod.unrecordable_pr_refs(f"review {URL}"), [])

    def test_a_message_naming_no_pr_is_not_flagged(self):
        self.assertEqual(self.mod.unrecordable_pr_refs("can you look at the roster?"), [])

    def test_shorthand_for_a_pr_already_named_by_url_is_not_flagged(self):
        self.assertEqual(self.mod.unrecordable_pr_refs(f"{URL} — i.e. #4242"), [])

    def test_a_url_to_ANOTHER_repo_does_not_cover_a_colliding_number(self):
        # Low PR numbers collide across repos constantly, so keying on the number
        # alone let this guard's own failure mode arrive through the guard.
        out = self.mod.unrecordable_pr_refs(
            "https://github.com/ag2-space/backend/pull/12 and sonichi/sutando#12")
        self.assertEqual([t for t, _ in out], ["sonichi/sutando#12"])
        self.assertEqual(out[0][1], "https://github.com/sonichi/sutando/pull/12")

    def test_CONTROL_a_url_and_the_SAME_repo_shorthand_is_still_allowed(self):
        msg = "https://github.com/sonichi/sutando/pull/12 and sonichi/sutando#12"
        self.assertEqual(self.mod.unrecordable_pr_refs(msg), [],
                         "keying on the pair must not start refusing the same-repo case")

    def test_a_single_digit_hash_is_prose_not_a_pr(self):
        self.assertEqual(self.mod.unrecordable_pr_refs("point #1 is wrong"), [])

    # --- the refusal, through main() -------------------------------------

    def test_an_ask_naming_a_pr_in_shorthand_is_REFUSED_before_sending(self):
        rc, err = self._run("please review #3760")
        self.assertEqual(rc, 7)
        self.assertEqual(self.calls, [], "refused ask must not reach room_ops")
        self.assertEqual(self._ledger(), [], "nothing recorded either")

    def test_the_refusal_says_what_it_refused_AND_what_would_satisfy_it(self):
        _, err = self._run("please review sonichi/sutando#3622")
        self.assertIn("sonichi/sutando#3622", err)
        self.assertIn("https://github.com/sonichi/sutando/pull/3622", err)

    def test_CONTROL_a_full_url_ask_sends(self):
        rc, _ = self._run(f"please review {URL}")
        self.assertEqual(rc, 0)
        self.assertGreater(len(self.calls), 0, 'the ask must actually be sent')

    def test_CONTROL_an_ask_with_no_pr_at_all_still_sends(self):
        rc, _ = self._run("can you look at the roster when you get a moment?")
        self.assertEqual(rc, 0, "an ask need not concern a PR; refusing it breaks the tool")
        self.assertGreater(len(self.calls), 0, 'the ask must actually be sent')

    def test_a_notice_is_not_refused(self):
        rc, _ = self._run("heads up on #3760", extra=["--kind", "notice"])
        self.assertEqual(rc, 0, "a notice records nothing by design, so it cannot lose a record")

    def _ledger(self):
        if not self.ledger.exists():
            return []
        return [x for x in self.ledger.read_text().splitlines() if x.strip()]


if __name__ == "__main__":
    unittest.main(verbosity=2)
