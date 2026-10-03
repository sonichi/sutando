#!/usr/bin/env python3
"""Pin on the send suite's real-gateway test. The vendored gateway resolves channel
config, token, URL, queue dirs and telemetry at import and reports a telemetry event
per queued task. This pins the EFFECTIVE contract, not nearby syntax: the module's
isolation block is executed and the resulting environment checked for containment
and exact values; every contract key is assigned exactly once; the last isolation
precedes the first gateway import; and the deny helper really rebinds every seam."""
import ast
import importlib.util
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "tests" / "pending-questions-send.test.py"
PATH_KEYS = ("CLAUDE_CONFIG_DIR", "AG2_DEVICE_ENV", "REMOTE_MEDIA_DIR", "AGENT_CONNECT_TASK_DIR",
             "AGENT_CONNECT_RESULT_DIR", "AGENT_CONNECT_STATE_DIR", "SUTANDO_STATE_DIR",
             "SUTANDO_TELEMETRY_ID_FILE")
EXACT = {"REMOTE_TASK_TOKEN": "http://127.0.0.1:9|fake-gateway-token",
         "REMOTE_TASK_URL": "http://127.0.0.1:9", "SUTANDO_TELEMETRY": "0", "DO_NOT_TRACK": "1"}
CONTRACT_KEYS = PATH_KEYS + tuple(EXACT)
GATEWAY_MODULE = "remote_gateway_bridge"
SEAMS = (("rgb", "_req"), ("urllib.request", "urlopen"), ("socket", "create_connection"))


def _env_key(node):
    """The key of `os.environ["KEY"] = ...` (any statement, any depth), else None."""
    if not isinstance(node, ast.Assign):
        return None
    for t in node.targets:
        if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
                and t.value.attr == "environ" and isinstance(t.value.value, ast.Name)
                and t.value.value.id == "os" and isinstance(t.slice, ast.Constant)):
            return t.slice.value
    return None


def _imports_gateway(node):
    if isinstance(node, ast.ImportFrom):
        return any(a.name == GATEWAY_MODULE for a in node.names)
    if isinstance(node, ast.Import):
        return any(GATEWAY_MODULE in a.name for a in node.names)
    return False


