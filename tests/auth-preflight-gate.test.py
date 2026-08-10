#!/usr/bin/env python3
"""Boot gate for the logged-out-CLI class (#2396) — src/auth-preflight-gate.sh.

Each case runs the gate inside a self-contained fixture repo in a tempdir
(stub scripts/sutando-config.sh + stub src/auth_preflight.py), so the gate's
own REPO-relative resolution points at the fixture and every fail-loud side
effect (pending-questions append, proactive file) lands in the fixture
workspace — never in the real one. Fixture paths are built from self.tmp (the
actual tempdir Path); see #2411 for the cwd-leak bug this avoids.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REAL_REPO = Path(__file__).resolve().parent.parent
GATE_SRC = REAL_REPO / "src" / "auth-preflight-gate.sh"

OK_PROBE = "import sys\nprint('{}')\nsys.exit(0)\n"
LOGIN_PROBE = (
    "import sys\n"
    "print('{\"verdict\": \"login_required\", \"remedy\": \"needs GUI /login on testhost: run X\"}')\n"
    "sys.exit(2)\n"
)


class TestAuthPreflightGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apg-"))
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        self.ws = self.tmp / "ws"
        (self.ws / "results").mkdir(parents=True)
        shutil.copy(GATE_SRC, self.repo / "src" / "auth-preflight-gate.sh")
        (self.repo / "scripts" / "sutando-config.sh").write_text(
            "#!/bin/bash\n"
            f'case "$1" in workspace) echo "{self.ws}";; host-label) echo testhost;; esac\n'
        )
        self.config_dir = self.tmp / "ccd"
        self.config_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_probe(self, body: str):
        (self.repo / "src" / "auth_preflight.py").write_text(body)

    def _run(self, env_extra=None, config_dir=None):
        import os
        env = dict(os.environ)
        env.pop("SSH_CONNECTION", None)
        env.pop("SUTANDO_SKIP_AUTH_PREFLIGHT", None)
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(self.repo / "src" / "auth-preflight-gate.sh"),
             str(config_dir or self.config_dir)],
            capture_output=True, text=True, env=env)

    def test_authenticated_passes_through(self):
        self._write_probe(OK_PROBE)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)

    def test_login_required_aborts_with_remedy_on_stderr(self):
        self._write_probe(LOGIN_PROBE)
        r = self._run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("ABORTING startup", r.stderr)
        self.assertIn("needs GUI /login on testhost", r.stderr)

    def test_login_required_writes_pending_questions_and_proactive(self):
        self._write_probe(LOGIN_PROBE)
        self._run()
        pq = self.ws / "hosts" / "testhost" / "pending-questions.md"
        self.assertTrue(pq.exists(), "pending-questions.md not written")
        self.assertIn("BOOT ABORTED", pq.read_text())
        proactive = list((self.ws / "results").glob("proactive-*.txt"))
        self.assertEqual(len(proactive), 1)
        body = proactive[0].read_text()
        self.assertTrue(body.startswith("[dm-only]"), body)
        self.assertIn("needs GUI /login on testhost", body)

    def test_skip_env_bypasses_gate(self):
        self._write_probe(LOGIN_PROBE)  # would abort if consulted
        r = self._run(env_extra={"SUTANDO_SKIP_AUTH_PREFLIGHT": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipped", r.stdout)

    def test_missing_probe_module_fails_open(self):
        r = self._run()  # no auth_preflight.py written
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipping", r.stderr)

    def test_ssh_context_warns_upfront(self):
        self._write_probe(LOGIN_PROBE)
        r = self._run(env_extra={"SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("SSH session detected", r.stderr)
        self.assertIn("GUI Terminal", r.stderr)

    def test_malformed_probe_output_falls_back_to_raw(self):
        self._write_probe("import sys\nprint('totally broken not json')\nsys.exit(2)\n")
        r = self._run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("totally broken not json", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
