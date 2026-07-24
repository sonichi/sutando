"""G1.5 node-bundle: resolve_node_runtime precedence + check_node_runtime shape.

Covers the resolver's four outcomes (bundled / app-bundle / system / none) and
the check's ok/down mapping, using temp executables so no host state leaks in.
"""

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)


def _make_exec(path: Path) -> str:
    path.write_text("#!/bin/sh\necho v99.0.0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class ResolveNodeRuntimeTest(unittest.TestCase):
    def test_sutando_node_wins_when_executable(self):
        with tempfile.TemporaryDirectory() as td:
            node = _make_exec(Path(td) / "node")
            out = hc.resolve_node_runtime(
                env={"SUTANDO_NODE": node}, which=lambda _: "/usr/fake/node"
            )
            self.assertEqual(out, {"source": "bundled", "path": node})

    def test_sutando_node_ignored_when_missing(self):
        # A dangling SUTANDO_NODE must not mask the PATH fallback.
        out = hc.resolve_node_runtime(
            env={"SUTANDO_NODE": "/nonexistent/node", "SUTANDO_APP_NODE_DIR": "/nonexistent"},
            which=lambda _: "/usr/fake/node",
        )
        self.assertEqual(out, {"source": "system", "path": "/usr/fake/node"})

    def test_app_bundle_beats_path(self):
        with tempfile.TemporaryDirectory() as td:
            node = _make_exec(Path(td) / "node")
            out = hc.resolve_node_runtime(
                env={"SUTANDO_APP_NODE_DIR": td}, which=lambda _: "/usr/fake/node"
            )
            self.assertEqual(out, {"source": "app-bundle", "path": node})

    def test_none_when_nothing_resolves(self):
        out = hc.resolve_node_runtime(
            env={"SUTANDO_APP_NODE_DIR": "/nonexistent"}, which=lambda _: None
        )
        self.assertEqual(out, {"source": "none", "path": None})


class CheckNodeRuntimeTest(unittest.TestCase):
    def test_down_when_none(self):
        orig = hc.resolve_node_runtime
        hc.resolve_node_runtime = lambda: {"source": "none", "path": None}
        try:
            out = hc.check_node_runtime()
        finally:
            hc.resolve_node_runtime = orig
        self.assertEqual(out["name"], "node-runtime")
        self.assertEqual(out["status"], "down")
        self.assertIn("JS services cannot start", out["detail"])

    def test_ok_reports_version_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            node = _make_exec(Path(td) / "node")
            orig = hc.resolve_node_runtime
            hc.resolve_node_runtime = lambda: {"source": "bundled", "path": node}
            try:
                out = hc.check_node_runtime()
            finally:
                hc.resolve_node_runtime = orig
        self.assertEqual(out["status"], "ok")
        self.assertIn("v99.0.0", out["detail"])
        self.assertIn("bundled", out["detail"])

    def test_ok_survives_version_probe_failure(self):
        # A resolvable-but-broken binary still reports ok with a probe note —
        # existence is the check's job; runnability shows in the detail.
        with tempfile.TemporaryDirectory() as td:
            broken = Path(td) / "node"
            broken.write_text("not a script")
            broken.chmod(0o755)
            orig = hc.resolve_node_runtime
            hc.resolve_node_runtime = lambda: {"source": "system", "path": str(broken)}
            try:
                out = hc.check_node_runtime()
            finally:
                hc.resolve_node_runtime = orig
        self.assertEqual(out["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
