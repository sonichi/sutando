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

    def test_deduped_empty_target_parses_like_its_spaced_twin(self):
        # One space apart: the spaced form parsed, the bare one did not, so
        # the bare one shipped its marker text and body to the channel.
        for src in ("[deduped:]\nfull reply elsewhere",
                    "[deduped: ]\nfull reply elsewhere"):
            with self.subTest(src=src.splitlines()[0]):
                r = parse_markers(src)
                self.assertEqual(len(r.actions), 1, "must parse as exactly one skip")
                self.assertEqual(r.actions[0].kind, "skip")
                self.assertEqual(r.actions[0].value, "deduped")
                self.assertEqual(r.actions[0].extra, "")
                self.assertNotIn("[deduped", r.body)

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


class TestDmOnlyMarker(unittest.TestCase):
    """The `[dm-only]` privacy guard: forces DM, suppresses any [channel:]
    redirect so private content (calendar/email briefing) can't be posted to a
    shared channel. Added 2026-07-18 after a morning briefing leaked to a
    channel via a [channel:] redirect."""

    def test_dm_only_emits_action_and_strips_marker(self):
        r = parse_markers("[dm-only]\nCalendar today: 7:30am write")
        self.assertEqual(r.body, "Calendar today: 7:30am write")
        self.assertEqual(first_action(r, "dm-only").kind, "dm-only")
        # No literal marker leaks into the delivered body.
        self.assertNotIn("[dm-only]", r.body)

    def test_dm_only_suppresses_redirect_marker_first(self):
        # BEFORE (no dm-only): the [channel:] redirect IS honored →
        # private content would be posted to the channel.
        before = parse_markers("[channel: 1527723291324842135]\nCalendar: 7:30am")
        self.assertEqual(first_action(before, "redirect").value, "1527723291324842135")

        # AFTER (dm-only present, redirect first): NO redirect action is
        # emitted — the body stays in the owner's DM — and both markers are
        # stripped so neither leaks.
        after = parse_markers("[dm-only]\n[channel: 1527723291324842135]\nCalendar: 7:30am")
        self.assertIsNone(first_action(after, "redirect"))
        self.assertEqual(first_action(after, "dm-only").kind, "dm-only")
        self.assertEqual(after.body, "Calendar: 7:30am")
        self.assertNotIn("[channel:", after.body)
        self.assertNotIn("[dm-only]", after.body)

    def test_dm_only_suppresses_redirect_regardless_of_order(self):
        # dm-only AFTER the channel marker still wins (matched anywhere).
        r = parse_markers("[channel: 1527723291324842135]\nsecret [dm-only] stuff")
        self.assertIsNone(first_action(r, "redirect"))
        self.assertEqual(first_action(r, "dm-only").kind, "dm-only")
        # The marker STAYS in the body here, on purpose: it is inline prose,
        # and stripping it rewrote the owner's sentence ("secret  stuff").
        # Only a standalone marker is stripped now. Detection is unchanged —
        # the two assertions above, which are what this test is named for,
        # still prove dm-only wins regardless of order.
        self.assertIn("[dm-only]", r.body)
        self.assertNotIn("[channel:", r.body)

    def test_skip_still_beats_dm_only(self):
        # A skipped body is delivered nowhere; dm-only is moot but must not
        # break the terminal skip.
        r = parse_markers("[no-send]\n[dm-only]\nnothing")
        self.assertEqual(r.body, "")
        self.assertEqual(r.actions[0].kind, "skip")


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

    def test_empty_target_markers_never_leak_in_any_family(self):
        # Redirect expects NO action — empty target is not a target (pinned
        # end-to-end in empty-redirect-target-default-route.test.py).
        for src, kinds in (("[deduped:]\nbody", ["skip"]),
                           ("[channel:]\nbody", []),
                           ("[file:]\nbody", ["attach"])):
            with self.subTest(src=src.splitlines()[0]):
                r = parse_markers(src)
                self.assertEqual([a.kind for a in r.actions], kinds)
                for marker in ("[deduped:", "[channel:", "[file:"):
                    self.assertNotIn(marker, r.body)

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


