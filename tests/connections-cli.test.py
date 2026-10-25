#!/usr/bin/env python3
"""scripts/connections.py through a subprocess, against a fake `security` binary.

The CLI is a dispatch layer: each verb calls one src/connections.py unit and prints.
The vault path is the real `vault_intercept`, pointed at a stub Keychain on PATH, so
the documented programmatic path is exercised without touching the host's Keychain.
Tokens name a closed local port, so the preview probe fails fast and unverified.
No output may carry a secret.

Run: python3 tests/connections-cli.test.py
"""
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "connections.py"

TOKEN_A = "http://127.0.0.1:9/alpha|secret-alpha"
TOKEN_B = "http://127.0.0.2:9/beta|secret-beta"

FAKE_SECURITY = r"""#!/usr/bin/env bash
# Stub Keychain: items are files under $FAKE_KEYCHAIN named by service.
verb="$1"; shift
key=""; value=""
while [ $# -gt 0 ]; do
  case "$1" in
    -s) key="$2"; shift 2;;
    -w) if [ "$verb" = add-generic-password ]; then value="$2"; shift 2; else shift; fi;;
    *) shift;;
  esac
done
case "$verb" in
  add-generic-password) printf '%s' "$value" > "$FAKE_KEYCHAIN/$key";;
  find-generic-password) [ -f "$FAKE_KEYCHAIN/$key" ] && cat "$FAKE_KEYCHAIN/$key" || exit 44;;
  delete-generic-password) [ -f "$FAKE_KEYCHAIN/$key" ] && rm "$FAKE_KEYCHAIN/$key" || exit 44;;
  *) exit 1;;
esac
"""


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        box = Path(self.tmp.name)
        self.ws = box / "workspace"
        self.keychain = box / "keychain"
        self.keychain.mkdir()
        bindir = box / "bin"
        bindir.mkdir()
        sec = bindir / "security"
        sec.write_text(FAKE_SECURITY)
        sec.chmod(0o755)
        self.env = hermetic_env(
            box / "home",
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            FAKE_KEYCHAIN=str(self.keychain),
            SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1",
            # The vault manifest resolves the workspace; keep it in the box.
            SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(self.ws),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, env_extra=None, stdin=None):
        env = dict(self.env, **(env_extra or {}))
        return subprocess.run([sys.executable, str(CLI), "--workspace", str(self.ws), *args],
                              env=env, capture_output=True, text=True, timeout=60, input=stdin)

    def assert_no_secret(self, proc):
        for stream in (proc.stdout, proc.stderr):
            self.assertNotIn("secret-alpha", stream)
            self.assertNotIn("secret-beta", stream)

    def test_add_previews_then_asks(self):
        proc = self.run_cli("add", TOKEN_A, stdin="n\n")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("server:    127.0.0.1  (unverified: from the gateway host)", proc.stdout)
        self.assertIn("gateway:   http://127.0.0.1:9/alpha", proc.stdout)
        self.assertIn("not saved", proc.stdout)
        self.assert_no_secret(proc)
        self.assertEqual(self.run_cli("list").stdout.strip(), "no connections")

        proc = self.run_cli("add", TOKEN_A, stdin="y\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("added 127-0-0-1 for 127.0.0.1 (token in vault as AG2_REMOTE_TOKEN_127_0_0_1) — primary",
                      proc.stdout)
        self.assertEqual((self.keychain / "AG2_REMOTE_TOKEN_127_0_0_1").read_text(), TOKEN_A)
        manifest = json.loads((self.ws / "state" / "secret-vault" / "keys.json").read_text())
        self.assertIn("AG2_REMOTE_TOKEN_127_0_0_1", manifest)

    def test_yes_needs_the_expected_server_and_checks_it(self):
        proc = self.run_cli("add", TOKEN_A, "--yes")
        self.assertEqual(proc.returncode, 2)
        proc = self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "somewhere.else")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("127.0.0.1", proc.stderr)
        self.assert_no_secret(proc)
        self.assertEqual(sorted(self.keychain.iterdir()), [])
        proc = self.run_cli("add", "-", "--yes", "--expected-server", "127.0.0.1", "--added-by", "login",
                            stdin=TOKEN_A + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "127.0.0.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already present:", proc.stdout)

    def test_list_set_primary_remove(self):
        self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "127.0.0.1")
        self.run_cli("add", TOKEN_B, "--yes", "--expected-server", "127.0.0.2")
        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("127-0-0-1", proc.stdout)
        self.assertIn("pending", proc.stdout)
        self.assert_no_secret(proc)
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([(r["instance"], r["primary"], r["state"]) for r in rows],
                         [("127-0-0-1", True, "pending"), ("127-0-0-2", False, "pending")])

        proc = self.run_cli("set-primary", "127-0-0-2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([r["primary"] for r in rows], [False, True])

        proc = self.run_cli("remove", "127-0-0-2")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("without force", proc.stderr)
        proc = self.run_cli("remove", "127-0-0-2", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.keychain / "AG2_REMOTE_TOKEN_127_0_0_2").exists())
        self.assertTrue((self.keychain / "AG2_REMOTE_TOKEN_127_0_0_1").exists())
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([(r["instance"], r["primary"]) for r in rows], [("127-0-0-1", False)],
                         "no row inherits the primary flag")

    def test_import_env_then_launch_plan(self):
        extra = {"REMOTE_TASK_TOKEN": TOKEN_A, "AG2_REMOTE_TOKEN_DEV": TOKEN_B}
        proc = self.run_cli("import-env", env_extra=extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ["imported (legacy) from REMOTE_TASK_TOKEN", "imported dev from AG2_REMOTE_TOKEN_DEV"])
        self.assert_no_secret(proc)
        self.assertEqual(self.run_cli("import-env", env_extra=extra).stdout.strip(), "nothing to import")

        proc = self.run_cli("launch-plan", env_extra={"AG2_REMOTE_TOKEN_QA": "http://127.0.0.3:9/|q"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ["dev\tAG2_REMOTE_TOKEN_DEV\tdev-ag2space\t0", "qa\tAG2_REMOTE_TOKEN_QA\tqa-ag2space\t0"])
        self.assert_no_secret(proc)

    def test_launch_and_reconnect_run_the_bridge_entry_point(self):
        box = Path(self.tmp.name)
        repo = box / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "remote-gateway-bridge.py").write_text(
            "import os, pathlib\n"
            "pathlib.Path(os.environ['FAKE_OUT']).write_text(os.environ['GATEWAY_INSTANCE'] + ' ' "
            "+ os.environ['REMOTE_TASK_CHANNEL_DIR'] + ' ' + repr(os.environ['REMOTE_PROACTIVE_ROOM']))\n")
        out = box / "launched.txt"
        self.run_cli("add", TOKEN_B, "--yes", "--expected-server", "127.0.0.2")
        proc = self.run_cli("launch", "127-0-0-2", "--repo", str(repo), "--logs-dir", str(box / "logs"),
                            env_extra={"FAKE_OUT": str(out), "REMOTE_PROACTIVE_ROOM": "!room:x"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("launched 127-0-0-2 (pid ", proc.stdout)
        for _ in range(50):
            if out.exists():
                break
            import time
            time.sleep(0.1)
        self.assertEqual(out.read_text(), "127-0-0-2 127-0-0-2-ag2space '!room:x'", "the primary keeps the room")
        record = json.loads((self.ws / "state" / "connections-launch" / "127-0-0-2.json").read_text())
        self.assertTrue(record["ok"])
        proc = self.run_cli("reconnect", "127-0-0-2", "--repo", str(repo), "--logs-dir", str(box / "logs"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reconnected 127-0-0-2", proc.stdout)
        proc = self.run_cli("launch", "nope", "--repo", str(repo), "--logs-dir", str(box / "logs"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no connection", proc.stderr)

    def test_vault_failure_is_named_and_writes_no_row(self):
        (self.keychain / "AG2_REMOTE_TOKEN_127_0_0_1").mkdir()  # a directory makes the stub's write fail
        proc = self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "127.0.0.1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("secret vault", proc.stderr)
        self.assert_no_secret(proc)
        self.assertEqual(self.run_cli("list").stdout.strip(), "no connections")


if __name__ == "__main__":
    unittest.main(verbosity=1)
