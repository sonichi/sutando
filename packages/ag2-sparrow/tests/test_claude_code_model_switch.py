#!/usr/bin/env python3
"""claude-local `model`: the switch and its refusals with the pane and the key sender stood in, so
this runs where no tmux does. The same paths on a real tmux server are in test_claude_code_adapter."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

from ag2_sparrow import tmux_pane_keys  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli, hold, launcher, trust  # noqa: E402

GEN = {"nonce": "a" * 32, "pane_pid": 7, "session_id": "$1", "session_created": 5, "pane_id": "%1"}
REC = {"generation": GEN, "title_token": "tok"}


RULE = "\u2500" * 40


def box(*rows):
    """Claude Code's input box as it is on screen: a rule, the prompt row and any further rows of a
    draft, a rule, the footer."""
    first, rest = (rows + ("",))[0], rows[1:]
    return "\n".join([RULE, "\u276f " + first, *rest, RULE, "  \u23f5\u23f5 bypass permissions on"])


class Pane:
    """What the pane shows, and the keys sent to it; `react(keys, pane)` is the stand-in Claude Code."""

    def __init__(self, react=None, session=True, rc=0):
        self.text, self.sent, self.react, self.session, self.rc = box(), [], react, session, rc
        self.sock, self.tmux = "/s", "tmux"

    def has_session(self, name=None):
        return self.session

    def capture(self, pane_id, styled=False):
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
    lc.tm, lc.run, lc.root, lc.logs, lc.cfg = pane, pane.run, str(root), [], {}
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

    def test_a_work_root_whose_model_was_switched_still_starts(self):
        """The switch leaves a record and a lock under state/. The start check names every entry it lets
        an agent's state hold: with these two missing from it, the job refused its next start."""
        from ag2_sparrow import claude_local_records as records, task_lifecycle
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root)))
        self.assertEqual(lc_for(Pane(says), self.root).switch_model(REC, "opus"), (True, "switched to opus"))
        left = sorted(p.name for p in (self.root / "state").iterdir() if "model" in p.name)
        self.assertEqual(left, ["model-switch.json", "model-switch.lock"])
        for check_only in (True, False):
            self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root), check_only=check_only), left)
        # A switch stopped between writing its record and moving it into place leaves a temp the start
        # check takes for a live writer's, and sweeps once it is old.
        state, made = self.root / "state", []

        def stopped(tmp, path):
            made.append(Path(tmp).name)
            raise KeyboardInterrupt("stopped between the write and the rename")
        real_unlink = os.unlink
        with mock.patch.object(records.os, "replace", side_effect=stopped), \
                mock.patch.object(records.os, "unlink", side_effect=lambda p: None), \
                self.assertRaises(KeyboardInterrupt):
            lc_for(Pane(says), self.root).switch_model(REC, "haiku")
        (temp,) = [p for p in state.iterdir() if p.name.endswith(".tmp")]
        self.assertRegex(temp.name, r"^\.model-switch\.json\.\w+\.tmp$")
        self.assertEqual(json.loads((state / "model-switch.json").read_text())["model"], "opus", "the old record stands")
        # Two writers never share a temp: each staging file has its own name.
        with mock.patch.object(records.os, "replace", side_effect=stopped), \
                mock.patch.object(records.os, "unlink", side_effect=lambda p: None), \
                self.assertRaises(KeyboardInterrupt):
            lc_for(Pane(says), self.root).switch_model(REC, "haiku")
        self.assertEqual(len(set(made)), 2, made)
        real_unlink(state / made[1])
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root), check_only=True), temp.name)
        os.utime(temp, (1, 1))
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root)))
        self.assertFalse(temp.exists(), "swept")
        # 0.11's fixed-name temp, left by a stop: removed at the next start whatever its age, and only when
        # it is a plain file (a link is refused, and what it points at is never touched).
        old = state / "model-switch.json.tmp"
        old.write_text('{"model": "ha')
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root), check_only=True))
        self.assertTrue(old.exists(), "a check removes nothing")
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root)))
        self.assertFalse(old.exists())
        target = self.root / "elsewhere.txt"
        target.write_text("kept")
        old.symlink_to(target)
        for check_only in (True, False):
            self.assertIn("state/model-switch.json.tmp in", task_lifecycle.prepare_work_dir(str(self.root), check_only=check_only))
        self.assertEqual((target.read_text(), old.is_symlink()), ("kept", True))
        old.unlink()
        # A temp under one of the allowed names is a plain file or it is refused, fresh or old, checked or
        # started: a directory there would otherwise pass while fresh and then break the sweep.
        for name in (".model-switch.json.x1.tmp", ".model-switch.lock.x1.tmp", ".isolated-turns.json.x1.tmp"):
            odd = state / name
            for make in (lambda: odd.mkdir(), lambda: odd.symlink_to(self.root / "nowhere")):
                make()
                for age in (None, (1, 1)):
                    if age and odd.is_dir() and not odd.is_symlink():
                        os.utime(odd, age)
                    for check_only in (True, False):
                        self.assertIn("state/%s in" % name, task_lifecycle.prepare_work_dir(str(self.root), check_only=check_only))
                odd.rmdir() if odd.is_dir() and not odd.is_symlink() else odd.unlink()
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root), check_only=True))
        # The isolated turn's hourly cap is kept there too, under the name the adapter itself gives it.
        from ag2_sparrow import relay_loop
        from ag2_sparrow.adapters.claude_code import inbox
        relay_loop.RateLimiter(str(self.root / "state" / inbox.ISOLATED_LIMITER_FILE), inbox.ISOLATED_PER_HOUR).record("task-g")
        self.assertTrue((self.root / "state" / "isolated-turns.json").is_file())
        self.assertIsNone(task_lifecycle.prepare_work_dir(str(self.root), check_only=True))
        # Anything else under state/ is still refused: the list did not become a pattern.
        (self.root / "state" / "model-switch.other").write_text("")
        self.assertIn("state/model-switch.other is not this adapter's state",
                      task_lifecycle.prepare_work_dir(str(self.root), check_only=True))

    def test_a_session_that_never_reports_the_switch_is_unconfirmed(self):
        pane = Pane()
        pane.text += "\nSet model to sonnet"      # an earlier switch's line does not count
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

    def test_a_draft_in_the_input_box_or_a_box_that_cannot_be_read_gets_no_keys(self):
        dim, footer = "\x1b[2m", "  \u23f5\u23f5 bypass permissions on"
        # After the drafts: no frame around the prompt, a frame cut off below or above, a dialog or a
        # menu left open, no prompt. An empty reading outside the frame is not evidence of an empty box.
        for shown, why in ((box("half a message"), "holds unsent text"),
                           (box("", "  a second line under a blank first one"), "holds unsent text"),
                           (box("first", "second"), "holds unsent text"),
                           # The owner's own words, unstyled, that read like Claude Code's hints.
                           (box('Try "fix lint errors"'), "holds unsent text"),
                           (box("Press up to edit queued messages"), "holds unsent text"),
                           ("old\n" + box() + "\nlater\n" + box("fix the bug"), "holds unsent text"),
                           (box("/mod") + "\n  /model   Set the AI model", "holds unsent text"),
                           ("\u276f ", "cannot be read"),
                           ("\u276f \n" + footer, "cannot be read"),
                           (RULE + "\n\u276f ", "cannot be read"),
                           ("\u276f \n" + RULE + "\n" + footer, "cannot be read"),
                           ("Select model\n\u276f 1. Default\n  2. Opus", "cannot be read"),
                           (RULE + "\n Select model\n\u276f 1. Default\n  2. Opus\n" + RULE, "cannot be read"),
                           ("no prompt on this screen", "cannot be read"), ("", "cannot be read")):
            pane = Pane(says)
            pane.text = shown
            switched, line = lc_for(pane, self.root).switch_model(REC, "opus")
            self.assertEqual((switched, why in line, pane.sent), (False, True, []), shown)
        # A placeholder or a suggestion is styled, never typed: the box is empty. So is a box whose
        # rules and footer arrive coloured, as a styled capture of the real screen has them.
        coloured = "\n".join("\x1b[38;5;244m%s\x1b[39m" % row if n != 1 else row for n, row in enumerate(box().splitlines()))
        for shown in (box('%sTry "fix lint errors"\x1b[0m' % dim), box("%sPress up to edit queued messages\x1b[0m" % dim),
                      box("\x1b[38;5;240mrun the tests\x1b[39m"), coloured):
            pane = Pane(says)
            pane.text = shown
            self.assertEqual(lc_for(pane, self.root).switch_model(REC, "opus"), (True, "switched to opus"), shown)
        self.assertEqual([launcher.composer_text(t) for t in (None, box(), box("hi "), box("a", " b "))],
                         [None, "", "hi", "a b "])
        self.assertEqual([launcher.composer_boxed(t) for t in (None, box(), "\u276f ", coloured)], [False, True, False, True])
        self.assertIs(launcher.claude_model_switch.pane_composer.composer_text.__module__ == "ag2_sparrow.pane_composer", True)

    def test_the_settings_pick_a_session_cannot_take_is_kept_and_the_next_start_runs_on_it(self):
        # 2026-11-25: the session sat on the usage-limit menu while it was restarted for an account change;
        # the page's pick came back "refused: the session does not show its whole input box".
        menu = "What do you want to do?\n\u276f 1. Stop and wait for limit to reset\n  2. Switch to extra usage"
        for pane, why in ((Pane(says), "it shows no input box"), (Pane(says, session=False), "no session is running")):
            pane.text = menu
            lc = lc_for(pane, self.root)
            slept = []
            lc.sleep = slept.append
            with mock.patch.object(launcher.time, "monotonic", side_effect=iter(range(0, 1000, 2)).__next__):
                switched, line = lc.switch_model(REC, "opus", pend=True)
            self.assertEqual((switched, line), (False, launcher.MODEL_PENDING % (why, "opus")))
            self.assertEqual((pane.sent, launcher.wanted_model(self.root), launcher.switched_model(self.root)),
                             ([], "opus", ""), "nothing typed; the pick waits; the model in use is unchanged")
            self.assertEqual(bool(slept), pane.session, "a session that is starting is given a moment to show its box")
        # The next start settles it: the session starts with --model, and nothing waits any more.
        self.assertEqual(hold.settle_wanted(self.root, now=lambda: 5.0), "opus")
        self.assertEqual(json.loads(self.root.joinpath(*launcher.MODEL_RECORD).read_text()), {"model": "opus", "at": 5.0})
        self.assertEqual((launcher.wanted_model(self.root), hold.settle_wanted(self.root)), ("", ""))
        argv = hold.claude_argv({"CLAUDE": "claude", "PLUGIN_DIR": str(self.root), "ROOT": str(self.root)}, "sid")
        self.assertEqual(argv[-2:], ["--model", "opus"])
        # A pick kept beside a model already in use keeps both, and a later live switch drops the waiting one.
        pane = Pane(says)
        pane.text = menu
        with mock.patch.object(launcher, "MODEL_BOX_WAIT_S", 0):
            lc_for(pane, self.root).switch_model(REC, "haiku", pend=True)
        self.assertEqual((launcher.switched_model(self.root), launcher.wanted_model(self.root)), ("opus", "haiku"))
        self.assertEqual(lc_for(Pane(says), self.root).switch_model(REC, "sonnet", pend=True), (True, "switched to sonnet"))
        self.assertEqual((launcher.switched_model(self.root), launcher.wanted_model(self.root)), ("sonnet", ""))

    def test_a_box_that_appears_while_waiting_is_typed_into_and_the_card_still_gets_the_refusal(self):
        pane = Pane(says)
        pane.text = "Welcome to Claude Code"
        lc = lc_for(pane, self.root)
        lc.sleep = lambda s: setattr(pane, "text", box())        # the session finished starting
        self.assertEqual(lc.switch_model(REC, "opus", pend=True), (True, "switched to opus"))
        # The in-chat card (no `pend`) keeps its own retry: it is refused as before, and nothing is kept.
        pane = Pane(says)
        pane.text = "Welcome to Claude Code"
        refused = lc_for(pane, self.root).switch_model(REC, "haiku")
        self.assertEqual((refused[0], "cannot be read" in refused[1], launcher.wanted_model(self.root)), (False, True, ""))
        # A pick that cannot be saved is the refusal, never a silent loss.
        with mock.patch.object(hold, "write_model_record", side_effect=OSError("read-only")), \
                mock.patch.object(launcher, "MODEL_BOX_WAIT_S", 0):
            self.assertIn("cannot be read", lc_for(pane, self.root).switch_model(REC, "haiku", pend=True)[1])
        self.root.joinpath(*launcher.MODEL_RECORD).write_text("[")
        self.assertEqual((hold.wanted_model(self.root), hold.settle_wanted(self.root)), ("", ""))
        self.root.joinpath(*launcher.MODEL_RECORD).write_text(json.dumps({"wanted": "opus"}))
        with mock.patch.object(hold, "write_model_record", side_effect=OSError("read-only")):
            self.assertEqual(hold.settle_wanted(self.root), "", "the start goes on with the model it had")

    def test_only_a_line_naming_the_model_asked_for_is_the_switch(self):
        def other(keys, pane):
            pane.text += "\n  \u23bf  Set model to Sonnet 4.6 and saved as your default"

        pane = Pane(other)
        with mock.patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 5.0]):
            switched, line = lc_for(pane, self.root).switch_model(REC, "opus", wait_s=2)
        self.assertEqual((switched, line), (False, "unconfirmed: /model opus was sent but Claude Code reported a "
                                                   "different model; nothing recorded"))
        self.assertEqual(launcher.switched_model(self.root), "", "a switch that was not this one is not recorded")
        for model, line, ok in (("opus", "Set model to Opus 4.6 and saved", True), ("opus[1m]", "Set model to opus[1m]", True),
                                # `default` names no variant: a default that is a 1M model still confirms it.
                                ("default", "Set model to Default (Opus 4.6 with 1M context)", True),
                                ("claude-opus-4-6", "Set model to Opus 4.6", True), ("claude-opus-4-6", "Set model to Opus 4.61", False),
                                ("claude-opus-4", "Set model to Opus 4.6", False), ("opus", "Set model to Opusplan", False),
                                ("default", "Set model to Default (recommended)", True), ("haiku", "Set model to Sonnet 4.6", False),
                                # The context variant is part of the model: neither line confirms the other.
                                ("opus[1m]", "Set model to Opus 4.6", False), ("opus", "Set model to Opus 4.6 (1M context)", False),
                                ("claude-opus-4-6[1m]", "Set model to Opus 4.6 (1M context)", True)):
            self.assertEqual(launcher.accepted_count(model, "x\n" + line), int(ok), (model, line))

        def plain(keys, pane):
            pane.text += "\nSet model to Opus 4.6 and saved as your default"

        with mock.patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 5.0]):
            switched, line = lc_for(Pane(plain), self.root).switch_model(REC, "opus[1m]", wait_s=2)
        self.assertEqual((switched, line.startswith("unconfirmed"), launcher.switched_model(self.root)), (False, True, ""))

    def test_a_1m_switch_answered_with_the_family_alone_counts_only_with_its_own_saved_setting(self):
        # Claude Code 2.1.322, measured 2026-11-22 on a claude-local agent: `/model sonnet[1m]` printed
        # this line (no context size) and saved `"model": "sonnet[1m]"` in the agent's settings.json.
        real = "  \u23bf  Set model to Sonnet 5.5 and saved as your default for new sessions"
        config = self.root / "claude-config"
        config.mkdir()
        settings = config / "settings.json"

        def answers(saved):
            def react(keys, pane):
                if keys[0] == "/model":
                    pane.text += "\n" + real
                    if saved is not None:
                        settings.write_text(json.dumps({"model": saved, "theme": "dark", "n": len(pane.sent)}))
            return react

        def switch(pane, model="sonnet[1m]"):
            lc = lc_for(pane, self.root)
            lc.cfg = {"CLAUDE_CONFIG_DIR": str(config)}
            with mock.patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 5.0]):
                return lc.switch_model(REC, model, wait_s=2)

        # The family's line alone, or with settings saved that name another variant (a plain Sonnet switch
        # saves too), or that name none (a theme change), proves no 1M context: unconfirmed, nothing recorded.
        for saved in (None, "sonnet", "opus[1m]", ""):
            settings.unlink(missing_ok=True)
            switched, line = switch(Pane(answers(saved)))
            self.assertEqual((switched, line.startswith("unconfirmed"), launcher.switched_model(self.root)),
                             (False, True, ""), (saved, line))
        self.assertEqual(switch(Pane(answers("sonnet[1m]"))), (True, "switched to sonnet[1m]"))
        self.assertEqual(launcher.switched_model(self.root), "sonnet[1m]")
        # The right value left by an earlier switch and not saved again says nothing about this one.
        os.utime(settings, (1, 1))
        self.assertFalse(switch(Pane(answers(None)))[0])
        # A settings file that was there and is gone is no evidence either.
        settings.write_text(json.dumps({"model": "sonnet[1m]"}))

        def removes(keys, pane):
            if keys[0] == "/model":
                pane.text += "\n" + real
                settings.unlink()
        self.assertFalse(switch(Pane(removes))[0])
        # A line already on screen from an earlier switch is not this one's, whatever the settings say.
        settings.write_text(json.dumps({"model": "sonnet[1m]"}))
        pane = Pane()
        pane.text = real + "\n" + box()
        self.assertFalse(switch(pane)[0])
        # No config dir of the agent's own means no settings file to count: the family's line alone stays unconfirmed.
        lc = lc_for(Pane(answers("sonnet[1m]")), self.root)
        lc.cfg = {}
        with mock.patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 5.0]):
            self.assertFalse(lc.switch_model(REC, "sonnet[1m]", wait_s=2)[0])

    def test_a_confirmation_that_may_not_have_been_sent_is_tried_once_and_is_not_a_switch(self):
        class Asks(Pane):
            def run(self, argv, **kw):
                keys = argv[argv.index("--") + 1:]
                self.sent.append(keys)
                if keys[0] == "/model":
                    self.text += "\nYes, switch / No, go back"
                    return types.SimpleNamespace(returncode=0)
                return types.SimpleNamespace(returncode=tmux_pane_keys.UNCERTAIN)

        pane = Asks()
        lc = lc_for(pane, self.root)
        lc.unsent = lambda gen, title, rc, what: "uncertain: %s may have been sent" % what
        self.assertEqual(lc.switch_model(REC, "opus", confirm=True),
                         (False, "uncertain: the confirmation may have been sent"))
        self.assertEqual(pane.sent, [["/model", "Space", "opus", "Enter"], ["Enter"]], "one Enter, never a second")
        gone = Asks()
        self.assertEqual(lc_for(gone, self.root).switch_model(REC, "opus", confirm=True)[1],
                         "cancelled: the tuple changed before the confirmation; nothing sent")
        ended = lc_for(Asks(), self.root)
        ended.unsent = lambda gen, title, rc, what: ""
        self.assertIn("the session ended at the confirmation", ended.switch_model(REC, "opus", confirm=True)[1])
        self.assertEqual(launcher.switched_model(self.root), "")

    def test_a_dialog_that_could_not_be_cancelled_is_said_to_be_still_on_screen_and_switches_take_turns(self):
        class Asks(Pane):
            def run(self, argv, **kw):
                keys = argv[argv.index("--") + 1:]
                self.sent.append(keys)
                if keys[0] == "/model":
                    self.text += "\nYes, switch / No, go back"
                return types.SimpleNamespace(returncode=0 if keys[0] == "/model" else 1)

        switched, line = lc_for(Asks(), self.root).switch_model(REC, "opus")
        self.assertEqual((switched, line.endswith("Its question may still be on the session's screen.")), (False, True), line)
        self.assertTrue((self.root / "state" / "model-switch.lock").exists(), "one switch at a time per agent")
        self.assertIs(launcher.MODEL_RE, launcher.claude_model_switch.MODEL_RE, "the core's rules, not a copy")

    def test_no_time_to_wait_is_unconfirmed_not_an_error(self):
        switched, line = lc_for(Pane(), self.root).switch_model(REC, "opus", wait_s=0)
        self.assertEqual((switched, line.startswith("unconfirmed")), (False, True), line)

    def test_a_switch_that_cannot_be_recorded_is_still_a_switch(self):
        pane = Pane(says)
        with mock.patch.object(launcher.os, "replace", side_effect=OSError("read-only")):
            self.assertEqual(lc_for(pane, self.root).switch_model(REC, "fable"), (True, "switched to fable"))
        self.assertEqual(launcher.switched_model(self.root), "")


