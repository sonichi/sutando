#!/usr/bin/env python3
"""Authorization is not cloud trust: src/connections.py never touches cloud-auth state.

A connection row is a launch directory entry. Adding one must not read, import or
write anything the Sutando Cloud bearer path (`cloud_auth`, `credential_resolver`,
`state/auth/`) reads. Pinned statically (imports and string constants) and at
runtime (every file the module writes sits under state/connections*).

Run: python3 tests/connections-no-cloud-trust.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import ast
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import connections as C  # noqa: E402

C.DEFAULT_GETADDRINFO = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))]

SOURCE = ROOT / "src" / "connections.py"
FORBIDDEN_MODULES = {"cloud_auth", "credential_resolver", "trusted_cloud_hosts"}
FORBIDDEN_TEXT = ("cloud-auth", "cloud_auth", "trusted_cloud_hosts", "state/auth", "apiBase")


class FakeVault:
    store = {}

    def get(self, key):
        return self.store[key]

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def _imports(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class NoCloudTrust(unittest.TestCase):
    def test_no_cloud_auth_import_or_mention(self):
        tree = ast.parse(SOURCE.read_text())
        self.assertFalse(_imports(tree) & FORBIDDEN_MODULES)
        constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for needle in FORBIDDEN_TEXT:
            self.assertFalse(any(needle in c for c in constants), needle)

    def test_writes_stay_under_state_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "state" / "auth").mkdir(parents=True)
            sentinel = ws / "state" / "auth" / "cloud-auth.json"
            sentinel.write_text("{}")
            before = sentinel.stat().st_mtime_ns
            vault = FakeVault()
            C.import_from_env({"REMOTE_TASK_TOKEN": "https://a-fixture.net|s"}, workspace=ws, vault=vault)
            C.add_connection("https://b-fixture.net|t", added_by="ui", expected_server="b-fixture.net",
                             fetch=lambda *a: {}, workspace=ws, vault=vault)
            C.set_primary("b-fixture-net", workspace=ws)
            C.launch("b-fixture-net", repo=ws, logs_dir=ws / "logs", environ={"SUTANDO_BRIDGE_LOCK": "0"}, workspace=ws, vault=vault,
                     spawn=lambda argv, env, log: 4_194_404)  # above any pid_max: never runs
            C.remove_connection("b-fixture-net", force=True, workspace=ws, vault=vault)
            written = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file()
                             and p != sentinel)
            for rel in written:
                self.assertTrue(rel.startswith("state/connections"), rel)
            self.assertEqual(sentinel.stat().st_mtime_ns, before)
            self.assertEqual(sentinel.read_text(), "{}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