def _load_suite():
    """Execute the suite's module (its isolation block runs; no test runs)."""
    saved = dict(os.environ)
    spec = importlib.util.spec_from_file_location("pq_send_suite_under_pin", SUITE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, saved


class Hermetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SUITE.read_text()
        cls.tree = ast.parse(cls.src)
        cls.mod, cls.saved_env = _load_suite()

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls.saved_env)
        sys.modules.pop("pq_send_suite_under_pin", None)

    def test_effective_environment_is_contained_and_exact(self):
        scratch = os.path.realpath(self.mod._GW_SCRATCH)
        self.assertTrue(os.path.isdir(scratch))
        for key in PATH_KEYS:
            with self.subTest(key):
                value = os.environ.get(key)
                self.assertIsNotNone(value, f"{key} unset after the isolation block")
                real = os.path.realpath(value)
                self.assertTrue(real == scratch or real.startswith(scratch + os.sep),
                                f"{key}={value} resolves outside scratch")
        for key, want in EXACT.items():
            with self.subTest(key):
                self.assertEqual(os.environ.get(key), want)

    def test_each_contract_key_is_assigned_exactly_once_and_environ_is_never_mutated_otherwise(self):
        counts = {k: 0 for k in CONTRACT_KEYS}
        for n in ast.walk(self.tree):
            k = _env_key(n)
            if k in counts:
                counts[k] += 1
            # any mutating call on os.environ, whatever its argument shape, and os.putenv
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                recv = n.func.value
                if n.func.attr in ("update", "setdefault", "pop", "popitem", "clear", "__setitem__") and \
                        isinstance(recv, ast.Attribute) and recv.attr == "environ" and \
                        isinstance(recv.value, ast.Name) and recv.value.id == "os":
                    self.fail(f"os.environ.{n.func.attr}() at line {n.lineno}")
                if n.func.attr == "putenv" and isinstance(recv, ast.Name) and recv.id == "os":
                    self.fail(f"os.putenv() at line {n.lineno}")
        for k, c in counts.items():
            with self.subTest(k):
                self.assertEqual(c, 1, f"{k} assigned {c} times; a later override would win")

    def test_the_runtime_check_rejects_a_poisoned_environment(self):
        good = {k: os.environ[k] for k in CONTRACT_KEYS}
        self.mod.assert_isolated(good, self.mod._GW_SCRATCH)
        for key, bad in (("REMOTE_MEDIA_DIR", "/tmp/pq-send-outside-scratch"),
                         ("REMOTE_TASK_TOKEN", "http://127.0.0.1:9|inherited"),
                         ("SUTANDO_TELEMETRY", "1"), ("AGENT_CONNECT_STATE_DIR", "")):
            with self.subTest(key):
                with self.assertRaises(AssertionError):
                    self.mod.assert_isolated({**good, key: bad}, self.mod._GW_SCRATCH)

    def test_the_importing_test_runs_the_runtime_check_around_the_writes(self):
        funcs = [n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)
                 and any(_imports_gateway(c) for c in ast.walk(n))]
        self.assertEqual(len(funcs), 1)
        fn = funcs[0]
        checks = [c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                  and c.func.id == "assert_isolated"]
        writes = [c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                  and c.func.attr == "_write_task"]
        imports = [c.lineno for c in ast.walk(fn) if _imports_gateway(c)]
        self.assertTrue(checks and writes and imports)
        self.assertLess(min(checks), min(imports), "the runtime check must precede the gateway import")
        self.assertGreater(max(checks), max(writes), "the runtime check must follow the last write")

    def test_all_isolation_precedes_the_first_gateway_import(self):
        last_iso = max(n.lineno for n in ast.walk(self.tree) if _env_key(n) in CONTRACT_KEYS)
        hits = [n.lineno for n in ast.walk(self.tree) if _imports_gateway(n)]
        self.assertTrue(hits, "the suite no longer imports the gateway; retire this pin")
        self.assertLess(last_iso, min(hits), f"isolation ends at line {last_iso}, import at {min(hits)}")
        self.assertNotIn("remote-gateway-bridge.py", self.src, "the workspace wrapper must not be executed")

    def test_the_deny_helper_rebinds_every_seam_and_each_one_refuses(self):
        self.assertEqual(tuple(self.mod.OUTBOUND_SEAMS), SEAMS)

        class Stub:
            pass
        targets = {}
        for name, attr in SEAMS:
            stub = targets.setdefault(name, Stub())
            setattr(stub, attr, lambda *a, **k: "reached")
        attempts = []
        rebound = self.mod.deny_outbound(targets, attempts)
        self.assertEqual(tuple(rebound), SEAMS)
        for name, attr in SEAMS:
            with self.subTest(f"{name}.{attr}"):
                with self.assertRaises(RuntimeError):
                    getattr(targets[name], attr)()
        self.assertEqual(sorted(attempts), sorted(a for _, a in SEAMS))

    def test_the_importing_test_uses_the_helper_and_checks_its_result(self):
        funcs = [n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)
                 and any(_imports_gateway(c) for c in ast.walk(n))]
        self.assertEqual(len(funcs), 1, "exactly one test imports the gateway")
        body = ast.unparse(funcs[0])
        self.assertIn("self.assertEqual(deny_outbound(targets, attempts), OUTBOUND_SEAMS)", body)
        writes = [c.lineno for c in ast.walk(funcs[0]) if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Attribute) and c.func.attr == "_write_task"]
        denies = [c.lineno for c in ast.walk(funcs[0]) if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Name) and c.func.id == "deny_outbound"]
        self.assertTrue(writes and denies and max(denies) < min(writes), "deny must precede the first write")
        for probe in ("rgb.TOKEN", "telemetry.opted_out()", "SUTANDO_TELEMETRY_ID_FILE",
                      "a request reached a transport"):
            self.assertIn(probe, body, f"the behaviour check for {probe} is gone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
