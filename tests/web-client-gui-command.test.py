#!/usr/bin/env python3
"""Structural regression test for web-client.ts POST /gui/command endpoint.

Guards the clear_tasks_history feature (added 2026-06-22):
  POST /gui/command {"command":"clear_tasks_history","keep":N}
  → SSE broadcast of gui-command event to all connected browser tabs
  → browser handler clears localStorage sutando-taskmap-v1 keeping N newest

Run: python3 tests/web-client-gui-command.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "web-client.ts").read_text()


class TestGuiCommandEndpoint(unittest.TestCase):
    """POST /gui/command must exist as a server-side handler."""

    def test_endpoint_path_present(self):
        self.assertIn("/gui/command", SRC, "POST /gui/command route must be present")

    def test_sse_broadcast(self):
        self.assertIn("gui-command", SRC, "endpoint must broadcast 'gui-command' SSE event")

    def test_sseClients_write(self):
        self.assertIn("sseClients", SRC, "endpoint must iterate sseClients to broadcast")


class TestGuiCommandBrowserHandler(unittest.TestCase):
    """Browser-side EventSource must handle gui-command events."""

    def test_listener_registered(self):
        self.assertIn(
            "addEventListener('gui-command'",
            SRC,
            "browser must register 'gui-command' SSE event listener",
        )

    def test_clear_tasks_history_command(self):
        self.assertIn(
            "clear_tasks_history",
            SRC,
            "browser handler must handle clear_tasks_history command",
        )

    def test_localstorage_key(self):
        self.assertIn(
            "sutando-taskmap-v1",
            SRC,
            "handler must reference the localStorage key sutando-taskmap-v1",
        )

    def test_keep_param(self):
        self.assertIn(
            "keepN",
            SRC,
            "handler must support keep=N param to retain N most recent tasks",
        )


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
