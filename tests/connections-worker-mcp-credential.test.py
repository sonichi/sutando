#!/usr/bin/env python3
"""A worker seat's s-space MCP proxy mints from the worker's own credential, never the core's.

`connections.worker_mcp_credential` answers with the worker's own identity row and the token the
vault holds for it, resolved by `connection_credential` like every room client's; it refuses, with
a reason and no secret, for a session that names no worker, a worker with no identity, a disabled
row or a missing token. `scripts/connections.py worker-mcp-credential` prints that one JSON line
to stdout for the proxy's memory, and `worker-mcp-credential-argv` the argv the launcher exports.

The record and the vault are fixtures; the CLI runs in a subprocess with a fake vault module.

Run: python3 tests/connections-worker-mcp-credential.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

VAULT = {}
_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = lambda var: VAULT[var]
sys.modules["vault_intercept"] = _FAKE_VI

import connections as C  # noqa: E402

ALPHA = "https://chat.alpha-fixture.net/relay"
CORE_AGENT, OWN_AGENT = "@core:alpha-fixture.net", "@helper:alpha-fixture.net"
W, OTHER_W = "7c700e11" + "0" * 24, "9d9d9d9d" + "0" * 24
OWN = C.worker_instance_for("alpha-fixture.net", W)
CORE_SECRET, OWN_SECRET = "core-secret", "own-secret"


def row(server, instance, gateway_url, agent, **extra):
    r = {"server": server, "instance": instance, "gateway_url": gateway_url,
         "token_ref": C.token_ref_for(instance), "primary": False, "added_by": "owner",
         "added_at": "2026-10-01T00:00:00Z", "verified": True, "server_source": "identity",
         "agent_mxid": agent}
    r.update(extra)
    return r


def rows(**own):
    return [row("alpha-fixture.net", "alpha", ALPHA, CORE_AGENT, primary=True),
            row("alpha-fixture.net", OWN, ALPHA, OWN_AGENT, worker=W, **own)]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="worker-mcp-credential-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ws = self.root / "workspace"
        (self.ws / "state").mkdir(parents=True)
        (self.root / "config" / "channels").mkdir(parents=True)
        VAULT.clear()
        VAULT[C.token_ref_for("alpha")] = ALPHA + "|" + CORE_SECRET
        VAULT[C.token_ref_for(OWN)] = ALPHA + "|" + OWN_SECRET
        self.record(rows())
        patcher = mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.root / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, rws):
        C.record_path(self.ws).write_text(json.dumps({"schema_version": C.SCHEMA_VERSION, "connections": rws}))

    def credential(self, worker, environ=None):
        return C.worker_mcp_credential(worker, workspace=self.ws, environ=environ or {})

    def refused(self, worker, **kw):
        with self.assertRaises(C.NoWorkerCredential) as cm:
            self.credential(worker, **kw)
        self.assertNotIn(CORE_SECRET, str(cm.exception))
        self.assertNotIn(OWN_SECRET, str(cm.exception))
        return cm.exception.reason


class TheResolver(Base):
    def test_the_workers_own_row_and_vault_token(self):
        cred = self.credential(W)
        self.assertEqual(cred, {"worker": W, "acts_as": "worker", "lane": OWN, "server": "alpha-fixture.net",
                                "agent_id": OWN_AGENT, "relay": ALPHA, "bearer": OWN_SECRET})
        self.assertNotEqual(cred["bearer"], CORE_SECRET)
        self.assertEqual(C.worker_enabled_identity(W, self.ws)["instance"], OWN)

    def test_a_bare_secret_in_the_vault_keeps_the_rows_gateway_as_its_relay(self):
        VAULT[C.token_ref_for(OWN)] = OWN_SECRET
        cred = self.credential(W)
        self.assertEqual((cred["relay"], cred["bearer"]), (ALPHA, OWN_SECRET))

    def test_the_cores_session_is_refused(self):
        self.assertEqual(self.refused(""), "no_worker")
        self.assertEqual(self.refused("Not A Worker"), "no_worker")

    def test_a_worker_with_no_identity_of_its_own_acts_as_the_core_with_no_secret(self):
        self.assertEqual(self.credential(OTHER_W), {"worker": OTHER_W, "acts_as": "core", "reason": "no_identity"})
        self.assertIsNone(C.worker_enabled_identity(OTHER_W, self.ws))

    def test_a_disabled_or_parked_identity_acts_as_the_core_too(self):
        for extra in ({"disabled": True}, {"parked": True}):
            with self.subTest(**extra):
                self.record(rows(**extra))
                self.assertEqual(self.credential(W), {"worker": W, "acts_as": "core", "reason": "disabled"})
                self.assertIsNone(C.worker_enabled_identity(W, self.ws))

    def test_a_missing_token_is_refused_and_never_answered_with_the_cores(self):
        del VAULT[C.token_ref_for(OWN)]
        self.assertEqual(self.refused(W), "no_token")
        VAULT[C.token_ref_for(OWN)] = "https://chat.beta-fixture.net/relay|" + OWN_SECRET
        self.assertEqual(self.refused(W), "no_token", "a token for another gateway is no token for this row")

    def test_an_unreadable_record_reads_as_no_identity(self):
        C.record_path(self.ws).write_text("{not json")
        self.assertEqual(self.credential(W)["acts_as"], "core")

    def test_the_argv_names_the_cli_in_the_repo_with_the_seats_interpreter(self):
        argv = C.mcp_worker_credential_argv(W, Path("/repo"), "/py/bin/python3")
        self.assertEqual(argv, ["/py/bin/python3", "/repo/scripts/connections.py", "worker-mcp-credential", "--worker", W])
        self.assertEqual(C.mcp_worker_credential_argv(W, Path("/repo"))[0], "python3")
        self.assertEqual(C.MCP_WORKER_CREDENTIAL_ENV, "AG2_MCP_WORKER_CREDENTIAL")


class TheCli(Base):
    """The subprocess the proxy runs: stdout carries the one JSON line, stderr the refusal, never a secret."""

    # The CLI's own argv parsing and printing, with the vault skill's module replaced before it loads.
    RUNNER = ("import json, os, runpy, sys, types\n"
              "m = types.ModuleType('vault_intercept')\n"
              "m.get_vault_key = lambda var: json.loads(os.environ['FAKE_VAULT'])[var]\n"
              "sys.modules['vault_intercept'] = m\n"
              "sys.argv = [sys.argv[1]] + sys.argv[2:]\n"
              "runpy.run_path(sys.argv[0], run_name='__main__')\n")

    def run_cli(self, *args, vault=None, environ=None):
        env = {**os.environ, "FAKE_VAULT": json.dumps(vault if vault is not None else VAULT),
               "CLAUDE_CONFIG_DIR": str(self.root / "config")}
        env.pop(C.SESSION_WORKER_ENV, None)
        env.update(environ or {})
        return subprocess.run([sys.executable, "-c", self.RUNNER, str(ROOT / "scripts" / "connections.py"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def cli(self, *args, vault=None, environ=None):
        return self.run_cli("--workspace", str(self.ws), "worker-mcp-credential", *args, vault=vault, environ=environ)

    def test_the_cli_prints_the_credential_once_to_stdout(self):
        r = self.cli("--worker", W)
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["bearer"], OWN_SECRET)
        self.assertEqual(json.loads(lines[0])["worker"], W)

    def test_the_seat_variable_names_the_worker_when_no_flag_does(self):
        r = self.cli(environ={C.SESSION_WORKER_ENV: W})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["lane"], OWN)

    def test_a_worker_with_no_identity_gets_acts_as_core_and_no_secret_on_stdout(self):
        r = self.cli("--worker", OTHER_W)
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stderr)
        self.assertEqual(json.loads(r.stdout), {"worker": OTHER_W, "acts_as": "core", "reason": "no_identity"})
        self.assertNotIn(CORE_SECRET, r.stdout)

    def test_a_refusal_is_exit_1_with_the_reason_and_no_secret_anywhere(self):
        r = self.cli()
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("refused, no_worker", r.stderr)
        self.assertNotIn(OWN_SECRET, r.stderr)
        r = self.cli("--worker", W, vault={C.token_ref_for("alpha"): ALPHA + "|" + CORE_SECRET})
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("refused, no_token", r.stderr)
        self.assertNotIn(CORE_SECRET, r.stderr + r.stdout, "the core's token is never the answer")

    def test_the_argv_command_prints_json_and_reads_no_vault(self):
        r = self.run_cli("worker-mcp-credential-argv", "--worker", W, "--repo", str(ROOT), "--python", "/py", vault={})
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stderr)
        self.assertEqual(json.loads(r.stdout), ["/py", str(ROOT / "scripts" / "connections.py"), "worker-mcp-credential",
                                                "--worker", W])


if __name__ == "__main__":
    unittest.main(verbosity=1)
