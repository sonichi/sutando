#!/usr/bin/env python3
"""Guard: import-time workspace dir creation must tolerate an absent resolved parent —
workspace-relative mkdirs need parents=True, not exist_ok alone.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Patch resolve_workspace BEFORE importing the module under test, then report what happened.
PROBE = r"""
import sys, json
from pathlib import Path
sys.path.insert(0, {src!r})
import workspace_default
target = Path({target!r})
workspace_default.resolve_workspace = lambda *a, **k: target
import importlib.util
spec = importlib.util.spec_from_file_location("_mut", {module!r})
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except FileNotFoundError as exc:
    print(json.dumps({{"error": "FileNotFoundError", "detail": str(exc)}})); raise SystemExit(0)
except Exception as exc:  # unrelated import failure — report, don't mask
    print(json.dumps({{"error": type(exc).__name__, "detail": str(exc)}})); raise SystemExit(0)
print(json.dumps({{
    "error": None,
    "tasks": (target / "tasks").is_dir(),
    "results": (target / "results").is_dir(),
    "resolved": str(target),
}}))
"""


def run_probe(module: Path, target: Path):
    code = PROBE.format(src=str(REPO / "src"), target=str(target), module=str(module))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(REPO), timeout=120)
    import json
    for line in reversed(out.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"probe produced no JSON.\nstdout={out.stdout[-800:]}\nstderr={out.stderr[-800:]}")


class WorkspaceDirsCreatedWhenParentAbsent(unittest.TestCase):
    def test_agent_api_imports_when_workspace_parent_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            # two levels absent: neither <td>/absent nor its child exists
            target = Path(td) / "absent" / "workspace"
            self.assertFalse(target.parent.exists(), "fixture must start with the parent missing")
            res = run_probe(REPO / "src" / "agent-api.py", target)
            self.assertIsNone(res["error"], f"import raised {res['error']}: {res.get('detail')}")
            # isolation check: the dirs must land under the tmp target, not the live workspace
            self.assertEqual(res["resolved"], str(target))
            self.assertTrue(res["tasks"], "tasks/ not created under the absent parent")
            self.assertTrue(res["results"], "results/ not created under the absent parent")

    def test_every_workspace_relative_mkdir_passes_parents(self):
        """Sites whose parent can be absent must not rely on exist_ok alone."""
        import re
        offenders = []
        for rel in ("src/agent-api.py", "src/github-webhook.py"):
            text = (REPO / rel).read_text()
            for var in re.findall(r"^\s*([A-Z_]+)\s*=\s*WORKSPACE_DIR\s*/", text, re.M):
                for m in re.finditer(rf"^\s*{var}\.mkdir\(([^)]*)\)", text, re.M):
                    if "parents=True" not in m.group(1):
                        offenders.append(f"{rel}: {var}.mkdir({m.group(1)})")
        self.assertEqual(offenders, [], f"workspace-relative mkdir without parents=True: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