class TestD7HeaderTolerance(unittest.TestCase):
    """D7 (owner directive 2026-05-19) prepends `**[core: N]**` + optional
    italic sub-line to every owner-facing reply. The header sits at byte 0,
    which previously shadowed the redirect regex (anchored at body start).
    The parser now peels the header off before marker scanning and re-stitches
    it onto the returned body — markers fire correctly, header stays visible.
    """

    def test_d7_header_does_not_shadow_redirect(self):
        text = "**[core: 2]**\n\n[channel: C09XYZ]\nHello redirected."
        r = parse_markers(text)
        self.assertEqual(first_action(r, "redirect").value, "C09XYZ")
        # Header preserved in user-facing body.
        self.assertTrue(r.body.startswith("**[core: 2]**"))
        # Redirect line itself stripped.
        self.assertNotIn("[channel:", r.body)
        # Discord channel-id form also accepted.
        r2 = parse_markers("**[core: 1]**\n\n[channel: 1506182697142325298]\nx")
        self.assertEqual(first_action(r2, "redirect").value, "1506182697142325298")

    def test_d7_header_with_italic_subline(self):
        text = (
            "**[core: 2]**\n"
            "_(channel→core handler switch from core-1)_\n"
            "\n"
            "[channel: C09XYZ]\n"
            "Body."
        )
        r = parse_markers(text)
        self.assertEqual(first_action(r, "redirect").value, "C09XYZ")
        # Both header lines preserved.
        self.assertIn("**[core: 2]**", r.body)
        self.assertIn("_(channel→core handler switch from core-1)_", r.body)

    def test_d7_header_without_marker_passes_through(self):
        text = "**[core: 2]**\n\nJust a normal reply, no markers."
        r = parse_markers(text)
        self.assertEqual(r.actions, [])
        # Body unchanged (header + content intact).
        self.assertEqual(r.body, text)

    def test_d7_plus_skip_keeps_skip_terminal(self):
        # Skip markers are invisible to the user — when combined with a D7
        # header, the header is discarded along with the body. Otherwise the
        # bridge would deliver a header-only message with no content.
        text = "**[core: 2]**\n\n[no-send]\nthis-is-internal"
        r = parse_markers(text)
        self.assertEqual(first_action(r, "skip").value, "no-send")
        self.assertEqual(r.body, "")

    def test_d7_plus_deduped_keeps_skip_terminal(self):
        text = "**[core: 2]**\n[deduped: task-1779164273868]\nfull elsewhere"
        r = parse_markers(text)
        skip = first_action(r, "skip")
        self.assertEqual(skip.value, "deduped")
        self.assertEqual(skip.extra, "task-1779164273868")
        self.assertEqual(r.body, "")

    def test_d7_plus_redirect_plus_attach(self):
        text = (
            "**[core: 2]**\n"
            "\n"
            "[channel: C09XYZ]\n"
            "[file: /tmp/x.txt]\n"
            "Body with attachment."
        )
        r = parse_markers(text)
        kinds = [a.kind for a in r.actions]
        self.assertEqual(kinds, ["redirect", "attach"])
        self.assertEqual(first_action(r, "attach").value, "/tmp/x.txt")
        # No marker leaks; header still in body.
        self.assertNotIn("[channel:", r.body)
        self.assertNotIn("[file:", r.body)
        self.assertIn("**[core: 2]**", r.body)


