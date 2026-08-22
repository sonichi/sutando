#!/usr/bin/env python3
"""Regression for the marker-parser adoption gap in src/dm-result.py.

Before this tidy, `dm-result.py` carried a private copy of the attachment
regex::

    _FILE_MARKER_RE = re.compile(r'\\[(?:file|send|attach):\\s*((?:/|~/)[^\\]:]+)\\]')

That shape only recognised values beginning `/` or `~/`, and excluded any
value containing a colon. The canonical parser in `src/result_markers.py`
recognises the documented marker shape and leaves *path authorization* to
`src/policy/egress/attachment.py`.

The divergence was user-visible: a marker the canonical parser strips was
left untouched by the private regex, so the REST fallback delivered the
literal text ``[file: some/relative/path.txt]`` into the owner's DM while
every other consumer stripped it and rejected the path at the allowlist.

These cases pin the post-adoption contract:

    parse -> strip marker from body -> authorize value -> maybe upload

A value that fails authorization must still have been stripped. "Rejected"
must never mean "leaked".

Dependency-light by design: imports only the two pure modules, never the
Discord bridge, and touches no real config, token, or workspace path.

Run: python3 tests/dm-result-adoption-gap.test.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from policy.egress.attachment import is_path_sendable  # noqa: E402

# Hermetic load: dm-result reads DISCORD_BOT_TOKEN from the environment, so
# no token file is created and $HOME is never touched. (The older
# discord-bridge-file-markers test wrote ~/.claude/channels/discord/.env —
# deliberately not repeated here.)
# --- Hermetic config root -------------------------------------------------
# dm-result resolves its token by READING FILES (`_load_token`), not from the
# environment, and resolves the owner from access.json. Left alone, this test
# silently borrows the developer's real ~/.claude/channels/discord/* — it then
# passes locally and fails in CI, where nothing resolves, send_dm() returns
# early, and every assertion sees "".
#
# (That host file exists here because the now-deleted
# tests/discord-bridge-file-markers.test.py created it — precisely the shape
# this tidy removes.)
#
# So: point CLAUDE_CONFIG_DIR at a throwaway root and seed a fake token there.
# Temp dirs + isolated config roots only; never ~/.claude, never a real token.
_CFG = Path(tempfile.mkdtemp(prefix="sutando-adoption-cfg-"))
(_CFG / "channels" / "discord").mkdir(parents=True, exist_ok=True)
(_CFG / "channels" / "discord" / ".env").write_text(
    "DISCORD_BOT_TOKEN=test-token-not-real\n"
)
os.environ["CLAUDE_CONFIG_DIR"] = str(_CFG)
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"
os.environ["SUTANDO_DM_OWNER_ID"] = "test-owner-id-not-real"
_spec = importlib.util.spec_from_file_location("dm_result_gap", REPO / "src" / "dm-result.py")
dm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dm)
import channels.discord.client as _rest  # noqa: E402  — the seam _send installs into


class _Resp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Transport:
    """Captures every request so we can inspect what would reach Discord."""

    def __init__(self):
        self.calls = []

    def urlopen(self, request, timeout=None):
        method = getattr(request, "method", None) or ("POST" if request.data is not None else "GET")
        url = request.full_url
        raw = request.data
        body = None
        if raw is not None:
            try:
                body = json.loads(raw.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"_raw": raw[:4000]}   # multipart upload
        self.calls.append({"method": method, "url": url, "body": body})
        if url.endswith("/users/@me/channels"):
            return _Resp({"id": "dm-gap-1"})
        return _Resp({"id": "msg-gap-1"})

    def sent_text(self):
        """Concatenate every textual content field actually transmitted."""
        out = []
        for c in self.calls:
            b = c.get("body")
            if isinstance(b, dict):
                if isinstance(b.get("content"), str):
                    out.append(b["content"])
                elif isinstance(b.get("_raw"), (bytes, bytearray)):
                    out.append(b["_raw"].decode("utf-8", "replace"))
        return "\n".join(out)


def _send(text):
    """Drive the REAL send_dm() and return everything it transmitted."""
    t = _Transport()

    def _tuple(req, timeout):
        resp = t.urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8", "replace")
        return getattr(resp, "status", 200), (json.loads(raw) if raw else None)

    def _read_json(req, timeout=None):
        resp = t.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8", "replace"))

    original = (dm._client, _rest.request_json)
    dm._client = lambda token: _rest.DiscordRestClient(
        token, transport=_tuple, timeout=30)
    _rest.request_json = _read_json
    try:
        dm.send_dm(text)
    finally:
        dm._client, _rest.request_json = original
    return t


def _attachments(text):
    """What send_dm() derives, observed through what it actually sends."""
    t = _send(text)
    return t.sent_text().strip(), t


# Values the LEGACY private regex could not match, but the canonical
# parser does. Each previously leaked into the DM as literal text.
LEGACY_BLIND_SPOTS = [
    "some/relative/path.txt",       # no leading / or ~/
    "./nearby.png",                 # explicit relative
    "/tmp/has:colon.txt",           # legacy char class excluded ':'
    "C:/windows/style.txt",         # colon again
]


class MarkerNeverLeaks(unittest.TestCase):
    """Drives the real send_dm() and inspects what it actually transmitted."""

    def test_legacy_blind_spots_never_reach_the_dm_as_literals(self):
        """THE adoption regression. Fails at the parent commit.

        The private regex only matched `/...` or `~/...` and excluded ':',
        so for each of these it stripped nothing and the owner's DM received
        the literal marker text."""
        for value in LEGACY_BLIND_SPOTS:
            with self.subTest(value=value):
                sent, _ = _attachments(f"Here you go [file: {value}]")
                self.assertNotIn("[file:", sent, f"literal marker leaked for {value!r}")
                self.assertNotIn(value, sent, f"marker value leaked for {value!r}")
                self.assertIn("Here you go", sent)

    def test_blind_spot_values_are_rejected_by_the_allowlist(self):
        """Stripped is not the same as sent — authorization still says no."""
        for value in LEGACY_BLIND_SPOTS:
            with self.subTest(value=value):
                self.assertFalse(
                    is_path_sendable(os.path.expanduser(value.strip())),
                    f"{value!r} must not be sendable — it is outside every root",
                )

    def test_rejected_attachment_uploads_nothing(self):
        """Body delivered clean; no multipart upload for an unauthorized path."""
        sent, t = _attachments("Report attached [attach: nope/outside.txt]")
        self.assertIn("Report attached", sent)
        self.assertNotIn("[attach:", sent)
        self.assertNotIn("nope/outside.txt", sent)

    def test_an_allowlisted_absolute_path_still_works(self):
        """No-regression: the normal flow this tidy must preserve."""
        allowed = f"/tmp/sutando-adoption-{os.getpid()}.txt"
        Path(allowed).write_text("x")
        try:
            self.assertTrue(is_path_sendable(allowed))
            sent, t = _attachments(f"See file [file: {allowed}]")
            self.assertIn("See file", sent)
            self.assertNotIn("[file:", sent)
            # The file was uploaded, not silently dropped. Inspect only calls
            # that actually carried a payload: the DM-channel-open request is
            # captured with body=None, and `.get("body", {})` does NOT guard
            # that (the default applies to a MISSING key, not a None value).
            uploads = [
                c for c in t.calls
                if isinstance(c.get("body"), dict) and c["body"].get("_raw") is not None
            ]
            self.assertTrue(uploads, "allowlisted file produced no upload request")
        finally:
            os.unlink(allowed)

    def test_skip_marker_delivers_no_message_and_no_attachment(self):
        """Skip precedence is terminal for *delivery*.

        Note on scope: the skip short-circuit lives in main(), which returns
        before any delivery path (and tests/dm-result-skip-markers.test.py
        covers that). Calling send_dm() directly bypasses main(), so a DM
        *channel-open* request can still occur. What must never happen is a
        message POST or an attachment upload — parse_markers returns an empty
        body and no attach actions past a skip, so send_dm no-ops."""
        t = _send("[no-send]\nbody [file: /tmp/sutando-x.png]")
        messages = [c for c in t.calls if "/messages" in c["url"]]
        self.assertEqual(messages, [], "a skip-marked body was delivered")
        self.assertNotIn("[no-send]", t.sent_text())
        self.assertNotIn("/tmp/sutando-x.png", t.sent_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
