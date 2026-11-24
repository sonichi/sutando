"""turn_error: the one line a room may read about why a local agent's turn failed.

Run:
    python3 packages/ag2-sparrow/tests/test_turn_error.py
"""
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# The registered-secret case loads diagnostics, which reads gateway credentials: no outer selector survives.
isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import diagnostics, turn_error  # noqa: E402


class ErrorLineTest(unittest.TestCase):
    def test_a_plain_message_passes_as_one_line(self):
        self.assertEqual(turn_error.error_line("Credit balance is too low"), "Credit balance is too low")
        self.assertEqual(turn_error.error_line("You've hit your usage limit.\n  Try again later."),
                         "You've hit your usage limit. Try again later.")

    def test_nothing_usable_is_empty(self):
        for message in (None, "", "  \n\t", {"message": "x"}, 3):
            self.assertEqual(turn_error.error_line(message), "", message)

    def test_it_is_bounded(self):
        line = turn_error.error_line("y" * 1000)
        self.assertEqual(len(line), turn_error.LIMIT)
        self.assertTrue(line.endswith("\u2026"))

    def test_secrets_are_removed(self):
        key = "sk-ant-api03-" + "A" * 40
        cases = {
            "invalid x-api-key " + key: key,
            "GET https://api.example/v1?api_key=hunter2&q=1 failed": "hunter2",
            "Authorization: Bearer abc.def.ghi rejected": "abc.def.ghi",
            "token ghp_" + "b" * 36 + " expired": "ghp_" + "b" * 36,
        }
        for message, secret in cases.items():
            line = turn_error.error_line(message)
            self.assertNotIn(secret, line, message)
            self.assertTrue(line, message)

    def test_a_registered_secret_withholds_the_line(self):
        diagnostics.register_secret("registered-relay-secret-value")
        self.assertEqual(turn_error.error_line("refused: registered-relay-secret-value"), diagnostics.WITHHELD_TEXT)

    def test_local_paths_are_removed(self):
        line = turn_error.error_line("cannot read /Users/someone/.codex/auth.json: denied; see ~/.claude/x or "
                                     "C:\\Users\\someone\\a.txt")
        for path in ("/Users/someone", "~/.claude", "C:\\Users"):
            self.assertNotIn(path, line)
        self.assertIn("<path>: denied", line)
        self.assertEqual(turn_error.error_line("see https://status.example/a/b"), "see https://status.example/a/b")


if __name__ == "__main__":
    unittest.main()
