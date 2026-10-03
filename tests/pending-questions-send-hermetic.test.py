#!/usr/bin/env python3
"""Targeted pin: the send suite imports the real gateway writer, which resolves
channel config, token, URL and queue dirs at import. Every one of those must be
pointed at scratch by a MODULE-LEVEL assignment before that import, else the test
reads the operator's token and writes their live state (the shape
scripts/lint-hermetic-bridge-tests.py enforces for the channel bridges)."""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "tests" / "pending-questions-send.test.py"
REQUIRED = ("CLAUDE_CONFIG_DIR", "AG2_DEVICE_ENV", "REMOTE_TASK_TOKEN", "REMOTE_TASK_URL",
            "REMOTE_MEDIA_DIR")
GATEWAY = ("remote_gateway_bridge", "remote-gateway-bridge.py")


def _env_key(node):
    t = node.targets[0] if isinstance(node, ast.Assign) and node.targets else None
    if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
            and t.value.attr == "environ" and isinstance(t.value.value, ast.Name)
            and t.value.value.id == "os" and isinstance(t.slice, ast.Constant)):
        return t.slice.value
    return None


class Hermetic(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(SUITE.read_text())
        self.src = SUITE.read_text()

    def test_every_gateway_input_is_isolated_at_module_level(self):
        keys = {_env_key(n) for n in self.tree.body}
        for k in REQUIRED:
            with self.subTest(k):
                self.assertIn(k, keys, f"{k} is not assigned at module level in {SUITE.name}")

    def test_the_config_dir_comes_from_a_temporary_directory(self):
        for n in self.tree.body:
            if _env_key(n) == "CLAUDE_CONFIG_DIR":
                calls = {c.func.attr for c in ast.walk(n.value)
                         if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
                self.assertTrue(calls & {"mkdtemp", "TemporaryDirectory"}, ast.dump(n.value))
                return
        self.fail("no module-level CLAUDE_CONFIG_DIR assignment")

    def test_the_gateway_is_imported_only_after_the_isolation(self):
        iso = min(n.lineno for n in self.tree.body if _env_key(n) in REQUIRED)
        hits = [i + 1 for i, line in enumerate(self.src.splitlines())
                if any(g in line for g in GATEWAY) and ("import" in line or "exec_module" in line)]
        self.assertTrue(hits, "the suite no longer imports the gateway; retire this pin")
        self.assertLess(iso, min(hits), f"isolation at line {iso} must precede the import at {min(hits)}")

    def test_the_workspace_wrapper_is_not_executed(self):
        # src/remote-gateway-bridge.py resolves the live workspace at import; only
        # the vendored module, whose dirs come from the isolated env, may be loaded.
        self.assertNotIn("remote-gateway-bridge.py", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
