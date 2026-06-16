"""Twin of node.test.ts."""

from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability import node  # noqa: E402


class NodeTest(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("SUTANDO_NODE_ID", None)
        node.reset_node_id()

    def test_override(self) -> None:
        os.environ["SUTANDO_NODE_ID"] = "mac-studio-test"
        node.reset_node_id()
        self.assertEqual(node.node_id(), "mac-studio-test")

    def test_hostname_fallback(self) -> None:
        os.environ.pop("SUTANDO_NODE_ID", None)
        node.reset_node_id()
        self.assertEqual(node.node_id(), socket.gethostname().split(".")[0] or "unknown")

    def test_caches_until_reset(self) -> None:
        os.environ["SUTANDO_NODE_ID"] = "first"
        node.reset_node_id()
        self.assertEqual(node.node_id(), "first")
        os.environ["SUTANDO_NODE_ID"] = "second"  # no reset -> cached value holds
        self.assertEqual(node.node_id(), "first")


if __name__ == "__main__":
    unittest.main()
