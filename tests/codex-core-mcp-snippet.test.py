"""The docs/codex-core.md MCP lookup fails loudly instead of printing an empty URL."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent


def _snippet():
    text = (REPO / "docs" / "codex-core.md").read_text()
    m = re.search(r"```bash\n(server=.*?)```", text, re.S)
    assert m, "snippet not found"
    return m.group(1)


def _run(**extra):
    with tempfile.TemporaryDirectory() as ws:
        env = isolated_child_env(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=ws, **extra)
        return subprocess.run(["bash", "-c", _snippet() + '\n[ -n "$mcp_url" ]'], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=60)


class CodexCoreMcpSnippet(unittest.TestCase):
    def test_no_primary_fails_and_names_fix(self):
        r = _run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("AG2_SERVER", r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_explicit_server_prints_its_url(self):
        r = _run(AG2_SERVER="ag2.space")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "https://mcp.ag2.space/mcp")

    def test_server_without_mcp_url_fails(self):
        r = _run(AG2_SERVER="nowhere.invalid")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no mcp_url", r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
