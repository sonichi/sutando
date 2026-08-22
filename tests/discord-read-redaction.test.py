#!/usr/bin/env python3
"""The bridge redacts a secret out of the task file; this reader handed it back.

`discord-bridge.py` applies BOTH `filter_chat_secrets` and `redact_vault_commands`
to message text before it reaches a task file. `discord-read.py` — the reader the
`context-reconstruct` step runs on every proactive-loop pass, and the one the
Discord skill's CONTEXT-FIRST rule mandates — imported neither.

So the redaction was not incomplete, it was bypassed through the other door.
Measured 2026-08-07: a `vault set TELEGRAM_BOT_TOKEN <token>` was correctly
stripped from the task file, and then the very next step read the channel and
pulled the token back verbatim.

The load-bearing cases are `test_vault_set_is_not_echoed_back` and
`test_secret_in_a_reply_target_is_redacted`: both FAIL on the parent commit,
where the reader prints message content untouched. The negative cases (ordinary
text unchanged) would pass against any implementation, including one that
redacts nothing, so on their own they prove nothing.

No real credential appears in this file — the fixtures are syntactically valid
but fabricated.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "discord-read.py"

_spec = importlib.util.spec_from_file_location("discord_read", SCRIPT)
dr = importlib.util.module_from_spec(_spec)
sys.modules["discord_read"] = dr
try:
    _spec.loader.exec_module(dr)
except SystemExit:
    pass

# Fabricated, never issued. Shaped like a Telegram bot token because that is the
# one that actually leaked.
FAKE_TOKEN = "8412345678:AAF-xyzXYZ_abcDEF123456789ghijkl"


def msg(content, author="sonichi"):
    return {"content": content, "author": {"username": author}}


class ReaderRedaction(unittest.TestCase):
    def test_vault_set_is_not_echoed_back(self):
        """THE pin. Fails on parent, which returns the token verbatim."""
        out = dr._render(msg(f"vault set TELEGRAM_BOT_TOKEN {FAKE_TOKEN}"), clip=None)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertIn("vault set TELEGRAM_BOT_TOKEN", out,
                      "the command should still be legible — only the value goes")

    def test_secret_in_a_reply_target_is_redacted(self):
        """`_reply_context` renders the message being answered. It calls `_render`,
        so it must inherit the redaction rather than need its own copy — a second
        copy is how two consumers start disagreeing about what a message says."""
        ctx = dr._reply_context(
            {"content": "ok", "author": {"username": "sonichi"},
             "referenced_message": {"content": f"vault set X {FAKE_TOKEN}",
                                    "author": {"username": "sonichi"}}},
            clip=None)
        self.assertIsNotNone(ctx)
        self.assertNotIn(FAKE_TOKEN, ctx)

    def test_secret_inside_a_forward_is_redacted(self):
        """Forwards are exempt from the length clip and carry the substance
        someone moved deliberately — so they are the LAST place a leak may
        survive, not a case to skip."""
        out = dr._render({
            "content": "",
            "author": {"username": "sonichi"},
            "message_snapshots": [{"message": {"content": f"vault set Y {FAKE_TOKEN}"}}],
        }, clip=None)
        self.assertIn("[forwarded]", out)
        self.assertNotIn(FAKE_TOKEN, out)

    def test_secret_in_a_forwarded_attachment_filename_is_redacted(self):
        """The body was redacted while the extras beside it were not — one
        field to the side of the case above. A filename is user-supplied and
        the bridge filters it on the same path, so it must not print raw.

        Uses the vault-set grammar deliberately: it is caught by
        `redact_vault_commands` alone, so this pins the WIRING (extras reach
        `_redact`) without depending on the optional `secret_scanner` library
        that `filter_chat_secrets` degrades silently without.
        """
        out = dr._render({
            "content": "",
            "author": {"username": "sonichi"},
            "message_snapshots": [{"message": {
                "content": "",
                "attachments": [{"filename": f"vault set X {FAKE_TOKEN}.txt"}],
            }}],
        }, clip=None)
        self.assertIn("[forwarded]", out)
        self.assertNotIn(FAKE_TOKEN, out)

    def test_secret_in_a_forwarded_embed_title_is_redacted(self):
        """Same gap, the other extra: an embed title is remote-controlled by
        whoever authored the embedded link."""
        out = dr._render({
            "content": "",
            "author": {"username": "sonichi"},
            "message_snapshots": [{"message": {
                "content": "",
                "embeds": [{"title": f"vault set Z {FAKE_TOKEN}"}],
            }}],
        }, clip=None)
        self.assertIn("[forwarded]", out)
        self.assertNotIn(FAKE_TOKEN, out)

    def test_forward_with_no_readable_body_still_says_so(self):
        """Guard the composed-inner refactor: redacting the JOINED string must
        not turn an empty forward into an empty render."""
        out = dr._render({
            "content": "",
            "author": {"username": "sonichi"},
            "message_snapshots": [{"message": {"content": ""}}],
        }, clip=None)
        self.assertIn("(forward with no readable body)", out)

    def test_redaction_happens_before_the_clip(self):
        """The clip can land MID-token. Redacting after it would leave the leading
        characters of a secret printed and no longer matchable — a leak that looks
        redacted.

        The padding is computed so the token genuinely straddles the boundary: an
        earlier version of this test padded past the clip entirely, so the token
        never appeared in the clipped window and the case passed on the parent
        commit AND against a clip-then-redact implementation. It asserted nothing.
        """
        # The separating space is load-bearing: the matcher needs a word boundary,
        # so padding glued to "vault" would fail the fixture for the wrong reason.
        prefix = " vault set Z "
        pad = "x" * (dr.CLIP - len(prefix) - 8)      # 8 token chars land inside
        out = dr._render(msg(pad + prefix + FAKE_TOKEN), clip=dr.CLIP)
        # Guard the guard: without redaction those 8 characters are printable, so
        # the assertion below has something to catch.
        self.assertGreater(len(pad + prefix), dr.CLIP - len(FAKE_TOKEN),
                           "fixture must straddle the clip or this test is vacuous")
        self.assertNotIn(FAKE_TOKEN[:8], out,
                         "a clipped-in-half secret is still a secret")

    def test_ordinary_text_is_untouched(self):
        """Additive: a message with no secret must render exactly as before."""
        self.assertEqual(dr._render(msg("2 merge"), clip=None), "2 merge")
        self.assertEqual(dr._render(msg("see PR #2891 for the probe fix"), clip=None),
                         "see PR #2891 for the probe fix")

    def test_empty_and_missing_content_do_not_raise(self):
        """`_redact` is called on every message, including the empty ones a
        forward produces — it must pass them through rather than fail the read."""
        self.assertEqual(dr._render(msg(""), clip=None), "")
        self.assertEqual(dr._render({"author": {"username": "x"}}, clip=None), "")

    def test_filters_match_the_bridge(self):
        """Both consumers must apply the SAME two helpers from the SAME modules.
        `redact_vault_commands` exists in two modules with different defaults;
        importing the wrong one silently diverges the reader from the bridge.
        Post-extraction the imports live ONCE in discord_reader; the CLI must
        resolve the IDENTICAL function object (stronger than a source-string
        match, which stays green when the call is disabled — REVIEW.md L13)."""
        shared = sys.modules["channels.discord.reader"]
        self.assertIs(dr._redact, shared._redact)
        src = (REPO / "src" / "channels" / "discord" / "reader.py").read_text()
        self.assertIn("from chat_secret_filter import filter_chat_secrets", src)
        self.assertIn("from vault_intercept import redact_vault_commands", src)


class SiblingReaderRedaction(unittest.TestCase):
    """`src/read_discord_channel.py` had the SAME bypass — and it is the reader
    CLAUDE.md tells the agent to PREFER (the graceful contextNotFrom path).

    Fixing only `discord-read.py` would have left the documented-preferred path
    leaking, which is the instance-vs-class mistake: the policy already lives in
    `chat_secret_filter` / `vault_intercept`, and the defect is a consumer not
    wired to it. Two consumers were unwired; both are now wired.

    Note what this does NOT overlap with: the contextNotFrom gate decides WHICH
    channel may be read. It says nothing about what the text contains, so a
    permitted channel can still carry a credential. Gate and redaction are
    orthogonal, and having one is not having the other.
    """

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "read_discord_channel", REPO / "src" / "read_discord_channel.py")
        self.rdc = importlib.util.module_from_spec(spec)
        sys.modules["read_discord_channel"] = self.rdc
        try:
            spec.loader.exec_module(self.rdc)
        except SystemExit:
            pass

    def _fetch(self, contents):
        msgs = [{"author": {"username": "sonichi"}, "content": c} for c in contents]
        self.rdc._api_get = lambda *a, **k: msgs
        return self.rdc.fetch_messages("123", len(msgs), "token")

    def test_vault_set_is_not_printed(self):
        """Fails on parent, which prints `m.get("content", "")` untouched."""
        out = self._fetch([f"vault set TELEGRAM_BOT_TOKEN {FAKE_TOKEN}"])
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertIn("vault set TELEGRAM_BOT_TOKEN", out)

    def test_ordinary_messages_and_authors_survive(self):
        """Additive — the reader's shape (`[author] content`, oldest-first) is
        unchanged for text with no secret in it."""
        out = self._fetch(["ordinary message"])
        self.assertEqual(out, "[sonichi] ordinary message")

    def test_empty_content_does_not_raise(self):
        self.assertEqual(self._fetch([""]), "[sonichi] ")

    def test_uses_the_same_two_helpers_as_the_bridge(self):
        """Both readers and the bridge must resolve the SAME policy, from the
        same modules. Post-extraction: identity against the shared module —
        the import-source pin lives on discord_reader.py (one canonical file)."""
        self.assertIs(self.rdc._redact, sys.modules["channels.discord.reader"]._redact)


if __name__ == "__main__":
    unittest.main(verbosity=2)
