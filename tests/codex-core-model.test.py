#!/usr/bin/env python3
"""src/codex_core_model.py: the Codex core's own model record, its name rule, Codex's model list and
the home it is read from. Core-only: nothing here writes the shared Codex home."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import codex_core_model as ccm  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ccm.main(list(argv))
        return rc, out.getvalue().strip()


class Names(Case):
    def test_codex_slugs_pass_and_claude_names_flags_and_shell_text_do_not(self):
        for good in ("gpt-6-luna", "gpt-5.6-terra", "o4_mini"):
            self.assertTrue(ccm.valid(good), good)
        for bad in ("", None, "opus", "Sonnet[1m]", "default", "claude-opus-5", "-m", "gpt; rm", "a" * 81):
            self.assertFalse(ccm.valid(bad), bad)


class Record(Case):
    def test_save_keeps_the_previous_codex_pick_and_refuses_bad_input(self):
        first = ccm.save(self.state, "gpt-a", applied="next-start", accepted=False, now=0)
        self.assertEqual((first["previous"], first["previous_source"], first["ts"]), (None, "none", "1970-01-01T00:00:00Z"))
        second = ccm.save(self.state, "gpt-b", applied="next-turn", accepted=True)
        self.assertEqual((second["previous"], second["previous_source"], second["runtime"]), ("gpt-a", "last-record", "codex"))
        self.assertEqual(ccm.current(self.state), "gpt-b")
        with self.assertRaises(ValueError):
            ccm.save(self.state, "opus", applied="next-turn", accepted=True)
        with self.assertRaises(ValueError):
            ccm.save(self.state, "gpt-c", applied="now", accepted=True)
        self.assertEqual(ccm.current(self.state), "gpt-b", "a refused save writes nothing")

    def test_a_missing_unreadable_or_claude_record_names_no_model(self):
        self.assertEqual(ccm.current(self.state), "")
        self.state.mkdir()
        for text in ("not json", "[1]", json.dumps({"model": "opus"}), json.dumps({"model": "-x", "runtime": "codex"})):
            ccm.record_path(self.state).write_text(text)
            self.assertEqual(ccm.current(self.state), "", text)

    def test_each_save_is_one_revision_and_a_seat_pick_is_pending_on_the_working_model(self):
        self.assertEqual((ccm.revision(self.state), ccm.launch(self.state)), (0, (0, "", "")))
        ccm.save(self.state, "gpt-a", applied="next-turn", accepted=True)
        rec = ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)
        self.assertEqual((rec["rev"], rec["status"], rec["working"], rec["previous"]), (2, "pending", "gpt-a", "gpt-a"))
        self.assertEqual(ccm.pending(self.state), ("gpt-b", 2))
        self.assertEqual(ccm.launch(self.state), (2, "gpt-b", "gpt-a"), "a TUI takes the pick; a seat starts on gpt-a")

    def test_in_use_is_the_pick_only_while_a_turn_the_seat_started_on_it_is_running(self):
        ccm.save(self.state, "gpt-a", applied="next-turn", accepted=True)
        rec = ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)
        read = lambda: ccm.read_record(self.state)
        self.assertEqual(ccm.in_use(read()), "gpt-a", "saved and not started: still the working model")
        self.assertFalse(ccm.started(self.state, "gpt-b", 0), "a revision that is none marks nothing")
        self.assertFalse(ccm.started(self.state, "sonnet", rec["rev"]), "nor a name that is no Codex model")
        self.assertFalse(ccm.started(self.root / "no-record", "gpt-b", 1), "nor anything without a record")
        self.assertTrue(ccm.started(self.state, "gpt-b", rec["rev"], now=1000.0))
        self.assertEqual((ccm.in_use(read(), now=1010.0), ccm.working(read()), ccm.pending(self.state)[0]),
                         ("gpt-b", "gpt-a", "gpt-b"), "running on the pick; still pending, still launched on gpt-a")
        self.assertEqual(ccm.launch(self.state)[2], "gpt-a")
        # A mark from a seat that died mid-turn, or one with a broken time, is not believed.
        self.assertEqual(ccm.in_use(read(), now=1000.0 + ccm.RUNNING_STALE_S + 1), "gpt-a")
        self.assertEqual(ccm.in_use(read(), now=999.0), "gpt-a")
        broken = dict(read(), running={"model": "gpt-b", "rev": rec["rev"], "ts": "soon"})
        self.assertEqual(ccm.in_use(broken), "gpt-a")
        self.assertEqual(ccm.in_use(dict(read(), running="yes")), "gpt-a")
        # No verdict: the mark goes, the pick stays pending. Only its own revision's mark is removed.
        self.assertFalse(ccm.stopped(self.state, rec["rev"] + 1))
        self.assertTrue(ccm.stopped(self.state, rec["rev"]))
        self.assertEqual((ccm.in_use(read()), read()["status"], "running" in read()), ("gpt-a", "pending", False))
        self.assertFalse(ccm.stopped(self.state))
        # Codex took it: confirmed at the turn's end, and the mark is gone with the settlement.
        self.assertTrue(ccm.started(self.state, "gpt-b", rec["rev"]))
        ccm.resolve(self.state, rec["rev"], True)
        self.assertEqual((ccm.in_use(read()), read()["working"], "running" in read()), ("gpt-b", "gpt-b", False))
        # A newer pick saved while Codex was still answering turn/start for an older one: Codex took the
        # older, and that is what the turn runs on. The mark is the turn's, whatever is pending by then.
        raced = ccm.save(self.state, "gpt-x", applied="next-turn", accepted=False)
        beat = ccm.save(self.state, "gpt-y", applied="next-turn", accepted=False)
        self.assertTrue(ccm.started(self.state, "gpt-x", raced["rev"]), "recorded though gpt-y is the pick now")
        self.assertEqual((ccm.in_use(read()), ccm.pending(self.state), ccm.working(read())),
                         ("gpt-x", ("gpt-y", beat["rev"]), "gpt-b"))
        self.assertTrue(ccm.stopped(self.state, raced["rev"]))
        ccm.resolve(self.state, beat["rev"], False, "refused")
        # A second pick saved while the first trial's turn is still running: that turn is on the first
        # pick, and says so; the second is what is pending; a restart would still launch on gpt-b.
        first = ccm.save(self.state, "gpt-c", applied="next-turn", accepted=False)
        self.assertTrue(ccm.started(self.state, "gpt-c", first["rev"]))
        second = ccm.save(self.state, "gpt-d", applied="next-turn", accepted=False)
        self.assertEqual((ccm.in_use(read()), ccm.pending(self.state)[0], ccm.working(read()), ccm.launch(self.state)[2]),
                         ("gpt-c", "gpt-d", "gpt-b", "gpt-b"))
        # Settling the newer pick does not end the older turn's observation; that turn's own end does.
        self.assertIsNone(ccm.resolve(self.state, first["rev"], True), "the record moved on: not settled")
        self.assertEqual(ccm.in_use(read()), "gpt-c")
        self.assertFalse(ccm.stopped(self.state, second["rev"]))
        self.assertTrue(ccm.stopped(self.state, first["rev"]))
        self.assertEqual((ccm.in_use(read()), "running" in read()), ("gpt-b", False))
        self.assertTrue(ccm.started(self.state, "gpt-d", second["rev"]))
        ccm.resolve(self.state, second["rev"], False, "refused")
        self.assertNotIn("running", read(), "its own settlement ends its mark")
        third = ccm.save(self.state, "gpt-e", applied="next-turn", accepted=False)
        self.assertTrue(ccm.started(self.state, "gpt-e", third["rev"]))
        self.assertTrue(ccm.stopped(self.state), "any mark, for a seat that is starting")
        self.assertEqual(ccm.in_use(dict(read(), running={"model": "gpt-e", "rev": 0, "ts": 1e18})), "gpt-b")

    def test_a_refused_pick_leaves_the_working_model_in_use(self):
        ccm.save(self.state, "gpt-a", applied="next-turn", accepted=True)
        ccm.save(self.state, "gpt-bad", applied="next-turn", accepted=False)
        rec = ccm.resolve(self.state, 2, False, "model not supported")
        self.assertEqual((rec["status"], rec["accepted"], rec["rejected_reason"]), ("rejected", False, "model not supported"))
        self.assertEqual(ccm.current(self.state), "gpt-a")
        self.assertEqual(ccm.launch(self.state), (2, "gpt-a", "gpt-a"), "a restart is not retried on the refused pick")
        self.assertEqual(ccm.pending(self.state), ("", 2))
        nxt = ccm.save(self.state, "gpt-c", applied="next-turn", accepted=False)
        self.assertEqual((nxt["previous"], nxt["working"], nxt["rev"]), ("gpt-a", "gpt-a", 3))

    def test_an_unconfirmed_pick_is_not_a_refusal_and_leaves_the_working_model_in_use(self):
        ccm.save(self.state, "gpt-a", applied="next-turn", accepted=True)
        ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)
        self.assertIsNone(ccm.unconfirmed(self.state, 1, "stale"), "rev 1 is no longer the record's")
        rec = ccm.unconfirmed(self.state, 2, "no verdict")
        self.assertEqual((rec["status"], rec["accepted"], rec["unconfirmed_reason"]), ("unconfirmed", False, "no verdict"))
        self.assertNotIn("rejected_reason", rec)
        self.assertEqual((ccm.current(self.state), ccm.launch(self.state), ccm.pending(self.state)),
                         ("gpt-a", (2, "gpt-a", "gpt-a"), ("", 2)))
        self.assertIsNone(ccm.resolve(self.state, 2, True), "a settled pick stays settled")
        self.assertEqual(ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)["previous"], "gpt-a")

    def test_an_accepted_pick_is_the_working_model_and_a_moved_on_record_is_never_settled(self):
        ccm.save(self.state, "gpt-a", applied="next-turn", accepted=False)
        ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)
        self.assertIsNone(ccm.resolve(self.state, 1, True), "rev 1 is no longer the record's")
        rec = ccm.resolve(self.state, 2, True)
        self.assertEqual((rec["status"], rec["working"]), ("accepted", "gpt-b"))
        self.assertIsNone(ccm.resolve(self.state, 2, False, "late"), "a settled pick stays settled")
        self.assertEqual(ccm.current(self.state), "gpt-b")

    def test_a_malformed_confirmed_rev_never_raises(self):
        for bad in ("x", [1], True, -3, None, 2.5):
            with self.subTest(bad=bad):
                ccm.save(self.state, "gpt-a", applied="next-turn", accepted=False)
                rec = json.loads(ccm.record_path(self.state).read_text())
                rec.update(confirmed="gpt-z", confirmed_rev=bad)
                ccm.record_path(self.state).write_text(json.dumps(rec))
                self.assertTrue(ccm.confirm(self.state, "gpt-a", rec["rev"]))
                rec = json.loads(ccm.record_path(self.state).read_text())
                rec["confirmed_rev"] = bad
                ccm.record_path(self.state).write_text(json.dumps(rec))
                self.assertIsNotNone(ccm.resolve(self.state, rec["rev"], True))
                rec = json.loads(ccm.record_path(self.state).read_text())
                rec["confirmed_rev"] = bad
                ccm.record_path(self.state).write_text(json.dumps(rec))
                ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)

    def test_a_confirmed_model_without_a_valid_revision_is_ignored_for_the_working_one(self):
        for bad in ("x", [1], True, -3, None, 2.5, 0):
            with self.subTest(bad=bad):
                rec = {"model": "gpt-p", "runtime": "codex", "status": "pending", "rev": 4,
                       "working": "gpt-w", "confirmed": "gpt-z", "confirmed_rev": bad}
                self.assertEqual(ccm.working(rec), "gpt-w")
                self.state.mkdir(exist_ok=True)
                ccm.record_path(self.state).write_text(json.dumps(rec))
                self.assertEqual(ccm.launch(self.state)[2], "gpt-w")
                nxt = ccm.save(self.state, "gpt-q", applied="next-turn", accepted=False)
                self.assertEqual(nxt["working"], "gpt-w", "the unverified value is never carried")
                self.assertNotIn("confirmed", nxt)
        self.assertEqual(ccm.working({"working": "gpt-w", "confirmed": "gpt-z", "confirmed_rev": 3}), "gpt-z")

    def test_a_record_from_before_revisions_reads_as_revision_0_and_not_pending(self):
        self.state.mkdir()
        ccm.record_path(self.state).write_text(json.dumps({"model": "gpt-a", "runtime": "codex", "accepted": True}))
        self.assertEqual((ccm.revision(self.state), ccm.pending(self.state), ccm.current(self.state)), (0, ("", 0), "gpt-a"))
        self.assertEqual(ccm.save(self.state, "gpt-b", applied="next-turn", accepted=False)["working"], "gpt-a")


class Choices(Case):
    def write(self, data):
        (self.root / "models_cache.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def test_visible_models_by_priority_once_each(self):
        self.write({"models": [{"slug": "gpt-b", "display_name": "GPT  B", "priority": 2},
                               {"slug": "gpt-a", "visibility": "list", "priority": 1},
                               {"slug": "gpt-h", "visibility": "hide", "priority": 0},
                               {"slug": "gpt-b", "display_name": "dup", "priority": 3},
                               {"slug": "opus"}, "not a dict", {"slug": "gpt-n", "priority": "x"}]})
        self.assertEqual(ccm.choices(self.root), [("gpt-a", "gpt-a"), ("gpt-b", "GPT B"), ("gpt-n", "gpt-n")])

    def test_no_cache_or_a_broken_one_is_no_list(self):
        self.assertEqual(ccm.choices(self.root), [])
        for data in ("{", json.dumps([1]), json.dumps({"models": "x"})):
            self.write(data)
            self.assertEqual(ccm.choices(self.root), [], data)


class Home(Case):
    def test_the_configured_home_then_codex_home_then_the_default(self):
        import codex_home
        with mock.patch.object(codex_home, "configured", return_value=("CODEX_HOME", str(self.root / "cfg"))):
            self.assertEqual(ccm.home(REPO), self.root / "cfg")
        with mock.patch.object(codex_home, "configured", side_effect=RuntimeError("unreadable")), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "env")}):
            self.assertEqual(ccm.home(REPO), self.root / "env")
        with mock.patch.object(codex_home, "configured", return_value=("", "")), \
                mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(ccm.home(REPO), self.root / ".codex")


class Cli(Case):
    def test_check_current_save_and_choices(self):
        self.assertEqual(self.main("check", "gpt-a"), (0, ""))
        self.assertEqual(self.main("check", "opus"), (2, ""))
        self.assertEqual(self.main("current", "--state-dir", str(self.state)), (0, ""))
        self.assertEqual(self.main("save", "--state-dir", str(self.state), "--model", "gpt-a", "--applied", "next-start"),
                         (0, "OK -"))
        self.assertEqual(self.main("save", "--state-dir", str(self.state), "--model", "gpt-b", "--applied", "next-turn",
                                   "--accepted"), (0, "OK gpt-a"))
        self.assertTrue(json.loads(ccm.record_path(self.state).read_text())["accepted"])
        self.assertEqual(self.main("current", "--state-dir", str(self.state)), (0, "gpt-b"))
        self.assertEqual(self.main("launch", "--state-dir", str(self.state)), (0, "2 gpt-b gpt-b"))
        self.assertEqual(self.main("working", "--state-dir", str(self.state)), (0, "gpt-b"))
        self.main("save", "--state-dir", str(self.state), "--model", "gpt-c", "--applied", "next-turn")
        self.assertEqual(self.main("working", "--state-dir", str(self.state)), (0, "gpt-b"), "the pick is not it yet")
        self.assertEqual(self.main("in-use", "--state-dir", str(self.state)), (0, "gpt-b"))
        ccm.started(self.state, "gpt-c", 3)
        self.assertEqual(self.main("in-use", "--state-dir", str(self.state)), (0, "gpt-c"))
        self.assertEqual(self.main("working", "--state-dir", str(self.root / "none")), (0, ""))
        ccm.save(self.state, "gpt-b", applied="next-turn", accepted=True)
        rc, out = self.main("save", "--state-dir", str(self.state), "--model", "sonnet", "--applied", "next-turn")
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith("FAILED"))
        (self.root / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-a", "display_name": "A"}]}))
        self.assertEqual(self.main("choices", "--codex-home", str(self.root)), (0, "gpt-a=A"))
        with mock.patch.object(ccm, "home", return_value=self.root):
            self.assertEqual(self.main("choices"), (0, "gpt-a=A"))

    def test_an_unwritable_state_folder_is_exit_1(self):
        blocker = self.root / "file"
        blocker.write_text("")
        rc, out = self.main("save", "--state-dir", str(blocker), "--model", "gpt-a", "--applied", "next-turn")
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("FAILED"))


if __name__ == "__main__":
    unittest.main()
