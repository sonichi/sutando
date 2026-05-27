"""Tests for outbox_log integration in skills/x-twitter/x-post.py.

Covers Phase B-1 of issue #932: skill-side senders write to outbox log.
Run: python3 tests/x-twitter-outbox.test.py
"""
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# Add skills/x-twitter to path so we can import x-post as a module.
sys.path.insert(0, str(ROOT / "skills" / "x-twitter"))


class TestPostTweetOutboxLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        self.tmp.cleanup()

    def _outbox_entries(self) -> list[dict]:
        path = self.workspace / "state" / "outbox.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _make_fake_response(self, tweet_id="123456789", status_code=201):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = {"data": {"id": tweet_id, "text": "hello"}}
        return resp

    def _import_x_post_fresh(self):
        """Import (or re-import) x-post with a clean _outbox_log cache.

        The file uses a hyphenated name so importlib.util is required.
        """
        cache_key = "x_post_module"
        if cache_key in sys.modules:
            mod = sys.modules[cache_key]
            mod._outbox_log = None
            return mod
        spec = importlib.util.spec_from_file_location(
            cache_key, ROOT / "skills" / "x-twitter" / "x-post.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[cache_key] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_successful_tweet_writes_outbox_entry(self):
        xp = self._import_x_post_fresh()
        fake_resp = self._make_fake_response()

        with patch.object(xp, "requests", create=True) as mock_req:
            mock_req.post.return_value = fake_resp
            tweet_id = xp.post_tweet("Hello world", auth=MagicMock())

        self.assertEqual(tweet_id, "123456789")
        entries = self._outbox_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["channel_type"], "x_twitter")
        self.assertEqual(e["recipient"], "broadcast")
        self.assertEqual(e["body_preview"], "Hello world")
        self.assertEqual(e["body_len"], len("Hello world"))
        self.assertIn("ts", e)

    def test_failed_tweet_does_not_write_outbox(self):
        xp = self._import_x_post_fresh()
        fake_resp = self._make_fake_response(status_code=403)
        fake_resp.text = "Forbidden"

        with patch.object(xp, "requests", create=True) as mock_req:
            mock_req.post.return_value = fake_resp
            with self.assertRaises(SystemExit):
                xp.post_tweet("Should not log", auth=MagicMock())

        self.assertEqual(self._outbox_entries(), [])

    def test_long_tweet_body_preview_is_truncated(self):
        xp = self._import_x_post_fresh()
        fake_resp = self._make_fake_response()
        long_text = "A" * 300

        with patch.object(xp, "requests", create=True) as mock_req:
            mock_req.post.return_value = fake_resp
            xp.post_tweet(long_text, auth=MagicMock())

        entries = self._outbox_entries()
        self.assertEqual(len(entries), 1)
        self.assertLessEqual(len(entries[0]["body_preview"]), 201)  # 200 + ellipsis char
        self.assertEqual(entries[0]["body_len"], 300)

    def test_outbox_missing_src_does_not_raise(self):
        """If outbox_log import fails (missing src/), post_tweet still succeeds."""
        xp = self._import_x_post_fresh()
        xp._outbox_log = None
        fake_resp = self._make_fake_response()

        original_path = list(sys.path)
        # Temporarily hide src/ from sys.path to simulate missing outbox_log.
        sys.path[:] = [p for p in sys.path if "src" not in p]
        # Remove cached outbox_log module so import fails.
        sys.modules.pop("outbox_log", None)

        try:
            with patch.object(xp, "requests", create=True) as mock_req:
                mock_req.post.return_value = fake_resp
                tweet_id = xp.post_tweet("Resilient post", auth=MagicMock())
        finally:
            sys.path[:] = original_path

        self.assertEqual(tweet_id, "123456789")


if __name__ == "__main__":
    unittest.main()
