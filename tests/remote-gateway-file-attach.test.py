#!/usr/bin/env python3
"""Tests for the remote-gateway-bridge outbound file-attach path.

Covers:
  1. `_post_ready_results` routes marker decisions through the unified
     parser (parse_markers, #873): skip markers archive without POSTing.
  2. `[file:]` markers upload via POST /v1/rooms/{room}/media, the marker
     is stripped from the delivered body, and the room comes from the
     task→room sidecar map recorded at queue time.
  3. Disallowed / missing-room attachments degrade to an in-band
     `[attachment not sent: …]` note — never a silent drop, never a crash.
  4. `[channel:]` redirect is re-stitched (gateway handles redirect
     server-side for this transport).
  5. The task→room sidecar map round-trips and is pruned on delivery.

Run: python3 tests/remote-gateway-file-attach.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "remote-gateway-bridge.py"


def _load(ws: Path):
    """Load the hyphenated bridge module against a scratch workspace."""
    os.environ["SUTANDO_TEST_WORKSPACE"] = str(ws)
    os.environ.setdefault("REMOTE_TASK_TOKEN", "test-token-0123456789abcdef")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.invalid/relay")
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("rgb_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("workspace_default.resolve_workspace", return_value=ws):
        spec.loader.exec_module(mod)
    # Re-point the module's derived paths at the scratch workspace regardless
    # of what resolve_workspace did during import (belt + braces for CI envs).
    mod.WS = ws
    mod.TASKS_DIR = ws / "tasks"
    mod.RESULTS_DIR = ws / "results"
    mod.ARCHIVE_RESULTS_DIR = ws / "results" / "archive"
    mod.INFLIGHT_FILE = ws / "state" / "remote-task-inflight.json"
    mod.TASK_ROOMS_FILE = ws / "state" / "remote-task-rooms.json"
    for d in (mod.TASKS_DIR, mod.RESULTS_DIR, ws / "state"):
        d.mkdir(parents=True, exist_ok=True)
    return mod


class FileAttachTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.mod = _load(self.ws)
        self.calls: list[tuple[str, str, dict | None]] = []

        def fake_req(method, path, payload=None, timeout=35):
            self.calls.append((method, path, payload))
            return {}

        self._req_patch = patch.object(self.mod, "_req", side_effect=fake_req)
        self._req_patch.start()

    def tearDown(self):
        self._req_patch.stop()
        self._tmp.cleanup()

    def _result(self, tid: str, body: str) -> None:
        (self.mod.RESULTS_DIR / f"{tid}.txt").write_text(body)

    # 1 — unified-parser skip semantics
    def test_skip_marker_archives_without_posting(self):
        self._result("t1", "[no-send] internal only")
        self.mod._post_ready_results({"t1"})
        self.assertEqual(self.calls, [])
        self.assertTrue(list((self.mod.ARCHIVE_RESULTS_DIR).glob("t1-*.txt")))

    # 2 — happy path: allowlisted file uploads, marker stripped, room from map
    def test_attach_uploads_and_strips_marker(self):
        # `/tmp/sutando-*` is an allowlisted prefix on every machine
        # (send_allowlist.SEND_ALLOWED_PREFIXES) — use it so the happy path
        # actually runs everywhere instead of skipping.
        fd, fpath = tempfile.mkstemp(prefix="sutando-attach-test-", suffix=".txt", dir="/tmp")
        os.write(fd, b"payload")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(fpath) and os.unlink(fpath))
        self.assertTrue(self.mod.is_path_sendable(fpath),
                        f"expected allowlisted prefix for {fpath}")
        self.mod._record_task_room("t2", "!room:server")
        self._result("t2", f"here you go [file: {fpath}]")
        self.mod._post_ready_results({"t2"})
        media = [c for c in self.calls if "/media" in c[1]]
        results = [c for c in self.calls if c[1] == "/v1/results"]
        self.assertEqual(len(media), 1)
        self.assertIn("/v1/rooms/%21room%3Aserver/media", media[0][1])
        self.assertEqual(media[0][2]["filename"], os.path.basename(fpath))
        self.assertEqual(len(results), 1)
        self.assertNotIn("[file:", results[0][2]["body"])
        self.assertIn("here you go", results[0][2]["body"])
        # map pruned on delivery
        self.assertNotIn("t2", self.mod._load_task_rooms())

    # 3 — disallowed path degrades to an in-band note
    def test_disallowed_attachment_noted_in_band(self):
        self.mod._record_task_room("t3", "!room:server")
        self._result("t3", "sensitive [file: /etc/passwd]")
        self.mod._post_ready_results({"t3"})
        media = [c for c in self.calls if "/media" in c[1]]
        results = [c for c in self.calls if c[1] == "/v1/results"]
        self.assertEqual(media, [])
        self.assertEqual(len(results), 1)
        self.assertIn("[attachment not sent: /etc/passwd", results[0][2]["body"])

    def test_unknown_room_noted_in_band(self):
        self._result("t4", "report [file: /tmp/whatever.txt]")
        self.mod._post_ready_results({"t4"})
        results = [c for c in self.calls if c[1] == "/v1/results"]
        self.assertEqual(len(results), 1)
        self.assertIn("origin room unknown", results[0][2]["body"])

    # 4 — redirect is re-stitched for the gateway to handle
    def test_redirect_restitched_first_line(self):
        self._result("t5", "[channel: !dev:server]\nthe reply")
        self.mod._post_ready_results({"t5"})
        results = [c for c in self.calls if c[1] == "/v1/results"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][2]["body"].startswith("[channel: !dev:server]"))
        self.assertIn("the reply", results[0][2]["body"])

    # 5 — sidecar map round-trip
    def test_rooms_map_roundtrip(self):
        self.mod._record_task_room("a", "!x:s")
        self.mod._record_task_room("b", "!y:s")
        self.assertEqual(self.mod._load_task_rooms(), {"a": "!x:s", "b": "!y:s"})
        self.mod._forget_task_room("a")
        self.assertEqual(self.mod._load_task_rooms(), {"b": "!y:s"})
        # corrupt file fails open
        self.mod.TASK_ROOMS_FILE.write_text("{not json")
        self.assertEqual(self.mod._load_task_rooms(), {})


if __name__ == "__main__":
    unittest.main(verbosity=1)
