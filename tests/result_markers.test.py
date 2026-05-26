"""Tests for result_markers.py — parse_markers and first_action."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from result_markers import Action, ParseResult, first_action, parse_markers


class TestParseMarkersEmpty(unittest.TestCase):
    def test_empty_string(self):
        r = parse_markers("")
        self.assertEqual(r.body, "")
        self.assertEqual(r.actions, [])

    def test_body_only_no_markers(self):
        r = parse_markers("Hello world")
        self.assertEqual(r.body, "Hello world")
        self.assertEqual(r.actions, [])

    def test_whitespace_only(self):
        r = parse_markers("   \n  ")
        self.assertEqual(r.actions, [])


class TestParseMarkersSkip(unittest.TestCase):
    def test_no_send_marker(self):
        r = parse_markers("[no-send]\nbody text")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "skip")
        self.assertEqual(r.actions[0].value, "no-send")
        self.assertEqual(r.body, "")

    def test_no_send_case_insensitive(self):
        r = parse_markers("[NO-SEND]\nbody")
        self.assertEqual(r.actions[0].kind, "skip")
        self.assertEqual(r.actions[0].value, "no-send")

    def test_replied_marker(self):
        r = parse_markers("[REPLIED]\nbody")
        self.assertEqual(r.actions[0].kind, "skip")
        self.assertEqual(r.actions[0].value, "REPLIED")

    def test_deduped_marker_extracts_task_id(self):
        r = parse_markers("[deduped: task-12345]\nbody")
        self.assertEqual(r.actions[0].kind, "skip")
        self.assertEqual(r.actions[0].value, "deduped")
        self.assertEqual(r.actions[0].extra, "task-12345")

    def test_deduped_strips_whitespace_from_task_id(self):
        r = parse_markers("[deduped:  task-99 ]\nbody")
        self.assertEqual(r.actions[0].extra, "task-99")

    def test_skip_is_terminal_ignores_attach(self):
        r = parse_markers("[no-send]\n[file: /tmp/out.png]")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "skip")

    def test_skip_is_terminal_ignores_redirect(self):
        r = parse_markers("[no-send]\n[channel: 123456789]\nbody")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "skip")

    def test_skip_returns_empty_body(self):
        r = parse_markers("[REPLIED]\nsome content here")
        self.assertEqual(r.body, "")

    def test_no_send_with_leading_whitespace(self):
        r = parse_markers("  [no-send]\nbody")
        self.assertEqual(r.actions[0].kind, "skip")


class TestParseMarkersRedirect(unittest.TestCase):
    def test_redirect_discord_channel_id(self):
        r = parse_markers("[channel: 1234567890123456789]\nbody text")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "redirect")
        self.assertEqual(r.actions[0].value, "1234567890123456789")

    def test_redirect_slack_channel_id(self):
        r = parse_markers("[channel: C082ABSMWFP]\nbody text")
        self.assertEqual(r.actions[0].kind, "redirect")
        self.assertEqual(r.actions[0].value, "C082ABSMWFP")

    def test_redirect_body_excludes_marker_line(self):
        r = parse_markers("[channel: 123]\nHello user")
        self.assertNotIn("[channel:", r.body)
        self.assertIn("Hello user", r.body)

    def test_redirect_strips_channel_id_whitespace(self):
        r = parse_markers("[channel:  C082  ]\nbody")
        self.assertEqual(r.actions[0].value, "C082")

    def test_redirect_plus_attach(self):
        r = parse_markers("[channel: 123]\nbody\n[file: /tmp/x.png]")
        kinds = [a.kind for a in r.actions]
        self.assertIn("redirect", kinds)
        self.assertIn("attach", kinds)
        self.assertEqual(kinds[0], "redirect")


class TestParseMarkersAttach(unittest.TestCase):
    def test_file_marker(self):
        r = parse_markers("done\n[file: /tmp/out.png]")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "attach")
        self.assertEqual(r.actions[0].value, "/tmp/out.png")

    def test_send_alias(self):
        r = parse_markers("body\n[send: /tmp/report.pdf]")
        self.assertEqual(r.actions[0].kind, "attach")
        self.assertEqual(r.actions[0].value, "/tmp/report.pdf")

    def test_attach_alias(self):
        r = parse_markers("body\n[attach: /tmp/photo.jpg]")
        self.assertEqual(r.actions[0].kind, "attach")
        self.assertEqual(r.actions[0].value, "/tmp/photo.jpg")

    def test_multiple_attach_markers_document_order(self):
        r = parse_markers("text\n[file: /a.png]\n[file: /b.png]")
        attach_paths = [a.value for a in r.actions if a.kind == "attach"]
        self.assertEqual(attach_paths, ["/a.png", "/b.png"])

    def test_attach_markers_stripped_from_body(self):
        r = parse_markers("Hello\n[file: /tmp/x.png]\nWorld")
        self.assertNotIn("[file:", r.body)
        self.assertNotIn("[send:", r.body)
        self.assertNotIn("[attach:", r.body)

    def test_attach_path_whitespace_stripped(self):
        r = parse_markers("[file:  /tmp/out.png  ]")
        self.assertEqual(r.actions[0].value, "/tmp/out.png")

    def test_body_text_preserved_after_attach_strip(self):
        r = parse_markers("Result: success\n[file: /tmp/x.png]\nDone.")
        self.assertIn("Result: success", r.body)
        self.assertIn("Done.", r.body)


class TestParseMarkersD7Header(unittest.TestCase):
    def test_d7_header_preserved_in_body(self):
        text = "**[core: 1]**\nmain text"
        r = parse_markers(text)
        self.assertIn("**[core: 1]**", r.body)
        self.assertIn("main text", r.body)

    def test_d7_header_doesnt_shadow_redirect(self):
        text = "**[core: 2]**\n[channel: C082]\nHello"
        r = parse_markers(text)
        kinds = [a.kind for a in r.actions]
        self.assertIn("redirect", kinds)
        self.assertIn("**[core: 2]**", r.body)
        self.assertIn("Hello", r.body)

    def test_d7_header_with_italic_subline(self):
        text = "**[core: 1]**\n_(primary)_\nbody text"
        r = parse_markers(text)
        self.assertIn("body text", r.body)
        self.assertEqual(r.actions, [])

    def test_d7_header_with_skip_marker_discards_header(self):
        text = "**[core: 1]**\n[no-send]\nbody"
        r = parse_markers(text)
        self.assertEqual(r.actions[0].kind, "skip")
        self.assertEqual(r.body, "")


class TestFirstAction(unittest.TestCase):
    def test_returns_first_matching_action(self):
        result = ParseResult(
            body="body",
            actions=[
                Action(kind="attach", value="/a.png"),
                Action(kind="attach", value="/b.png"),
            ],
        )
        a = first_action(result, "attach")
        self.assertEqual(a.value, "/a.png")

    def test_returns_none_when_no_match(self):
        result = ParseResult(body="body", actions=[Action(kind="attach", value="/x")])
        self.assertIsNone(first_action(result, "redirect"))

    def test_returns_none_on_empty_actions(self):
        result = ParseResult(body="body", actions=[])
        self.assertIsNone(first_action(result, "skip"))

    def test_skip_found_among_mixed_actions(self):
        result = ParseResult(
            body="",
            actions=[Action(kind="skip", value="no-send")],
        )
        a = first_action(result, "skip")
        self.assertIsNotNone(a)
        self.assertEqual(a.value, "no-send")


if __name__ == "__main__":
    unittest.main()