class ModelCommand(unittest.TestCase):
    def run_model(self, *argv, selected=object(), config=({"ROOT": "/w"}, ""), switch=(True, "switched to opus"),
                  wanted=""):
        args = types.SimpleNamespace(model=argv[0] if argv else None, confirm="--confirm" in argv)
        made = []

        class Lc:
            def __init__(self, cfg, config_dir):
                made.append((cfg, config_dir))

            def switch_model(self, rec, model, confirm=False, pend=False):
                made.append((rec, model, confirm))
                self.pend = pend
                return switch

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "selected", return_value=selected), \
                mock.patch.object(cli, "load_config", return_value=config), \
                mock.patch.object(cli, "config_dir", return_value="/c"), \
                mock.patch.object(cli.launcher, "Launcher", Lc), \
                mock.patch.object(cli.launcher, "switched_model", return_value="sonnet"), \
                mock.patch.object(cli.launcher, "wanted_model", return_value=wanted), \
                mock.patch.object(cli.records, "load_supervisor", return_value={"r": 1}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.model(args)
        return rc, out.getvalue(), err.getvalue(), made

    def test_it_prints_the_recorded_model_or_switches_and_says_how_it_went(self):
        rc, out, _, made = self.run_model()
        shown = json.loads(out)
        self.assertEqual((rc, shown["model"], shown["current"], shown["pending"], made), (0, "sonnet", "sonnet", None, []))
        rc, out, _, made = self.run_model("opus", "--confirm")
        self.assertEqual((rc, json.loads(out)), (0, {"switched": True, "model": "opus", "detail": "switched to opus"}))
        self.assertEqual(made, [({"ROOT": "/w"}, "/c"), ({"r": 1}, "opus", True)])
        rc, out, _, _ = self.run_model("opus", switch=(False, "unconfirmed: x"))
        self.assertEqual((rc, json.loads(out)["switched"]), (1, False))

    def test_a_pick_kept_for_the_next_start_is_pending_not_an_error(self):
        # 2026-11-25: the settings page showed "refused: the session does not show its whole input box".
        rc, out, _, _ = self.run_model("opus", "--confirm", switch=(False, "saved: ..."), wanted="opus")
        self.assertEqual((rc, json.loads(out)), (0, {"switched": False, "model": "opus", "detail": "saved: ...",
                                                    "pending": "opus"}))
        rc, out, _, _ = self.run_model(wanted="opus")
        self.assertEqual((rc, json.loads(out)["current"], json.loads(out)["pending"]), (0, "sonnet", "opus"))
        # Another pick is waiting, not this one: the refusal stands.
        rc, out, _, _ = self.run_model("opus", switch=(False, "refused: x"), wanted="haiku")
        self.assertEqual((rc, "pending" in json.loads(out)), (1, False))

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
