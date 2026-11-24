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
from ag2_sparrow import diagnostics, result_markers, turn_error  # noqa: E402


class ErrorLineTest(unittest.TestCase):
    def test_a_plain_message_passes_as_one_line(self):
        self.assertEqual(turn_error.error_line("Credit balance is too low"), "Credit balance is too low")
        self.assertEqual(turn_error.error_line("You've hit your usage limit.\n  Try again later."),
                         "You've hit your usage limit. Try again later.")

    def test_only_the_owner_in_a_dm_reads_the_cause(self):
        why = "Credit balance is too low"
        self.assertEqual(turn_error.owner_reason("owner", "dm", why), why)
        self.assertEqual(turn_error.owner_reason("owner", " DM ", why), why)
        for tier in ("team", "guest", "other", None, "", "Owner "):
            self.assertEqual(turn_error.owner_reason(tier, "dm", why), "", tier)
        for kind in ("room", None, "", "group", "dm-ish"):
            self.assertEqual(turn_error.owner_reason("owner", kind, why), "", kind)

    def test_slash_commands_and_plain_word_values_are_kept(self):
        for kept in ("Invalid API key · Please run /login", "OAuth token has expired. Please run /login",
                     "Usage limit reached; try /status or /upgrade", "Token: expired",
                     "Error: 401 Unauthorized - token: invalid_grant", "Please reauth: run codex login",
                     "Your API key: is invalid", "Missing bearer or basic authentication in header",
                     "prompt is too long: 215000 tokens > 200000 maximum"):
            self.assertEqual(turn_error.error_line(kept), kept)

    def test_the_second_review_leftovers_are_removed(self):
        cases = {
            "file:/Users/alice/a.txt": "<path>",
            "open:/Users/alice/x": "open:<path>",
            "/Users/alice/Documents/secret plan.txt": "<path>",
            "Cookie: session=abcd1234efgh5678ijkl": "Cookie: [REDACTED]",
            "pwd=hunter2pass": "pwd=[REDACTED]",
            "pass: hunter2pass": "pass: [REDACTED]",
            "session_id=a1b2c3d4e5f6": "session_id=[REDACTED]",
            'api_key = "abc def ghi"': 'api_key = "[REDACTED]"',
            "api_key=abc,def1234567890": "api_key=[REDACTED]",
            "https://tokenonly@host.example/x": "https://[REDACTED]@host.example/x",
            "https://user:pa/ss@host.example/": "https://[REDACTED]@host.example/",
        }
        for message, want in cases.items():
            self.assertEqual(turn_error.error_line(message), want, message)

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

    def test_secrets_outside_a_query_and_in_url_userinfo_are_removed(self):
        cases = {
            "failed to connect via proxy https://user:hunter2pass@proxy.corp.example:8080/": "hunter2pass",
            "x-api-key: 9f8e7d6c5b4a39281706f5e4d3c2b1a0": "9f8e7d6c5b4a39281706f5e4d3c2b1a0",
            '{"api_key": "abcdef1234567890abcdef1234567890"}': "abcdef1234567890abcdef1234567890",
            "token=abcdef1234567890abcdef": "abcdef1234567890abcdef",
            "https://x.test/cb#access_token=FRAGTOKEN123456": "FRAGTOKEN123456",
            "authorization=Bearer:abc123secretvalue": "abc123secretvalue",
        }
        for message, secret in cases.items():
            line = turn_error.error_line(message)
            self.assertNotIn(secret, line, message)
            self.assertIn("[REDACTED]", line, message)
        self.assertEqual(turn_error.error_line("https://user:pw@proxy.example:8080/ refused"),
                         "https://[REDACTED]@proxy.example:8080/ refused")
        for kept in ("authentication_failed: invalid request", "max_tokens: 4096 exceeded"):
            self.assertEqual(turn_error.error_line(kept), kept)

    def test_control_characters_are_dropped(self):
        self.assertEqual(turn_error.error_line("\x1b[31mCredit balance\x1b[0m is too low"), "Credit balance is too low")
        self.assertEqual(turn_error.error_line("a\x00b\x07c"), "abc")
        self.assertEqual(turn_error.error_line("evil‮txt.exe"), "eviltxt.exe")
        self.assertEqual(turn_error.error_line("a\x0bb\x0cc\x85d e"), "a b c d e")

    def test_delivery_markers_in_the_error_are_inert(self):
        for message in ("stream error [file: notes/private.md]", "oops [dm-only] [channel: 123] [no-send]",
                        "bad [send: /tmp/x] [attach: a.txt] [REPLIED]"):
            line = turn_error.error_line(message)
            self.assertEqual(result_markers.parse_markers(line).actions, [], line)
            body = "Codex ended this task with status failed: %s." % line
            self.assertEqual(result_markers.parse_markers(body).actions, [], body)
        self.assertEqual(turn_error.error_line("stream error [file: notes/private.md]"),
                         "stream error [ file: notes/private.md]")

    def test_local_paths_are_removed(self):
        line = turn_error.error_line("cannot read /Users/someone/.codex/auth.json: denied; see ~/.claude/x or "
                                     "C:\\Users\\someone\\a.txt")
        for path in ("/Users/someone", "~/.claude", "C:\\Users"):
            self.assertNotIn(path, line)
        self.assertIn("<path>: denied", line)
        self.assertEqual(turn_error.error_line("see https://status.example/a/b"), "see https://status.example/a/b")

    def test_every_path_shape_is_removed(self):
        cases = {
            "cannot open /Users/alice/Library/Application Support/Codex/auth.json": "cannot open <path>",
            "file:///Users/alice/a.txt": "<path>",
            "C:/Users/alice/x.json": "<path>",
            "\\\\server\\share\\alice\\f.txt": "<path>",
            "$HOME/alice/x": "<path>",
            "%USERPROFILE%\\alice\\x": "<path>",
            "open /secret.txt failed": "open <path> failed",
        }
        for message, want in cases.items():
            self.assertEqual(turn_error.error_line(message), want, message)
        self.assertEqual(turn_error.error_line("rate 10/min and/or 1/2"), "rate 10/min and/or 1/2")


if __name__ == "__main__":
    unittest.main()
