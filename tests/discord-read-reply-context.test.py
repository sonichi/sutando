#!/usr/bin/env python3
"""A terse reply is uninterpretable without its target, and this reader dropped it.

`discord-read.py` is the reader `context-reconstruct` runs on every proactive-loop
pass. It rendered a reply as bare text with nothing indicating what was answered.

Measured 2026-08-04 in the owner channel — both of these are replies, and the
Discord API returns `referenced_message` for both:

    sonichi: "2 merge"
    sonichi: "2y\\n3 I didn't delete it."

Read from the channel alone, neither means anything. They only parsed because the
*task file* carried a `[Replying to ...]` block — and that block exists only when
a message becomes a task. Catching up on history, or reading a channel where no
task was created, the referent is simply gone.

A peer hit the consequence the same day: it missed that a bare `2` on its own line
was the answer to an enumerated question, re-asked the owner twice, and got "What
are you waiting for".

Labelled rather than inlined, matching how forwards are handled two functions up:
attributing a quoted message to the replier is its own misreading.
"""
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "discord-read.py"

_spec = importlib.util.spec_from_file_location("discord_read", SCRIPT)
dr = importlib.util.module_from_spec(_spec)
sys.modules["discord_read"] = dr
try:
    _spec.loader.exec_module(dr)
except SystemExit:
    pass


def reply(content, ref_body, ref_author="Sutando-Mini", author="sonichi"):
    return {"content": content, "author": {"username": author},
            "referenced_message": {"content": ref_body,
                                   "author": {"username": ref_author}}}


class ReplyContext(unittest.TestCase):
    def test_the_real_owner_reply_now_carries_its_target(self):
        """THE pin — the exact message that motivated this. Fails on parent."""
        ctx = dr._reply_context(reply("2 merge", "…**2 — holding, and here's why.**…"))
        self.assertIsNotNone(ctx)
        self.assertIn("replying to Sutando-Mini", ctx)
        self.assertIn("holding", ctx)

    def test_body_is_unchanged(self):
        """The reply's own text must render exactly as before — this is additive."""
        self.assertEqual(dr._render(reply("2 merge", "anything")), "2 merge")

    def test_multiline_target_is_flattened_to_one_line(self):
        """A multi-line target must not break the one-message-per-line shape that
        every downstream reader (and my own greps) assume."""
        ctx = dr._reply_context(reply("2y", "line one\n\nline two\n  line three"))
        self.assertNotIn("\n", ctx)
        self.assertIn("line one line two line three", ctx)

    def test_target_is_clipped_harder_than_a_body(self):
        """The point is to identify WHICH message, not to re-read it."""
        ctx = dr._reply_context(reply("k", "x" * 900))
        self.assertLessEqual(len(ctx), 160)
        self.assertLess(dr.REPLY_CLIP, dr.CLIP)

    def test_non_reply_returns_none(self):
        """No extra line for ordinary messages — the common case must not get noisier."""
        self.assertIsNone(dr._reply_context({"content": "hi", "author": {"username": "x"}}))

    def test_malformed_reference_does_not_raise(self):
        """A reader that crashes on odd input is worse than one that omits context;
        context-reconstruct runs this every pass."""
        for bad in (None, "not-a-dict", [], 42):
            with self.subTest(bad=bad):
                self.assertIsNone(dr._reply_context({"content": "x", "referenced_message": bad}))

    def test_empty_target_body_still_names_the_author(self):
        """An attachment-only or forwarded target has no text — naming who it was
        still identifies the referent."""
        ctx = dr._reply_context(reply("2", ""))
        self.assertIn("Sutando-Mini", ctx)
        self.assertIn("no readable body", ctx)

    def test_a_forwarded_target_uses_the_same_renderer(self):
        """Targets go through _render, so the forward fix above applies to them too
        — otherwise replying to a forward reintroduces the blank-line bug."""
        msg = {"content": "?", "author": {"username": "sonichi"},
               "referenced_message": {
                   "content": "", "author": {"username": "Sutando-Pro"},
                   "message_snapshots": [{"message": {"content": "the forwarded substance"}}]}}
        ctx = dr._reply_context(msg)
        self.assertIn("the forwarded substance", ctx)


class ReplyContextReachesStdout(unittest.TestCase):
    """End-to-end through `main()` — the render loop, not just the helper.

    The helper tests above prove `_reply_context` returns the right string.
    They would ALL still pass against a reader whose print loop never called
    it, which is precisely the shape of the bug this PR fixes: the production
    failure was a missing line in the loop, not a wrong helper. Mirrors the
    same end-to-end block in `discord-read-forwarded.test.py`, which exists
    for that identical reason.
    """

    REPLY = {
        "id": "2", "timestamp": "2026-08-04T01:24:28.000000+00:00",
        "author": {"username": "sonichi"}, "content": "2 merge",
        "referenced_message": {"author": {"username": "Sutando-Mini"},
                               "content": "**2 — holding, and here's why.**"},
    }
    PLAIN = {
        "id": "1", "timestamp": "2026-08-04T01:20:00.000000+00:00",
        "author": {"username": "sonichi"}, "content": "an ordinary message",
    }

    @contextlib.contextmanager
    def _run(self, messages):
        buf = io.StringIO()
        with mock.patch.object(dr, "_load_token", lambda env: "test-token"), \
             mock.patch.object(dr, "_fetch",
                               lambda extra, channel_id, page, headers: list(messages)), \
             contextlib.redirect_stdout(buf):
            rc = dr.main(["1507725277630042122", "--operator"])
        yield rc, buf.getvalue()

    def test_the_reply_line_reaches_stdout(self):
        """THE pin for the CLI print branch. A reader that builds the context
        and never prints it is indistinguishable from one that has no context
        at all — from the caller's side, which is the side that matters."""
        with self._run([self.REPLY, self.PLAIN]) as (rc, out):
            self.assertEqual(rc, 0, out)
            self.assertIn("replying to Sutando-Mini", out)
            self.assertIn("holding", out)

    def test_the_context_is_indented_under_its_own_message(self):
        """Shape, not just presence: the context must be the indented line
        directly beneath the message it belongs to. With two messages in the
        page, a context attached to the wrong one still contains the right
        text — so asserting only `in out` cannot catch a misplacement."""
        with self._run([self.REPLY, self.PLAIN]) as (rc, out):
            lines = [l for l in out.splitlines() if l.strip()]
        idx = next(i for i, l in enumerate(lines) if "2 merge" in l)
        self.assertGreater(len(lines), idx + 1, f"no line follows the reply: {lines!r}")
        ctx_line = lines[idx + 1]
        self.assertTrue(ctx_line.startswith("    "),
                        f"context line is not indented: {ctx_line!r}")
        self.assertIn("replying to", ctx_line)

    def test_an_ordinary_message_gets_no_extra_line(self):
        """The FALSE side of the same branch. Without this, the branch is only
        half-exercised and the common case could grow a stray blank line that
        no test would notice."""
        with self._run([self.PLAIN]) as (rc, out):
            lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"expected exactly one line, got {lines!r}")
        self.assertNotIn("replying to", out)


if __name__ == "__main__":
    unittest.main()
