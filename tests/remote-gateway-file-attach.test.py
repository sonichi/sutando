#!/usr/bin/env python3
"""Tests for the remote-gateway-bridge outbound file-attach path.

Covers:
  1. `_post_ready_results` routes marker decisions through the unified
     parser: skip markers POST raw to close the lease, then archive.
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
        # Provenance is explicit: the drain guards non-owner output, so a
        # fixture with no task file is a guarded one, not an owner one.
        (self.mod.TASKS_DIR / f"{tid}.txt").write_text(
            f"id: {tid}\naccess_tier: owner\ntask: fixture\n")
        (self.mod.RESULTS_DIR / f"{tid}.txt").write_text(body)

    # Skip markers still POST to close the lease; the server suppresses their
    # user-facing delivery.
    def test_skip_marker_posts_raw_body_then_archives(self):
        self._result("t1", "[no-send] internal only")
        self.mod._post_ready_results({"t1"})
        posts = [c for c in self.calls if c[0] == "POST" and c[1] == "/v1/results"]
        self.assertEqual(len(posts), 1)
        self.assertIn("[no-send]", (posts[0][2] or {}).get("body", ""),
                      "raw marker preserved so the server suppresses delivery")
        self.assertTrue(list((self.mod.ARCHIVE_RESULTS_DIR).glob("t1-*.txt")))

    # 1b — a skip-marker POST that fails must NOT archive: the open lease is
    # only recoverable while the result file survives for the next drain.
    def test_skip_marker_post_failure_keeps_the_result_for_retry(self):
        import urllib.error
        failures = [
            urllib.error.HTTPError("http://gw.invalid", 502, "bad gateway", None, None),
            urllib.error.URLError("connection refused"),
            TimeoutError("read timed out"),
        ]
        for i, exc in enumerate(failures):
            with self.subTest(failure=type(exc).__name__):
                tid = f"tfail{i}"
                self._result(tid, "[no-send] internal only")
                inflight = {tid}
                with patch.object(self.mod, "_req", side_effect=exc):
                    self.mod._post_ready_results(inflight)
                self.assertTrue((self.mod.RESULTS_DIR / f"{tid}.txt").exists(),
                                "failed POST must keep the result for retry")
                self.assertFalse(list((self.mod.ARCHIVE_RESULTS_DIR).glob(f"{tid}-*.txt")),
                                 "failed POST must not archive")
                self.assertIn(tid, inflight, "failed POST must stay in flight")

    # 1c — an unreadable alias ledger defers rather than POSTing under a guessed id
    def test_skip_marker_defers_when_alias_ledger_unreadable(self):
        self._result("tledger", "[no-send] internal only")
        inflight = {"tledger"}
        with patch.object(self.mod, "_delivery_tid", return_value=None):
            self.mod._post_ready_results(inflight)
        self.assertEqual([c for c in self.calls if c[1] == "/v1/results"], [],
                         "must not POST under a guessed delivery id")
        self.assertTrue((self.mod.RESULTS_DIR / "tledger.txt").exists())
        self.assertIn("tledger", inflight)

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

    # failure branches (coverage bar: every degrade path observable)
    def test_upload_stat_and_read_failures_noted(self):
        fd, fpath = tempfile.mkstemp(prefix="sutando-attach-test-", suffix=".txt", dir="/tmp")
        os.write(fd, b"x"); os.close(fd)
        self.addCleanup(lambda: os.path.exists(fpath) and os.unlink(fpath))
        with patch.object(self.mod.os.path, "getsize", side_effect=OSError("boom")):
            ok, reason = self.mod._upload_attachment("!r:s", fpath)
        self.assertFalse(ok); self.assertIn("stat failed", reason)
        with patch("builtins.open", side_effect=OSError("denied")):
            ok, reason = self.mod._upload_attachment("!r:s", fpath)
        self.assertFalse(ok); self.assertIn("read failed", reason)

    def test_upload_oversize_refused(self):
        fd, fpath = tempfile.mkstemp(prefix="sutando-attach-test-", suffix=".bin", dir="/tmp")
        os.write(fd, b"x"); os.close(fd)
        self.addCleanup(lambda: os.path.exists(fpath) and os.unlink(fpath))
        with patch.object(self.mod, "MAX_MEDIA_BYTES", 0):
            ok, reason = self.mod._upload_attachment("!r:s", fpath)
        self.assertFalse(ok); self.assertIn("exceeds", reason)

    def test_upload_http_and_network_errors(self):
        import urllib.error
        fd, fpath = tempfile.mkstemp(prefix="sutando-attach-test-", suffix=".txt", dir="/tmp")
        os.write(fd, b"x"); os.close(fd)
        self.addCleanup(lambda: os.path.exists(fpath) and os.unlink(fpath))
        self._req_patch.stop()
        try:
            with patch.object(self.mod, "_req",
                              side_effect=urllib.error.HTTPError("u", 500, "boom", {}, None)):
                ok, reason = self.mod._upload_attachment("!r:s", fpath)
            self.assertFalse(ok); self.assertIn("HTTP 500", reason)
            with patch.object(self.mod, "_req",
                              side_effect=urllib.error.URLError("down")):
                ok, reason = self.mod._upload_attachment("!r:s", fpath)
            self.assertFalse(ok); self.assertIn("network error", reason)
        finally:
            self._req_patch.start()

    def test_result_post_errors_leave_result_for_retry(self):
        import urllib.error
        self._req_patch.stop()
        try:
            for exc in (urllib.error.HTTPError("u", 502, "bad", {}, None),
                        urllib.error.URLError("down")):
                self._result("t9", "plain reply")
                with patch.object(self.mod, "_req", side_effect=exc):
                    inflight = {"t9"}
                    self.mod._post_ready_results(inflight)
                # not archived, still inflight — the next loop retries
                self.assertIn("t9", inflight)
                self.assertTrue((self.mod.RESULTS_DIR / "t9.txt").exists())
                (self.mod.RESULTS_DIR / "t9.txt").unlink()
        finally:
            self._req_patch.start()

    def test_save_rooms_persist_failure_never_raises(self):
        with patch.object(self.mod.json, "dumps", side_effect=RuntimeError("boom")):
            self.mod._save_task_rooms({"a": "!x:s"})  # must not raise

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
