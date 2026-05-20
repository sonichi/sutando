#!/usr/bin/env python3
"""
Unit tests for src/result_markers.py — the unified result-body marker parser
that closes #873.

Covers:
  - SKIP markers ([no-send], [REPLIED], [deduped: <id>]) at body start
  - REDIRECT marker ([channel: <id>]) at body start
  - ATTACH markers ([file:], [send:], [attach:]) anywhere in body
  - Precedence (skip beats redirect beats attach)
  - Edge cases: empty body, whitespace before skip, malformed markers, etc.
  - "No marker ever leaks as literal text in body" — the load-bearing invariant

Run: python3 tests/result-markers.test.py
Exit code: 0 on pass, 1 on fail.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from result_markers import parse_markers, first_action  # noqa: E402


class TestSkipMarkers(unittest.TestCase):
    def test_no_send_at_start(self):
        r = parse_markers("[no-send]\nthis is ignored")
        self.assertEqual(r.body, "")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "skip")
        self.assertEqual(r.actions[0].value, "no-send")

    def test_replied_at_start(self):
        r = parse_markers("[REPLIED]\nalready handled")
        self.assertEqual(r.body, "")
        self.assertEqual(r.actions[0].value, "REPLIED")

    def test_deduped_captures_task_id(self):
        r = parse_markers("[deduped: task-1779164273868]\nfull reply elsewhere")
        self.assertEqual(r.body, "")
        self.assertEqual(r.actions[0].value, "deduped")
        self.assertEqual(r.actions[0].extra, "task-1779164273868")

    def test_skip_strips_leading_whitespace(self):
        r = parse_markers("  [no-send]\nbody")
        self.assertEqual(r.actions[0].kind, "skip")

    def test_skip_case_insensitive_for_no_send_and_deduped(self):
        r1 = parse_markers("[NO-SEND]\nx")
        r2 = parse_markers("[DEDUPED: task-1]\nx")
        self.assertEqual(r1.actions[0].value, "no-send")
        self.assertEqual(r2.actions[0].value, "deduped")

    def test_replied_case_sensitive(self):
        # [REPLIED] in caps is the canonical form. lowercase shouldn't match.
        r = parse_markers("[replied]\nbody")
        self.assertEqual(r.actions, [])
        self.assertIn("[replied]", r.body)


class TestRedirectMarker(unittest.TestCase):
    def test_redirect_at_start_strips_marker(self):
        r = parse_markers("[channel: C09TEUW5DE1]\nhello team")
        self.assertEqual(r.body, "hello team")
        self.assertEqual(r.actions[0].kind, "redirect")
        self.assertEqual(r.actions[0].value, "C09TEUW5DE1")

    def test_redirect_discord_numeric_channel(self):
        r = parse_markers("[channel: 1499520683267592432]\nRFC announcement")
        self.assertEqual(r.actions[0].value, "1499520683267592432")
        self.assertEqual(r.body, "RFC announcement")

    def test_redirect_only_at_start(self):
        # An attach-style marker in middle of body doesn't get parsed as redirect
        r = parse_markers("body talking about [channel: 12345] inline")
        self.assertEqual(first_action(r, "redirect"), None)
        # And the literal text stays (bridges may strip if they want)
        self.assertIn("[channel: 12345]", r.body)


class TestAttachMarkers(unittest.TestCase):
    def test_file_marker(self):
        r = parse_markers("here it is [file: /tmp/sutando-a.png]")
        self.assertEqual(r.actions[0].kind, "attach")
        self.assertEqual(r.actions[0].value, "/tmp/sutando-a.png")
        self.assertEqual(r.body, "here it is")

    def test_send_marker(self):
        r = parse_markers("[send: /docs/x.pdf] check this")
        self.assertEqual(r.actions[0].value, "/docs/x.pdf")
        self.assertEqual(r.body, "check this")

    def test_attach_marker(self):
        r = parse_markers("done [attach: /notes/y.md]")
        self.assertEqual(r.actions[0].value, "/notes/y.md")

    def test_multiple_attaches_document_order(self):
        r = parse_markers("a [file: /a] b [send: /b] c [attach: /c] d")
        paths = [a.value for a in r.actions if a.kind == "attach"]
        self.assertEqual(paths, ["/a", "/b", "/c"])

    def test_attach_markers_stripped_from_body(self):
        r = parse_markers("here is [file: /a]")
        self.assertNotIn("[file:", r.body)
        self.assertNotIn("/a]", r.body)


class TestPrecedence(unittest.TestCase):
    def test_skip_beats_redirect(self):
        r = parse_markers("[no-send]\n[channel: C123]\nbody")
        # Skip is terminal — no redirect should be parsed.
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "skip")

    def test_skip_beats_attach(self):
        r = parse_markers("[deduped: task-1]\n[file: /x]")
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0].kind, "skip")

    def test_redirect_plus_attach_coexist(self):
        r = parse_markers("[channel: C123]\nbody [file: /tmp/sutando-x.png]")
        kinds = [a.kind for a in r.actions]
        self.assertEqual(kinds, ["redirect", "attach"])
        self.assertEqual(r.body, "body")


class TestEdgeCases(unittest.TestCase):
    def test_empty_body(self):
        r = parse_markers("")
        self.assertEqual(r.body, "")
        self.assertEqual(r.actions, [])

    def test_plain_text_no_markers(self):
        r = parse_markers("just a normal reply")
        self.assertEqual(r.body, "just a normal reply")
        self.assertEqual(r.actions, [])

    def test_malformed_skip_does_not_match(self):
        # Missing closing bracket — should be literal text, not parsed
        r = parse_markers("[no-send\nbody")
        self.assertEqual(r.actions, [])
        self.assertIn("[no-send", r.body)

    def test_first_action_helper(self):
        r = parse_markers("[channel: C1]\n[file: /a] [file: /b]")
        self.assertEqual(first_action(r, "redirect").value, "C1")
        self.assertEqual(first_action(r, "attach").value, "/a")
        self.assertEqual(first_action(r, "skip"), None)


class TestNoLeakInvariant(unittest.TestCase):
    """The load-bearing claim of #873: no marker ever leaks as literal text
    in the parsed `body` field. Whatever a bridge passes through, the user
    sees clean output.
    """

    def test_no_attach_marker_in_body(self):
        r = parse_markers("body [file: /a] [send: /b] [attach: /c] end")
        for marker in ("[file:", "[send:", "[attach:"):
            self.assertNotIn(marker, r.body)

    def test_no_redirect_marker_in_body_when_at_start(self):
        r = parse_markers("[channel: C1]\nhello")
        self.assertNotIn("[channel:", r.body)

    def test_skip_strips_entire_body(self):
        # Body is "" for skips so no leak possible.
        for prefix in ("[no-send]", "[REPLIED]", "[deduped: task-x]"):
            r = parse_markers(f"{prefix}\nthis is internal")
            self.assertEqual(r.body, "")


if __name__ == "__main__":
    unittest.main()
