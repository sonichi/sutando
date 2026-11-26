#!/usr/bin/env python3
"""src/claude_model_switch.py: the one source of what typing /model into Claude Code relies on, for
the core's model-switch skill (through its command line) and for ag2-sparrow's claude-local adapter."""
import contextlib
import io
import os
import re
import tempfile
import json
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import claude_model_switch as rules  # noqa: E402
sys.path.insert(0, str(REPO / "src" / "delivery"))
import pane_gate  # noqa: E402
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow import claude_model_switch as bundled  # noqa: E402

RULE, FOOTER = "\u2500" * 30, "  \u23f5\u23f5 bypass permissions on"
DIM, GREY, OFF = "\x1b[2m", "\x1b[38;5;240m", "\x1b[0m"


def box(*rows):
    """Claude Code's input box as it is on screen: a rule, the prompt row and any further rows of a
    draft, a rule, the footer."""
    return "\n".join([RULE, "\u276f " + rows[0], *rows[1:], RULE, FOOTER])


def kind(text):
    return "unknown" if text is None else "text" if text else "empty"


# (what the screen is, the screen, the one answer, inside its frame, pane_gate's `pending`). The last
# reads the prompt row alone; it is pinned where it differs from the whole box ("differs").
SCREENS = (
    ("an empty box inside its frame", box(""), "empty", True, "empty"),
    ("an empty box, nbsp after the glyph", RULE + "\n\u276f\u00a0\n" + RULE + "\n" + FOOTER, "empty", True, "empty"),
    ("a one-line draft", box("fix the bug "), "text", True, "text"),
    ("a draft of several lines", box("first", "  second"), "text", True, "text"),
    ("a draft whose first line is blank", box("", "  second"), "text", True, "empty"),            # differs
    ("a dim placeholder", box('%sTry "fix lint errors"%s' % (DIM, OFF)), "empty", True, "empty"),
    ("a grey suggestion", box("%srun the tests\x1b[39m" % GREY), "empty", True, "empty"),
    ("an unstyled hint", box('Try "fix lint errors"'), "empty", True, "text"),                    # differs
    ("a prompt row with no frame", "\u276f ", "empty", False, "empty"),
    ("a prompt row and footer, no rules", "\u276f \n" + FOOTER, "empty", False, "empty"),
    ("a box cut off at the bottom", RULE + "\n\u276f ", "empty", False, "empty"),
    ("a box cut off at the top", "\u276f \n" + RULE + "\n" + FOOTER, "empty", False, "empty"),
    ("the /model dialog", "Select model\n\u276f 1. Default (recommended)\n  2. Opus\n  Enter to confirm \u00b7 Esc to cancel",
     "text", False, "text"),
    ("the /model dialog between rules", RULE + "\n Select model\n\u276f 1. Default\n  2. Opus\n" + RULE, "text", False, "text"),
    ("a menu left open under the box", box("/mod") + "\n  /model   Set the AI model", "text", True, "text"),
    ("an old empty box above a draft", "old\n" + box("") + "\nlater\n" + box("fix the bug"), "text", True, "text"),
    ("an old draft above an empty box", box("sent already") + "\nreply\n" + box(""), "empty", True, "empty"),
    ("no prompt on the screen", "no prompt on this screen\n" + RULE, "unknown", False, "unknown"),
    ("an empty screen", "", "unknown", False, "unknown"),
)


