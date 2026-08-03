#!/usr/bin/env python3
"""Executing regression for telegram-bridge send_reply() marker adoption.

WHY THIS EXISTS, given the guard tests already pass
---------------------------------------------------
`tests/bridge-marker-no-leak.test.py` proves telegram-bridge *contains* the
right strings — it reads the source and checks that `parse_markers` is imported
and called and that no local `file|send|attach` regex is declared. That is a
source scan. It cannot tell whether `send_reply()` actually routes a real
payload through the parser, and a regression that kept the import while
reverting the body would sail past it (CR #2551, @qingyun-wu: hosted diff
coverage was red on exactly these lines, 418-420, because nothing executed
them).

So this drives the real function with `api` / `send_file` stubbed and asserts on
what the owner would receive.

The bug being guarded: send_reply() used to compile its own `file|send|attach`
regex, which stripped attachment markers but left every OTHER marker in the
body. `poll_proactive()` passes RAW result text here, so `[dm-only]` and
`[channel:]` reached the owner verbatim — the morning briefing is emitted as a
proactive result carrying `[dm-only]`, and it rendered with the marker visible.

Run: python3 tests/telegram-bridge-send-reply-markers.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load_bridge():
    """Import telegram-bridge.py by path (hyphenated name is not importable).

    The module reads config at import time; point it at a throwaway config dir
    so this test never touches the host's real Telegram state.
    """
    # In-process env only — never write a token into the caller's config dir
    # (#2357: a test seeding a stub Discord token into the real config dir).
    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="tg-sendreply-cfg-")
    os.environ["SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER"] = "1"
    # The module exits at import without a token; it is never used because
    # `api` is stubbed before any call, but import must get past the guard.
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-credential")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_under_test", REPO / "src" / "telegram-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TB = _load_bridge()


def _allowlisted_file() -> str:
    """A file send_allowlist actually permits.

    `is_path_sendable()` only allows SEND_ALLOWED_ROOTS and the
    `/tmp/sutando-` / `/private/tmp/sutando-` prefixes. A plain
    NamedTemporaryFile lands in /var/folders/... and is BLOCKED, so an
    attachment assertion written against one passes for the wrong reason —
    nothing was sent because policy refused, not because the code is right.
    """
    d = tempfile.mkdtemp(prefix="sutando-tg-sendreply-", dir="/tmp")
    f = Path(d) / "shot.png"
    f.write_bytes(b"x")
    return str(f)


class SendReplyMarkerAdoption(unittest.TestCase):
    """Every case drives the real send_reply(); none inspect source text."""

    def setUp(self):
        self.sent: list[dict] = []
        self.files: list[str] = []

        def fake_api(method, **kw):
            self.sent.append({"method": method, **kw})
            return {"ok": True}

        def fake_send_file(chat_id, path):
            self.files.append(path)
            return {"ok": True}

        self._api, self._send_file = TB.api, TB.send_file
        TB.api, TB.send_file = fake_api, fake_send_file

    def tearDown(self):
        TB.api, TB.send_file = self._api, self._send_file

    def _bodies(self) -> str:
        return "\n".join(c.get("text", "") for c in self.sent if c["method"] == "sendMessage")

    # ---- the leak this fix exists to stop ---------------------------------

    def test_dm_only_marker_never_reaches_the_owner(self):
        """The morning-briefing shape: proactive text carrying [dm-only]."""
        TB.send_reply(123, "[dm-only]\nGood morning — 3 meetings today.")
        body = self._bodies()
        self.assertNotIn("[dm-only]", body, "marker leaked into the owner's message")
        self.assertIn("Good morning", body, "prose was lost while stripping")

    def test_channel_marker_never_reaches_the_owner(self):
        TB.send_reply(123, "[channel: 1234567890123456789]\nRouted elsewhere.")
        body = self._bodies()
        self.assertNotIn("[channel:", body, "marker leaked into the owner's message")
        self.assertIn("Routed elsewhere.", body)

    def test_each_leading_marker_is_stripped_on_its_own(self):
        """The shapes that actually reach this function, one marker each.

        Deliberately NOT a combined `[dm-only]\n[no-send]` payload: markers have
        different scoping rules (`[dm-only]` is detected anywhere, `[no-send]`
        only at body start), so a combined case asserts parse_markers ordering
        semantics rather than this function's adoption of it. That belongs to
        result_markers, not here.
        """
        for marker, rest in (("[dm-only]", "briefing"),
                             ("[channel: 1234567890123456789]", "routed"),
                             ("[REPLIED]", "already sent")):
            with self.subTest(marker=marker):
                self.sent.clear()
                TB.send_reply(123, f"{marker}\n{rest}")
                body = self._bodies()
                self.assertNotIn(marker.split(":")[0], body, f"{marker} leaked to the owner")

    # ---- attachments still work, derived from parse_markers actions -------

    def test_attachment_marker_is_extracted_and_stripped(self):
        path = _allowlisted_file()
        try:
            TB.send_reply(123, f"see this [file: {path}] thanks")
            self.assertNotIn("[file:", self._bodies(), "attachment marker leaked into text")
            self.assertIn("see this", self._bodies())
            self.assertIn(path, self.files, "attachment was not sent via send_file")
        finally:
            os.unlink(path)

    def test_return_shape_reports_what_was_delivered(self):
        r = TB.send_reply(123, "[dm-only]\nplain body")
        self.assertEqual(r["text_chunks"], 1)
        self.assertEqual(r["files_sent"], 0)
        self.assertTrue(r["ok"])

    # ---- idempotence: the task path passes an ALREADY-parsed body ---------

    def test_double_parsing_cannot_double_send(self):
        """poll_results passes parsed.body and sends its own attachments.

        parse_markers must be idempotent here or the attachment goes twice.
        """
        path = _allowlisted_file()
        try:
            from result_markers import parse_markers
            already = parse_markers(f"body [file: {path}]").body
            TB.send_reply(123, already)
            self.assertEqual(self.files, [], "re-sent an attachment the caller already handled")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
