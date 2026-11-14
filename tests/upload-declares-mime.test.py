#!/usr/bin/env python3
"""V148: every upload declares its media type, so the server does not fall back to
`application/octet-stream` and draw an agent's video as a file card.

Covers the one policy module and all three call sites that POST media to the gateway.
"""
import base64
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, os.path.join(REPO, "skills", "agent-room-ops"))

from media_mime import DEFAULT_MIME, guess_mime, is_video  # noqa: E402


class GuessMime(unittest.TestCase):
    def test_video_extensions_are_named_not_defaulted(self):
        # The bug: these arrived as octet-stream, so the server made a file card.
        self.assertEqual(guess_mime("clip.mp4"), "video/mp4")
        self.assertEqual(guess_mime("screen.mov"), "video/quicktime")
        self.assertEqual(guess_mime("capture.webm"), "video/webm")
        for name in ("clip.mp4", "screen.mov", "capture.webm"):
            self.assertTrue(is_video(guess_mime(name)), name)

    def test_case_and_path_do_not_matter(self):
        self.assertEqual(guess_mime("CLIP.MP4"), "video/mp4")
        self.assertEqual(guess_mime("/a/b/Screen Recording.MOV"), "video/quicktime")

    def test_non_video_is_unchanged(self):
        self.assertEqual(guess_mime("shot.png"), "image/png")
        self.assertFalse(is_video(guess_mime("shot.png")))

    def test_unknown_falls_back_to_the_servers_own_default(self):
        # No second-guessing: when nothing is known we say what the server would assume.
        self.assertEqual(guess_mime("noextension"), DEFAULT_MIME)
        self.assertEqual(guess_mime(""), DEFAULT_MIME)


class RoomOpsSendMedia(unittest.TestCase):
    """skills/agent-room-ops/media.py — the agent's own `room_ops send`."""

    def _send(self, filename):
        import media as room_media

        outbox = tempfile.mkdtemp()
        path = os.path.join(outbox, filename)
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02")
        os.environ["ROOM_MEDIA_ALLOW"] = outbox

        sent = {}

        def fake_request(method, url, headers, data=None, **kw):
            sent["body"] = json.loads(data.decode())
            return 200, b"{}", {}

        orig_gateway = room_media.gateway
        orig_http = room_media.http_request
        orig_gate = room_media.gate_allows
        room_media.gateway = lambda room_id: ("https://gw.invalid", {})
        room_media.http_request = fake_request
        room_media.gate_allows = lambda *a, **k: True
        try:
            out = room_media.send_media("!r:s.space", path, "@a:s.space", gate={})
        finally:
            room_media.gateway = orig_gateway
            room_media.http_request = orig_http
            room_media.gate_allows = orig_gate
        self.assertTrue(out["ok"], out)
        return sent["body"]

    def test_an_mp4_declares_video_mp4(self):
        body = self._send("clip.mp4")
        self.assertEqual(body.get("mime"), "video/mp4")

    def test_the_bytes_and_name_still_ride_along(self):
        body = self._send("clip.mp4")
        self.assertEqual(body["filename"], "clip.mp4")
        self.assertEqual(base64.b64decode(body["content_b64"]), b"\x00\x01\x02")

    def test_a_png_is_not_mislabelled(self):
        self.assertEqual(self._send("shot.png").get("mime"), "image/png")


class SparrowBridgeUploads(unittest.TestCase):
    """The two gateway POSTs in remote_gateway_bridge.py declare the type too.

    Read as source rather than imported: the bridge pulls in the whole sparrow runtime,
    which a unit test should not start.
    """

    def setUp(self):
        src = os.path.join(REPO, "packages", "ag2-sparrow", "ag2_sparrow",
                           "remote_gateway_bridge.py")
        with open(src, encoding="utf-8") as f:
            self.text = f.read()

    def test_room_media_post_sends_mime(self):
        i = self.text.index('f"/v1/rooms/{safe_room}/media"')
        self.assertIn("guess_mime(name)", self.text[i:i + 400])

    def test_task_media_post_sends_mime(self):
        i = self.text.index('f"/v1/tasks/{safe_tid}/media"')
        self.assertIn("guess_mime(name)", self.text[i:i + 400])

    def test_it_uses_the_shared_policy_not_its_own(self):
        self.assertIn("from .media_mime import guess_mime", self.text)
        self.assertNotIn("mimetypes.guess_type", self.text,
                         "the bridge must not grow a second copy of the policy")


class VendoredCopyMatches(unittest.TestCase):
    """The ag2-sparrow copy is byte-identical, as sync_from_src.py promises."""

    def test_no_drift(self):
        a = os.path.join(REPO, "src", "media_mime.py")
        b = os.path.join(REPO, "packages", "ag2-sparrow", "ag2_sparrow", "media_mime.py")
        with open(a, "rb") as f1, open(b, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_it_is_registered_for_syncing(self):
        tool = os.path.join(REPO, "packages", "ag2-sparrow", "tools", "sync_from_src.py")
        with open(tool, encoding="utf-8") as f:
            self.assertIn('"src/media_mime.py": "media_mime.py"', f.read())


if __name__ == "__main__":
    unittest.main()