class Rules(unittest.TestCase):
    def test_only_an_alias_or_a_claude_id_may_be_typed(self):
        for model in ("default", "opus", "fable[1m]", "claude-opus-4-6", "claude-fable-5-2[1m]"):
            self.assertTrue(rules.MODEL_RE.match(model), model)
        for model in ("", "gpt-5", "opus; rm -rf x", "opus fable", "Enter", "default[1m]", "claude-Opus"):
            self.assertFalse(rules.MODEL_RE.match(model), model)

    def test_the_switch_is_the_line_naming_that_model(self):
        for model, line, ok in (("opus", "  Set model to Opus 4.6 and saved as your default", True),
                                ("opus", "Set model to Opusplan", False), ("opus[1m]", "Set model to opus[1m]", True),
                                ("claude-opus-4-6", "Set model to Opus 4.6", True),
                                ("claude-opus-4-6", "Set model to Opus 4.61", False),
                                ("claude-opus-4", "Set model to Opus 4.6", False),
                                ("default", "Set model to Default (recommended)", True),
                                ("haiku", "Set model to Sonnet 4.6", False)):
            self.assertEqual(bool(rules.model_accepted(model).search(line)), ok, (model, line))

    def test_every_screen_gets_one_answer_through_the_pane_gate_and_through_the_model_switch(self):
        """The shared table: the core's pane gate and the model switch (src and the copy the package
        bundles) give the same empty / text / unknown for every screen, and the same frame verdict."""
        def cli(screen):
            r = subprocess.run([sys.executable, str(REPO / "src" / "delivery" / "pane_gate.py"), "composer-text",
                                "--runtime", "claude"], input=screen, capture_output=True, text=True)
            return None if r.returncode == pane_gate.EXIT_UNSAFE else r.stdout.rstrip("\n")

        for what, screen, answer, boxed, prompt_row in SCREENS:
            readings = {"pane_gate.composer_text": pane_gate.composer_text(screen, pane_gate.CLAUDE),
                        "pane_gate.py composer-text": cli(screen),
                        "claude_model_switch.composer_text": rules.composer_text(screen),
                        "the bundled copy": bundled.composer_text(screen)}
            self.assertEqual({name: kind(text) for name, text in readings.items()}, dict.fromkeys(readings, answer), what)
            self.assertEqual(len(set(readings.values())), 1, (what, readings))
            self.assertEqual([pane_gate.composer_boxed(screen, pane_gate.CLAUDE), rules.composer_boxed(screen),
                              bundled.composer_boxed(screen)], [boxed] * 3, what)
            self.assertEqual(kind(pane_gate.pending_text(screen, pane_gate.CLAUDE)), prompt_row, what)
            # Typing is allowed only on a box known empty inside its frame, with nothing unstyled on its
            # prompt row; never on text, never on unknown, never on words that only read like a hint.
            self.assertEqual([m.composer_typed(screen) for m in (rules, bundled)],
                             [pane_gate.pending_text(screen, pane_gate.CLAUDE)] * 2, what)
            self.assertEqual(rules.composer_text(screen) == "" and rules.composer_boxed(screen)
                             and rules.composer_typed(screen) == "",
                             answer == "empty" and boxed and prompt_row == "empty", what)
        self.assertEqual((rules.composer_text(None), rules.composer_boxed(None), rules.composer_typed(None)),
                         (None, False, None), "a failed capture")
        # The same words as a real draft and as the hint Claude Code draws: only the styling differs.
        for words in ('Try "fix lint errors"', "Press up to edit queued messages"):
            self.assertEqual((rules.composer_typed(box(words)), rules.composer_typed(box(DIM + words + OFF))), (words, ""))
        self.assertEqual(rules.composer_text(box("a", " b ")), "a b ", "every row of the box, as the pane gate joins them")

    def test_a_capture_taken_with_styling_reads_as_the_same_screen_without_it(self):
        """`capture-pane -e` colours the rules and the footer. The pane gate is handed plain captures
        for its whole-box reading (a coloured rule reads to it as text: refused, never typed into); the
        model switch takes the styled one, to tell a hint from typed text, through the shared
        frame_plain, and must answer what the pane gate answers for the plain screen."""
        def styled(screen):
            return "\n".join(row if row.startswith("\u276f") else "\x1b[38;5;244m%s\x1b[39m" % row
                             for row in screen.splitlines())

        for what, screen, answer, boxed, _ in SCREENS:
            if not screen:
                continue
            shown = styled(screen)
            self.assertEqual(pane_gate.frame_plain(shown, pane_gate.CLAUDE), screen, what)
            self.assertEqual((kind(rules.composer_text(shown)), rules.composer_boxed(shown)), (answer, boxed), what)
            self.assertEqual(rules.composer_text(shown), pane_gate.composer_text(screen, pane_gate.CLAUDE), what)
        self.assertEqual(kind(pane_gate.composer_text(styled(box("")), pane_gate.CLAUDE)), "text",
                         "unprepared, the pane gate errs toward text: stricter, never looser")

    def test_a_line_counts_only_for_its_own_context_variant(self):
        shown = "Set model to Opus 4.6\nSet model to Opus 4.6 (1M context)\nSet model to Sonnet 4.6"
        self.assertEqual([rules.accepted_count("opus", shown), rules.accepted_count("opus[1m]", shown),
                          rules.accepted_count("haiku", shown)], [1, 1, 0])

    def test_default_names_no_context_variant_so_a_1m_default_still_confirms_it(self):
        for model, line, count in (("default", "Set model to Default (recommended)", 1),
                                   ("default", "Set model to Default (Opus 4.6 with 1M context)", 1),
                                   ("default", "  \u23bf  Set model to default (claude-opus-4-6[1m])", 1),
                                   ("default", "Set model to Opus 4.6 (1M context)", 0),
                                   ("opus[1m]", "Set model to Opus 4.6 (1M context)", 1),
                                   ("opus[1m]", "Set model to Opus 4.6", 0),
                                   ("opus", "Set model to Opus 4.6 (1M context)", 0),
                                   ("claude-opus-4-6", "Set model to Opus 4.6 (1M context)", 0),
                                   ("claude-opus-4-6[1m]", "Set model to Opus 4.6", 0)):
            self.assertEqual(rules.accepted_count(model, "x\n" + line), count, (model, line))

    def test_the_command_line_count_is_as_strict_as_the_function(self):
        def count(model, screen):
            return subprocess.run([sys.executable, str(REPO / "src" / "claude_model_switch.py"), "count", model],
                                  input=screen, capture_output=True, text=True).stdout.strip()

        self.assertEqual(count("opus[1m]", "Set model to Opus 4.6\n"), "0", "plain Opus does not confirm a 1M request")
        self.assertEqual(count("opus", "Set model to Opus 4.6 (1M context)\n"), "0")
        self.assertEqual(count("opus[1m]", "Set model to Opus 4.6 (1M context)\n"), "1")
        self.assertEqual(count("opus", "Set model to Opus 4.6\nSet model to Sonnet 4\n"), "1")
        self.assertEqual(count("default", "Set model to Default (Opus 4.6 with 1M context)\n"), "1",
                         "a default that is itself a 1M model is still the default")
        self.assertEqual(count("default", "Set model to Default (recommended)\nSet model to Opus 4.6 (1M context)\n"), "1")

    def test_the_input_box_is_read_by_the_cores_parser_not_a_copy(self):
        """One parser: the pane gate's names are src/pane_composer.py's own objects, the model switch
        calls the same module, and the Claude data both read with is one glyph and one footer."""
        composer = rules.pane_composer
        for name in ("composer_text", "composer_frame_visible", "composer_boxed", "frame_plain", "prompt_line",
                     "pending_text", "after_prompt", "PromptLine", "_prompt_glyph", "_SGR", "_GHOST", "SGR_STRIP",
                     "BORDER_LINE", "COMPOSER_PLACEHOLDER", "TIP_ROW", "CLAUDE_IDLE"):
            self.assertIs(getattr(pane_gate, name), getattr(composer, name), name)
        self.assertEqual((pane_gate.CLAUDE.glyph, pane_gate.CLAUDE.idle_ready, pane_gate.CLAUDE.alternate_glyphs,
                          pane_gate.CLAUDE.composer_requires_single_line),
                         (composer.CLAUDE_COMPOSER.glyph, composer.CLAUDE_COMPOSER.idle_ready,
                          composer.CLAUDE_COMPOSER.alternate_glyphs, composer.CLAUDE_COMPOSER.composer_requires_single_line))
        source = (REPO / "src" / "claude_model_switch.py").read_text(encoding="utf-8")
        self.assertNotIn("re.compile(r\"\\x1b", source, "no screen pattern of its own")
        self.assertEqual(bundled.pane_composer.__name__, "ag2_sparrow.pane_composer", "the package loads its bundled copy")

    def test_a_1m_switch_answered_without_its_size_counts_only_after_a_fresh_save_naming_it(self):
        # Claude Code 2.1.322, measured 2026-11-22: `/model sonnet[1m]` printed this line, naming no
        # context size, and saved `"model": "sonnet[1m]"` in its settings.json.
        real = "  \u23bf  Set model to Sonnet 5.5 and saved as your default for new sessions"
        self.assertEqual(rules.accepted_count("sonnet[1m]", real), 0, "the line alone is not a [1m] switch")
        self.assertEqual(rules.evidence("sonnet[1m]", real), (0, 1))
        self.assertEqual(rules.evidence("sonnet[1m]", "Set model to Sonnet 5.5 (200k context)"), (0, 0),
                         "a line stating another size contradicts a [1m] switch")
        self.assertEqual(rules.evidence("opus[1m]", real), (0, 0), "another family's line never counts")
        two = real + "\n" + real
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "settings.json"

            def save(model, ns):
                f.write_text(json.dumps({"model": model, "theme": "dark"}))
                os.utime(f, ns=(ns, ns))

            # A fresh save naming the model confirms a NEW size-omitted line.
            save("sonnet", 1_000_000_000)
            base = rules.baseline("sonnet[1m]", real, f)
            save("sonnet[1m]", 2_000_000_000)
            self.assertTrue(rules.settled("sonnet[1m]", two, f, base))
            # Unchanged pane, changed saved setting: no new line, so no switch.
            self.assertFalse(rules.settled("sonnet[1m]", real, f, base))
            # The same value already saved and not saved again: a new line is not confirmed by it.
            base = rules.baseline("sonnet[1m]", real, f)
            self.assertFalse(rules.settled("sonnet[1m]", two, f, base))
            # A fresh save of another model confirms nothing; a new strict line always does.
            save("sonnet", 3_000_000_000)
            self.assertFalse(rules.settled("sonnet[1m]", two, f, base))
            self.assertTrue(rules.settled("sonnet[1m]", real + "\nSet model to Sonnet 5.5 (1M context)", f, base))
            # A plain switch reads its strict line as before, whatever the setting.
            self.assertTrue(rules.settled("sonnet", two, f, rules.baseline("sonnet", real, f)))
            self.assertFalse(rules.settled("sonnet[1m]", two, f, "garbage"), "an unreadable baseline accepts nothing")
            # The command line, as pane-observe.sh calls it.
            cli = [sys.executable, str(REPO / "src" / "claude_model_switch.py")]
            save("sonnet", 4_000_000_000)
            base = subprocess.run(cli + ["baseline", "sonnet[1m]", "--settings", str(f)], input=real + "\n",
                                  capture_output=True, text=True).stdout.strip()
            save("sonnet[1m]", 5_000_000_000)
            out = subprocess.run(cli + ["settled", "sonnet[1m]", "--settings", str(f), "--baseline", base],
                                 input=two + "\n", capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "1", out.stderr)
            # In process, as the command line runs it.
            save("sonnet", 6_000_000_000)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                with unittest.mock.patch.object(sys, "stdin", io.StringIO(real + "\n")):
                    self.assertEqual(rules.main(["baseline", "sonnet[1m]", "--settings", str(f)]), 0)
                base = out.getvalue().strip()
                save("sonnet[1m]", 7_000_000_000)
                with unittest.mock.patch.object(sys, "stdin", io.StringIO(two + "\n")):
                    self.assertEqual(rules.main(["settled", "sonnet[1m]", "--settings", str(f), "--baseline", base]), 0)
            self.assertEqual(out.getvalue().split()[-1], "1")
            for bad in ("not json", "[1]", json.dumps({"model": 5})):
                f.write_text(bad)
                self.assertEqual(rules.saved_setting(f), "", bad)
            # A missing file has a mark of its own, so its first save is fresh.
            missing = Path(d) / "absent.json"
            self.assertTrue(rules.settings_mark(missing).startswith("absent."))
        self.assertEqual(rules.family_count("sonnet[1m]", two + "\nSet model to Sonnet 5.5 (200k context)"), 3,
                         "family_count, kept for the claude-local launcher, counts every size")

    def test_the_shell_scripts_get_the_same_patterns_from_the_command_line(self):
        def run(*argv):
            return subprocess.run([sys.executable, str(REPO / "src" / "claude_model_switch.py"), *argv],
                                  capture_output=True, text=True)

        self.assertEqual(run("model-pattern").stdout.strip(), rules.MODEL_PATTERN)
        self.assertEqual(run("accept", "claude-opus-4-6").stdout.strip(), rules.accept_pattern("claude-opus-4-6"))
        self.assertEqual(run("confirm-text").stdout.strip(), rules.MODEL_CONFIRM)
        for bad in (["accept"], ["accept", "gpt-5"], [], ["model-pattern", "x"]):
            self.assertEqual(run(*bad).returncode, 2, bad)
        # The patterns are plain EREs, so grep -E in the skill reads them as Python does.
        for pattern in (rules.MODEL_PATTERN, rules.accept_pattern("opus"), rules.accept_pattern("claude-opus-4-6")):
            self.assertNotIn("(?", pattern)
            re.compile(pattern)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with unittest.mock.patch.object(sys, "stdin", io.StringIO("Set model to Opus 4.6\n")):
                self.assertEqual(rules.main(["count", "opus"]), 0)
            self.assertEqual((rules.main(["model-pattern"]), rules.main(["accept", "opus"]), rules.main([])), (0, 0, 2))


if __name__ == "__main__":
    unittest.main()