class UnknownAttachKeywords(unittest.TestCase):
    """Only file/send/attach are attachment aliases.

    Migrated from tests/discord-bridge-file-markers.test.py, which tested the
    Discord bridge's private `_split_file_markers()` helper. That helper is
    gone (the grammar now lives solely here), so the case moved to the
    canonical suite rather than being dropped.

    NOTE: that file's `test_relative_path_does_not_match` was deliberately NOT
    migrated. It asserted the *legacy* private regex's `(?:/|~/)`-only shape.
    The canonical parser extracts any marker value and defers path
    authorization to src/send_allowlist.py, so a relative value is recognised,
    stripped from the body, and then rejected at the allowlist — see
    tests/dm-result-adoption-gap.test.py.
    """

    def test_unknown_keyword_is_not_an_attachment(self):
        for bad in ("reply", "foo", "channel-x", "files"):
            with self.subTest(keyword=bad):
                r = parse_markers(f"body [{bad}: /tmp/sutando-x.png]")
                self.assertEqual([a for a in r.actions if a.kind == "attach"], [])

    def test_unknown_keyword_is_left_in_the_body(self):
        """Not an attachment marker => not our protocol => don't mangle prose."""
        text = "body [reply: 12345678901234567890]"
        self.assertEqual(parse_markers(text).body, text)

    def test_aliases_extract_and_strip(self):
        for kw in ("file", "send", "attach"):
            with self.subTest(alias=kw):
                r = parse_markers(f"body [{kw}: /tmp/sutando-x.png]")
                self.assertEqual(
                    [a.value for a in r.actions if a.kind == "attach"],
                    ["/tmp/sutando-x.png"],
                )
                self.assertNotIn(f"[{kw}:", r.body)


class BacktickedMarkerIsProse(unittest.TestCase):
    """A marker inside a markdown code span is prose ABOUT the feature.

    Treating it as a directive strips it from the sentence and pushes an
    invented path at the allowlist. Inline markers OUTSIDE backticks stay
    directives -- that shape is relied on and pinned elsewhere in this file.
    """

    def _attach(self, body):
        return [a.value for a in parse_markers(body).actions if a.kind == "attach"]

    def test_backticked_marker_is_not_an_action(self):
        self.assertEqual(self._attach("the `[file: /path]` marker uploads"), [])

    def test_backticked_marker_survives_in_the_body(self):
        body = "explain: `[attach: /p]` sends a file."
        self.assertEqual(parse_markers(body).body, body)

    def test_bare_inline_marker_is_still_a_directive(self):
        self.assertEqual(self._attach("see this [file: /tmp/x.png] thanks"),
                         ["/tmp/x.png"])

    def test_standalone_marker_is_still_a_directive(self):
        self.assertEqual(self._attach("text\n[send: /tmp/a.png]\n"), ["/tmp/a.png"])

    def test_marker_in_the_middle_of_a_span_is_prose(self):
        # Backtick-ADJACENCY is only a proxy for span membership: a marker that
        # sits inside a longer span touches no backtick.
        self.assertEqual(self._attach("run `foo [file: /p.png] bar` now"), [])
        self.assertEqual(self._attach("run ``foo [file: /p.png] bar`` now"), [])

    def test_fenced_block_is_prose(self):
        # The commonest way to DOCUMENT the marker, so the case that matters most.
        for fence in ("```", "~~~"):
            self.assertEqual(
                self._attach(f"example:\n{fence}\n[file: /p.png]\n{fence}\ndone"), [])

    def test_indented_block_is_prose(self):
        self.assertEqual(self._attach("example:\n\n    [file: /p.png]\n\ndone"), [])
        self.assertEqual(self._attach("example:\n\n\t[file: /p.png]\n\ndone"), [])

    def test_code_block_does_not_swallow_a_later_directive(self):
        r = self._attach("```\nshown [file: /a.png]\n```\nreal:\n[file: /b.png]")
        self.assertEqual(r, ["/b.png"])

    def test_unmatched_backtick_does_not_disarm_a_directive(self):
        # A stray backtick opens no span; failing the other way would silently
        # drop a real attachment.
        self.assertEqual(self._attach("a ` stray and [file: /b.png]"), ["/b.png"])


if __name__ == "__main__":
    unittest.main()
