#!/usr/bin/env python3
"""Structural pin on the send suite's real-gateway test. The vendored gateway
resolves channel config, token, URL, queue dirs and telemetry at import and
reports a telemetry event per queued task, so the suite must set every one of
those to a safe value at MODULE level before the import, and the test that uses
the writer must block the network before it writes. This pins the values and the
order, not just that assignments exist."""
import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "tests" / "pending-questions-send.test.py"
SCRATCH_NAME = "_GW_SCRATCH"
# key -> how its value must look: "tmp" (derived from a temp factory or the scratch
# name), or an exact string
CONTRACT = {
    "CLAUDE_CONFIG_DIR": "tmp", "AG2_DEVICE_ENV": "tmp", "REMOTE_MEDIA_DIR": "tmp",
    "AGENT_CONNECT_TASK_DIR": "tmp", "AGENT_CONNECT_RESULT_DIR": "tmp",
    "AGENT_CONNECT_STATE_DIR": "tmp", "SUTANDO_STATE_DIR": "tmp",
    "SUTANDO_TELEMETRY_ID_FILE": "tmp",
    "REMOTE_TASK_TOKEN": "http://127.0.0.1:9|fake-gateway-token",
    "REMOTE_TASK_URL": "http://127.0.0.1:9",
    "SUTANDO_TELEMETRY": "0", "DO_NOT_TRACK": "1",
}
GATEWAY_MODULE = "remote_gateway_bridge"
NETWORK_SEAMS = {("rgb", "_req"), ("urllib.request", "urlopen"), ("socket", "create_connection")}


def _env_assign(node):
    """(key, value-node) for a module-level `os.environ["KEY"] = ...`, else None."""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    t = node.targets[0]
    if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
            and t.value.attr == "environ" and isinstance(t.value.value, ast.Name)
            and t.value.value.id == "os" and isinstance(t.slice, ast.Constant)):
        return t.slice.value, node.value
    return None


def _is_tmp(value):
    for c in ast.walk(value):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and \
                c.func.attr in ("mkdtemp", "TemporaryDirectory"):
            return True
        if isinstance(c, ast.Name) and c.id == SCRATCH_NAME:
            return True
    return False


def _imports_gateway(node):
    for c in ast.walk(node):
        if isinstance(c, ast.ImportFrom) and any(a.name == GATEWAY_MODULE for a in c.names):
            return True
        if isinstance(c, ast.Import) and any(GATEWAY_MODULE in a.name for a in c.names):
            return True
    return False


class Hermetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SUITE.read_text()
        cls.tree = ast.parse(cls.src)
        cls.env = {}
        for n in cls.tree.body:
            hit = _env_assign(n)
            if hit:
                cls.env.setdefault(hit[0], (n.lineno, hit[1]))
        scratch = [n for n in cls.tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == SCRATCH_NAME for t in n.targets)]
        cls.scratch_ok = bool(scratch) and _is_tmp(scratch[0].value)

    def test_every_gateway_input_has_its_safe_value_at_module_level(self):
        self.assertTrue(self.scratch_ok, f"{SCRATCH_NAME} must be a module-level mkdtemp")
        for key, want in CONTRACT.items():
            with self.subTest(key):
                self.assertIn(key, self.env, f"{key} not assigned at module level")
                _, value = self.env[key]
                if want == "tmp":
                    self.assertTrue(_is_tmp(value), f"{key} does not point at scratch: {ast.dump(value)}")
                else:
                    self.assertIsInstance(value, ast.Constant)
                    self.assertEqual(value.value, want)

    def test_all_isolation_precedes_the_first_gateway_import(self):
        last_iso = max(line for line, _ in self.env.values())
        hits = [n.lineno for n in ast.walk(self.tree) if hasattr(n, "lineno") and _imports_gateway(n)]
        self.assertTrue(hits, "the suite no longer imports the gateway; retire this pin")
        self.assertLess(last_iso, min(hits), f"isolation ends at line {last_iso}, gateway imported at {min(hits)}")
        self.assertNotIn("remote-gateway-bridge.py", self.src, "the workspace wrapper must not be executed")

    def test_the_importing_test_blocks_every_network_seam_before_writing(self):
        funcs = [n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and _imports_gateway(n)]
        self.assertEqual(len(funcs), 1, "exactly one test imports the gateway")
        fn = funcs[0]
        seams, first_write = set(), None
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "_write_task":
                first_write = min(first_write or c.lineno, c.lineno)
        # the seams are rebound in one loop over (object, attribute) literals
        for c in ast.walk(fn):
            if isinstance(c, ast.Tuple) and len(c.elts) == 2 and isinstance(c.elts[1], ast.Constant):
                obj = ast.unparse(c.elts[0])
                if (obj, c.elts[1].value) in NETWORK_SEAMS and (first_write is None or c.lineno < first_write):
                    seams.add((obj, c.elts[1].value))
        self.assertEqual(seams, NETWORK_SEAMS, f"network seams blocked before the write: {sorted(seams)}")
        self.assertIsNotNone(first_write)
        body = ast.unparse(fn)
        for probe in ("rgb.TOKEN", "telemetry.opted_out()", "SUTANDO_TELEMETRY_ID_FILE",
                      "a request reached a transport"):
            self.assertIn(probe, body, f"the behaviour check for {probe} is gone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
