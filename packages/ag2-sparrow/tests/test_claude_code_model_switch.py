#!/usr/bin/env python3
"""claude-local `model`: the switch and its refusals with the pane and the key sender stood in, so
this runs where no tmux does. The same paths on a real tmux server are in test_claude_code_adapter."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ag2_sparrow import tmux_pane_keys  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli, launcher  # noqa: E402

GEN = {"nonce": "a" * 32, "pane_pid": 7, "session_id": "$1", "session_created": 5, "pane_id": "%1"}
REC = {"generation": GEN, "title_token": "tok"}


class Pane:
    """What the pane shows, and the keys sent to it; `react(keys, pane)` is the stand-in Claude Code."""

    def __init__(self, react=None, session=True, rc=0):
        self.text, self.sent, self.react, self.session, self.rc = "", [], react, session, rc
        self.sock, self.tmux = "/s", "tmux"

    def has_session(self, name=None):
        return self.session

    def capture(self, pane_id):
        return self.text

    def run(self, argv, **kw):
        if isinstance(self.rc, Exception):
            raise self.rc
        keys = argv[argv.index("--") + 1:]
        self.sent.append(keys)
        if self.rc == 0 and self.react:
            self.react(keys, self)
        return types.SimpleNamespace(returncode=self.rc)


def lc_for(pane, root):
    lc = launcher.Launcher.__new__(launcher.Launcher)
    lc.tm, lc.run, lc.root, lc.logs = pane, pane.run, str(root), []
    lc.log, lc.sleep, lc.clock = lc.logs.append, lambda s: None, lambda: 1000.0
    lc.unsent = lambda gen, title, rc, what: "" if rc == 99 else "cancelled: the tuple changed before %s; nothing sent" % what
    return lc


def says(keys, pane):
    if keys[0] == "/model":
        pane.text += "\nSet model to %s" % keys[2]


class SwitchModel(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)

    def test_a_reported_switch_is_recorded_and_sent_through_the_fence(self):
        pane = Pane(says)
        lc = lc_for(pane, self.root)
        self.assertEqual(launcher.switched_model(self.root), "")
        self.assertEqual(lc.switch_model(REC, "opus"), (True, "switched to opus"))
        self.assertEqual(pane.sent, [["/model", "Space", "opus", "Enter"]])
        self.assertEqual((launcher.switched_model(self.root), lc.logs), ("opus", ["model: switched to opus"]))
        record = self.root.joinpath(*launcher.MODEL_RECORD)
        self.assertEqual(json.loads(record.read_text()), {"model": "opus", "at": 1000.0})
        for body in ("[", "[1]", json.dumps({"model": None})):
            record.write_text(body)
            self.assertEqual(launcher.switched_model(self.root), "", body)

    def test_what_is_not_a_model_or_has_no_session_sends_nothing(self):
        pane = Pane(says)
        lc = lc_for(pane, self.root)
        for bad in ("", None, "gpt-5", "opus; rm -rf x", "Enter"):
            self.assertEqual(lc.switch_model(REC, bad)[0], False, bad)
        self.assertEqual(lc.switch_model({}, "opus"), (False, "no running session to switch; nothing sent"))
        self.assertEqual(lc_for(Pane(says, session=False), self.root).switch_model(REC, "opus")[0], False)
        self.assertEqual((pane.sent, launcher.switched_model(self.root)), ([], ""))

    def test_keys_the_fence_withholds_or_cannot_send_are_not_a_switch(self):
        for rc, expect in ((1, "cancelled: the tuple changed before /model; nothing sent"),
                           (99, "the session ended before /model; nothing switched")):
            self.assertEqual(lc_for(Pane(says, rc=rc), self.root).switch_model(REC, "opus"), (False, expect))
        for boom in (OSError("no bash"), subprocess.TimeoutExpired("x", 1)):
            lc = lc_for(Pane(says, rc=boom), self.root)
            seen = []
            lc.unsent = lambda gen, title, rc, what: seen.append(rc) or "uncertain"
            self.assertEqual((lc.switch_model(REC, "opus"), seen), ((False, "uncertain"), [tmux_pane_keys.UNCERTAIN]))
        self.assertEqual(launcher.switched_model(self.root), "")

    def test_a_session_that_never_reports_the_switch_is_unconfirmed(self):
        pane = Pane()
        pane.text = "Set model to sonnet"          # an earlier switch's line does not count
        with mock.patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 5.0]):
            switched, line = lc_for(pane, self.root).switch_model(REC, "haiku", wait_s=2)
        self.assertEqual((switched, line), (False, "unconfirmed: /model haiku was sent but Claude Code did not report "
                                                   "the switch in 2 s"))
        self.assertEqual(launcher.switched_model(self.root), "")

    def test_the_confirm_dialog_is_answered_once_with_confirm_and_cancelled_without(self):
        def asks(keys, pane):
            if keys[0] == "/model":
                pane.asked = keys[2]
                pane.text += "\nYes, switch / No, go back"
            elif keys == ["Enter"]:
                pane.text += "\nSet model to %s" % pane.asked

        pane = Pane(asks)
        switched, line = lc_for(pane, self.root).switch_model(REC, "opus")
        self.assertEqual((switched, "--confirm" in line, pane.sent[-1]), (False, True, ["Escape"]))
        again = Pane(asks)
        again.text = pane.text                         # the cancelled dialog is still on screen
        self.assertEqual(lc_for(again, self.root).switch_model(REC, "sonnet", confirm=True), (True, "switched to sonnet"))
        self.assertEqual(again.sent, [["/model", "Space", "sonnet", "Enter"], ["Enter"]])

    def test_a_switch_that_cannot_be_recorded_is_still_a_switch(self):
        pane = Pane(says)
        with mock.patch.object(launcher.os, "replace", side_effect=OSError("read-only")):
            self.assertEqual(lc_for(pane, self.root).switch_model(REC, "fable"), (True, "switched to fable"))
        self.assertEqual(launcher.switched_model(self.root), "")


class ModelCommand(unittest.TestCase):
    def run_model(self, *argv, selected=object(), config=({"ROOT": "/w"}, ""), switch=(True, "switched to opus")):
        args = types.SimpleNamespace(model=argv[0] if argv else None, confirm="--confirm" in argv)
        made = []

        class Lc:
            def __init__(self, cfg, config_dir):
                made.append((cfg, config_dir))

            def switch_model(self, rec, model, confirm=False):
                made.append((rec, model, confirm))
                return switch

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "selected", return_value=selected), \
                mock.patch.object(cli, "load_config", return_value=config), \
                mock.patch.object(cli, "config_dir", return_value="/c"), \
                mock.patch.object(cli.launcher, "Launcher", Lc), \
                mock.patch.object(cli.launcher, "switched_model", return_value="sonnet"), \
                mock.patch.object(cli.records, "load_supervisor", return_value={"r": 1}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.model(args)
        return rc, out.getvalue(), err.getvalue(), made

    def test_it_prints_the_recorded_model_or_switches_and_says_how_it_went(self):
        rc, out, _, made = self.run_model()
        self.assertEqual((rc, json.loads(out), made), (0, {"model": "sonnet"}, []))
        rc, out, _, made = self.run_model("opus", "--confirm")
        self.assertEqual((rc, json.loads(out)), (0, {"switched": True, "model": "opus", "detail": "switched to opus"}))
        self.assertEqual(made, [({"ROOT": "/w"}, "/c"), ({"r": 1}, "opus", True)])
        rc, out, _, _ = self.run_model("opus", switch=(False, "unconfirmed: x"))
        self.assertEqual((rc, json.loads(out)["switched"]), (1, False))

    def test_no_job_or_an_unreadable_config_is_unusable(self):
        self.assertEqual(self.run_model(selected=None)[0], cli.EXIT_UNUSABLE)
        rc, out, err, made = self.run_model("opus", config=({}, "the config is missing"))
        self.assertEqual((rc, out, err, made), (cli.EXIT_UNUSABLE, "", "error: the config is missing\n", []))

    def test_the_command_line_reaches_it_with_an_optional_model_and_confirm(self):
        seen = []
        with mock.patch.object(cli, "model", side_effect=lambda args: seen.append((args.model, args.confirm)) or 0):
            self.assertEqual((cli.main(["model"]), cli.main(["model", "opus", "--confirm"])), (0, 0))
        self.assertEqual(seen, [(None, False), ("opus", True)])


if __name__ == "__main__":
    unittest.main()
