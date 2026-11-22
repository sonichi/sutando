#!/usr/bin/env python3
"""src/claude_model_switch.py: the one source of what typing /model into Claude Code relies on, for
the core's model-switch skill (through its command line) and for ag2-sparrow's claude-local adapter."""
import contextlib
import io
import re
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import claude_model_switch as rules  # noqa: E402


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

    def test_the_input_box_is_the_rows_between_its_two_rules_and_unknown_when_it_is_not_whole(self):
        rule, dim, grey = "\u2500" * 30, "\x1b[2m", "\x1b[38;5;240m"

        def box(*rows):
            return "\n".join([rule, "\u276f " + rows[0], *rows[1:], rule, "  footer"])

        for shown, text in ((None, None), ("", None), ("no prompt here", None), ("\u276f ", None),
                            (rule + "\n\u276f ", None), (rule + "\n" + rule, None),
                            ("Select model\n\u276f 1. Default\n  2. Opus", None),
                            (box(""), ""), (box("hi "), "hi"), (box("", "  second"), "second"), (box("a", "b"), "a\nb"),
                            ("old\n" + box("") + "\n" + box("fix the bug"), "fix the bug"),
                            (box('%sTry "fix lint errors"\x1b[0m' % dim), ""),
                            (rule + "\n\u276f\u00a0%srun the tests\x1b[39m\n" % grey + rule, "")):
            self.assertEqual(rules.composer_text(shown), text, shown)

    def test_a_line_counts_for_a_context_variant_only_when_asked_to_tell_them_apart(self):
        shown = "Set model to Opus 4.6\nSet model to Opus 4.6 (1M context)\nSet model to Sonnet 4.6"
        self.assertEqual([rules.accepted_count("opus", shown), rules.accepted_count("opus", shown, True),
                          rules.accepted_count("opus[1m]", shown, True), rules.accepted_count("haiku", shown)], [2, 1, 1, 0])

    def test_the_shell_scripts_get_the_same_patterns_from_the_command_line(self):
        def run(*argv):
            return subprocess.run([sys.executable, str(REPO / "src" / "claude_model_switch.py"), *argv],
                                  capture_output=True, text=True)

        self.assertEqual(run("model-pattern").stdout.strip(), rules.MODEL_PATTERN)
        self.assertEqual(run("accept", "claude-opus-4-6").stdout.strip(), rules.accept_pattern("claude-opus-4-6"))
        counted = subprocess.run([sys.executable, str(REPO / "src" / "claude_model_switch.py"), "count", "opus"],
                                 input="Set model to Opus 4.6\nSet model to Sonnet 4\n", capture_output=True, text=True)
        self.assertEqual(counted.stdout.strip(), "1")
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
