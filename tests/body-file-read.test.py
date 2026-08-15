#!/usr/bin/env python3
"""The shared --body-file reader contract, plus each adapter's delegation to it.
Run: python3 tests/body-file-read.test.py"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent

# ISOLATE BEFORE ANY BRIDGE IMPORT. discord-bridge resolves channel config at
# module scope, so isolation applied later would run after the read it prevents.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-body-file-")
os.environ.pop("CLAUDE_HOME", None)
os.environ["SUTANDO_TEST_MODE"] = "1"
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-token\n", encoding="utf-8")

sys.path.insert(0, str(REPO / "src"))
import body_file  # noqa: E402


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReaderContract(unittest.TestCase):
    """Every branch here is fail-closed: a miss means a sender hangs or over-reads."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="body-file-")

    def _p(self, name):
        return os.path.join(self.td, name)

    def test_regular_file_returns_content_with_trailing_newline_stripped(self):
        p = self._p("b.txt")
        pathlib.Path(p).write_text("hello `world`'s body\n", encoding="utf-8")
        self.assertEqual(body_file.read_body_file(p), "hello `world`'s body")

    def test_interior_newlines_survive(self):
        p = self._p("multi.txt")
        pathlib.Path(p).write_text("line one\n\nline three\n", encoding="utf-8")
        self.assertEqual(body_file.read_body_file(p), "line one\n\nline three")

    def test_missing_path_exits(self):
        with self.assertRaises(SystemExit) as cm:
            body_file.read_body_file(self._p("nope.txt"))
        self.assertIn("cannot read --body-file", str(cm.exception))

    def test_fifo_is_refused_without_ever_opening_it(self):
        p = self._p("pipe")
        os.mkfifo(p)
        with self.assertRaises(SystemExit) as cm:
            body_file.read_body_file(p)
        self.assertIn("not a regular file", str(cm.exception))

    def test_directory_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            body_file.read_body_file(self.td)
        self.assertIn("not a regular file", str(cm.exception))

    def test_oversize_is_refused_from_stat_before_reading(self):
        p = self._p("big.txt")
        pathlib.Path(p).write_bytes(b"x" * (body_file.MAX_BODY_BYTES + 1))
        with self.assertRaises(SystemExit) as cm:
            body_file.read_body_file(p)
        self.assertIn("over the", str(cm.exception))

    def test_growth_after_stat_is_still_refused(self):
        # The stat says small, the bytes say large. Without the post-read
        # re-check a file that grows between the two calls slips the bound.
        p = self._p("grows.txt")
        pathlib.Path(p).write_bytes(b"x" * (body_file.MAX_BODY_BYTES + 1))
        real = os.stat(p)
        lying = os.stat_result((real.st_mode, 0, 0, 1, 0, 0, 10) + tuple(real)[7:])
        with mock.patch.object(body_file.os, "stat", return_value=lying):
            with self.assertRaises(SystemExit) as cm:
                body_file.read_body_file(p)
        self.assertIn("exceeds", str(cm.exception))

    def test_exactly_at_the_limit_is_accepted(self):
        p = self._p("edge.txt")
        pathlib.Path(p).write_bytes(b"x" * body_file.MAX_BODY_BYTES)
        self.assertEqual(len(body_file.read_body_file(p)), body_file.MAX_BODY_BYTES)

    def test_non_utf8_is_refused(self):
        p = self._p("latin.txt")
        pathlib.Path(p).write_bytes(b"caf\xe9 not utf-8")
        with self.assertRaises(SystemExit) as cm:
            body_file.read_body_file(p)
        self.assertIn("not UTF-8", str(cm.exception))


class AdaptersDelegate(unittest.TestCase):
    """Wiring: neither entrypoint may carry its own copy of the bounds."""

    def test_bot2bot_post_delegates_to_the_shared_owner(self):
        post = _load("bot2bot_post_wiring", "skills/bot2bot-post/post.py")
        self.assertIs(post._read_body_file, body_file.read_body_file)
        self.assertIs(post.MAX_BODY_BYTES, body_file.MAX_BODY_BYTES)

    def test_discord_bridge_delegates_to_the_shared_owner(self):
        # CLAUDE_CONFIG_DIR was isolated at module scope, above every import.
        db = _load("db_wiring", "src/discord-bridge.py")
        self.assertIs(db._read_body_file, body_file.read_body_file)
        self.assertIs(db.MAX_BODY_BYTES, body_file.MAX_BODY_BYTES)


class PostArgumentErrors(unittest.TestCase):
    """post.py's --body-file argument shape. Each case exits before any network call."""

    @classmethod
    def setUpClass(cls):
        cls.post = _load("bot2bot_post_args", "skills/bot2bot-post/post.py")

    def _main(self, argv):
        with mock.patch.object(sys, "argv", ["post.py"] + argv):
            with self.assertRaises(SystemExit) as cm:
                self.post.main()
        return cm.exception

    def test_no_arguments_prints_usage_and_exits_nonzero(self):
        self.assertEqual(self._main([]).code, 1)

    def test_kind_without_body_prints_usage(self):
        self.assertEqual(self._main(["claim"]).code, 1)

    def test_body_file_without_a_path_is_refused(self):
        self.assertIn("requires a path", str(self._main(["claim", "--body-file"])))

    def test_trailing_argument_after_the_path_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("body")
        exc = self._main(["claim", "--body-file", fh.name, "extra"])
        self.assertIn("drop", str(exc))

    def test_empty_body_file_is_refused_rather_than_posted_blank(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("   \n")
        self.assertIn("empty", str(self._main(["claim", "--body-file", fh.name])))

    def test_unknown_kind_is_still_rejected(self):
        self.assertIn("kind must be one of", str(self._main(["notakind", "text"])))

    def test_the_flag_later_in_a_body_stays_prose(self):
        # Compatibility guarantee: it must NOT be read as a path here. Reaching
        # the kind check means the body was joined as text, not opened.
        self.assertIn("kind must be one of",
                      str(self._main(["please", "document", "--body-file", "usage"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
