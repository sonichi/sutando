#!/usr/bin/env python3
"""scripts/connections.py through a subprocess, against a fake `security` binary.

The CLI is a dispatch layer: each verb calls one src/connections.py unit and prints.
The vault path is the real `vault_intercept`, pointed at a stub Keychain on PATH, so
the documented programmatic path is exercised without touching the host's Keychain.
Tokens name reserved `.example` hosts, so the preview probe fails (no such host) and the
rows are unverified, gateway-sourced.
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

TOKEN_A = "https://alpha-fixture.net/relay|secret-alpha"
TOKEN_B = "https://beta-fixture.net/relay|secret-beta"

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
            SUTANDO_BRIDGE_LOCK="0",  # the fake bridge scripts take no lease
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
        self.assertIn("server:    alpha-fixture.net  (unverified: from the gateway host)", proc.stdout)
        self.assertIn("gateway:   https://alpha-fixture.net/relay", proc.stdout)
        self.assertIn("not saved", proc.stdout)
        self.assert_no_secret(proc)
        self.assertEqual(self.run_cli("list").stdout.strip(), "no connections")

        proc = self.run_cli("add", TOKEN_A, stdin="y\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("added alpha-fixture-net for alpha-fixture.net (token in vault as AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET) — primary",
                      proc.stdout)
        self.assertEqual((self.keychain / "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET").read_text(), TOKEN_A)
        manifest = json.loads((self.ws / "state" / "secret-vault" / "keys.json").read_text())
        self.assertIn("AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET", manifest)

    def test_a_token_aimed_at_an_address_is_refused_before_any_probe(self):
        proc = self.run_cli("add", "http://127.0.0.1:9/relay|s", "--yes", "--expected-server", "127.0.0.1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid_gateway", proc.stderr)
        self.assertEqual(self.run_cli("list").stdout.strip(), "no connections")

    def test_yes_needs_the_expected_server_and_checks_it(self):
        proc = self.run_cli("add", TOKEN_A, "--yes")
        self.assertEqual(proc.returncode, 2)
        proc = self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "somewhere.else")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("alpha-fixture.net", proc.stderr)
        self.assert_no_secret(proc)
        self.assertEqual(sorted(self.keychain.iterdir()), [])
        proc = self.run_cli("add", "-", "--yes", "--expected-server", "alpha-fixture.net", "--added-by", "login",
                            stdin=TOKEN_A + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "alpha-fixture.net")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already present:", proc.stdout)

    def test_list_set_primary_remove(self):
        self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "alpha-fixture.net")
        self.run_cli("add", TOKEN_B, "--yes", "--expected-server", "beta-fixture.net")
        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("alpha-fixture-net", proc.stdout)
        self.assertIn("pending", proc.stdout)
        self.assert_no_secret(proc)
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([(r["instance"], r["primary"], r["state"], r["server_source"]) for r in rows],
                         [("alpha-fixture-net", True, "pending", "gateway"), ("beta-fixture-net", False, "pending", "gateway")])

        self.assertEqual(self.run_cli("primary").stdout.strip(), "alpha-fixture-net")
        proc = self.run_cli("set-primary", "beta-fixture-net")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([r["primary"] for r in rows], [False, True])
        self.assertEqual(self.run_cli("primary").stdout.strip(), "beta-fixture-net")

        proc = self.run_cli("remove", "beta-fixture-net")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("without force", proc.stderr)
        proc = self.run_cli("remove", "beta-fixture-net", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.keychain / "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET").exists())
        self.assertTrue((self.keychain / "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET").exists())
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([(r["instance"], r["primary"]) for r in rows], [("alpha-fixture-net", False)],
                         "no row inherits the primary flag")
        proc = self.run_cli("primary")
        self.assertEqual((proc.returncode, proc.stdout), (2, ""), "no primary: exit 2, nothing printed")

    def test_disconnect_keeps_the_token_and_known_servers_joins_the_rows(self):
        self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "alpha-fixture.net")
        self.run_cli("add", TOKEN_B, "--yes", "--expected-server", "beta-fixture.net")
        proc = self.run_cli("disconnect", "alpha-fixture-net")
        self.assertEqual(proc.returncode, 1, "the primary is refused, like remove without --force")
        self.assertIn("primary_cannot_disconnect", proc.stderr)
        proc = self.run_cli("disconnect", "beta-fixture-net")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("disconnected beta-fixture-net", proc.stdout)
        self.assertIn("reconnect to resume", proc.stdout)
        self.assert_no_secret(proc)
        self.assertTrue((self.keychain / "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET").exists(), "the token stays in the vault")
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([(r["instance"], r["disabled"], r["state"], r["reason"]) for r in rows],
                         [("alpha-fixture-net", False, "pending", None), ("beta-fixture-net", True, "parked", "disabled")])
        self.assertIn("parked (disabled)", self.run_cli("list").stdout)
        self.assertEqual(self.run_cli("launch-plan").stdout.count("\n"), 1, "the plan omits the disabled row")
        self.assertIn("alpha-fixture-net\t", self.run_cli("launch-plan").stdout)
        proc = self.run_cli("disconnect", "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no connection", proc.stderr)
        proc = self.run_cli("set-primary", "beta-fixture-net")
        self.assertEqual(proc.returncode, 1, "a disconnected row cannot become primary")
        self.assertIn("disabled_cannot_be_primary", proc.stderr)
        self.assertEqual(self.run_cli("primary").stdout.strip(), "alpha-fixture-net", "the primary is unchanged")
        self.run_cli("add", TOKEN_A.replace("alpha", "legacy"), "--yes", "--expected-server", "legacy-fixture.net",
                     "--instance", "legacy-fixture")
        proc = self.run_cli("add", TOKEN_A.replace("alpha", "reserved"), "--yes", "--expected-server", "reserved-fixture.net",
                            "--instance", "known-servers")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("reserved", proc.stderr)

        proc = self.run_cli("known-servers", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        servers = json.loads(proc.stdout)
        self.assertTrue(servers, "the resolver's registry is never empty")
        self.assertTrue(all(set(s) == {"server", "connected", "instance", "primary", "disabled"} for s in servers))
        self.assertTrue(all(s["connected"] is False for s in servers), "fixture servers are registered nowhere")
        self.assert_no_secret(proc)
        proc = self.run_cli("known-servers")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("SERVER"))

    def test_import_env_then_launch_plan(self):
        extra = {"REMOTE_TASK_TOKEN": TOKEN_A, "AG2_REMOTE_TOKEN_DEV": TOKEN_B}
        proc = self.run_cli("import-env", env_extra=extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ["imported (legacy) from REMOTE_TASK_TOKEN", "imported dev from AG2_REMOTE_TOKEN_DEV"])
        self.assert_no_secret(proc)
        self.assertEqual(self.run_cli("import-env", env_extra=extra).stdout.strip(), "nothing to import")

        proc = self.run_cli("launch-plan", env_extra={"AG2_REMOTE_TOKEN_QA": "https://qa-fixture.net/|q"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ["dev\tAG2_REMOTE_TOKEN_DEV\tdev-ag2space\t0", "qa\tAG2_REMOTE_TOKEN_QA\tqa-ag2space\t0"])
        self.assert_no_secret(proc)
        proc = self.run_cli("remove", "dev")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("import-env", env_extra=extra)
        self.assertIn("AG2_REMOTE_TOKEN_DEV: connection removed earlier; unset the variable", proc.stdout)
        self.assertNotIn("imported dev", proc.stdout)

    def test_a_corrupt_record_fails_the_plan_loudly_and_env_only_launch_still_works(self):
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        (self.ws / "state" / "connections.json").write_text('{"schema_version": 1, "connections": [{"tru')
        proc = self.run_cli("launch-plan")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid JSON", proc.stderr)
        self.assertEqual(proc.stdout, "")
        box = Path(self.tmp.name)
        repo = box / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "remote-gateway-bridge.py").write_text(
            "import os, pathlib\npathlib.Path(os.environ['FAKE_OUT']).write_text(os.environ['GATEWAY_INSTANCE'])\n")
        out = box / "env-only.txt"
        proc = self.run_cli("launch", "dev", "--repo", str(repo), "--logs-dir", str(box / "logs"), "--env-only",
                            env_extra={"AG2_REMOTE_TOKEN_DEV": TOKEN_B, "FAKE_OUT": str(out)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for _ in range(50):
            if out.exists():
                break
            import time
            time.sleep(0.1)
        self.assertEqual(out.read_text(), "dev")

    def test_launch_and_reconnect_run_the_bridge_entry_point(self):
        box = Path(self.tmp.name)
        repo = box / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "remote-gateway-bridge.py").write_text(
            "import os, pathlib\n"
            "pathlib.Path(os.environ['FAKE_OUT']).write_text(os.environ['GATEWAY_INSTANCE'] + ' ' "
            "+ os.environ['REMOTE_TASK_CHANNEL_DIR'] + ' ' + repr(os.environ['REMOTE_PROACTIVE_ROOM']))\n")
        out = box / "launched.txt"
        self.run_cli("add", TOKEN_B, "--yes", "--expected-server", "beta-fixture.net")
        proc = self.run_cli("launch", "beta-fixture-net", "--repo", str(repo), "--logs-dir", str(box / "logs"),
                            env_extra={"FAKE_OUT": str(out), "REMOTE_PROACTIVE_ROOM": "!room:x"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("launched beta-fixture-net (pid ", proc.stdout)
        for _ in range(50):
            if out.exists():
                break
            import time
            time.sleep(0.1)
        self.assertEqual(out.read_text(), "beta-fixture-net beta-fixture-net-ag2space '!room:x'", "the primary keeps the room")
        record = json.loads((self.ws / "state" / "connections-launch" / "beta-fixture-net.json").read_text())
        self.assertTrue(record["ok"])
        proc = self.run_cli("reconnect", "beta-fixture-net", "--repo", str(repo), "--logs-dir", str(box / "logs"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reconnected beta-fixture-net", proc.stdout)
        proc = self.run_cli("launch", "nope", "--repo", str(repo), "--logs-dir", str(box / "logs"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no connection", proc.stderr)

        launch_args = ("--repo", str(repo), "--logs-dir", str(box / "logs"))
        self.run_cli("disconnect", "beta-fixture-net")
        proc = self.run_cli("update-token", "beta-fixture-net", TOKEN_A, *launch_args)
        self.assertEqual(proc.returncode, 1, "a code for another server is refused")
        self.assertIn("token_server_mismatch", proc.stderr)
        self.assert_no_secret(proc)
        self.assertEqual((self.keychain / "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET").read_text(), TOKEN_B, "the vault is untouched")
        rotated = "https://beta-fixture.net/relay|secret-beta-rotated"
        proc = self.run_cli("update-token", "beta-fixture-net", "-", *launch_args, stdin=rotated + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("updated token of beta-fixture-net (beta-fixture.net; vault key AG2_REMOTE_TOKEN_BETA_FIXTURE_NET replaced; "
                      "relaunched, pid ", proc.stdout)
        self.assert_no_secret(proc)
        self.assertEqual((self.keychain / "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET").read_text(), rotated)
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual([(r["instance"], r["disabled"], r["token_ref"]) for r in rows],
                         [("beta-fixture-net", False, "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET")], "re-enabled, same key")
        proc = self.run_cli("update-token", "nope", rotated, *launch_args)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no connection", proc.stderr)

    def test_vault_failure_is_named_and_writes_no_row(self):
        (self.keychain / "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET").mkdir()  # a directory makes the stub's write fail
        proc = self.run_cli("add", TOKEN_A, "--yes", "--expected-server", "alpha-fixture.net")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("secret vault", proc.stderr)
        self.assert_no_secret(proc)
        self.assertEqual(self.run_cli("list").stdout.strip(), "no connections")


class InProcess(unittest.TestCase):
    """main() driven directly, with the domain module faked, for the branches a subprocess cannot show."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("connections_cli", CLI)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def run_main(self, argv, **fakes):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        saved = {k: getattr(self.cli.connections, k) for k in fakes}
        for k, v in fakes.items():
            setattr(self.cli.connections, k, v)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = self.cli.main(argv)
        finally:
            for k, v in saved.items():
                setattr(self.cli.connections, k, v)
        return rc, out.getvalue(), err.getvalue()

    def test_domain_errors_exit_one_with_the_message(self):
        def boom(*a, **k):
            raise self.cli.connections.UnknownConnection("no connection 'x'")
        rc, out, err = self.run_main(["remove", "x"], remove_connection=boom)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("connections: no connection 'x'", err)

    def test_yes_without_expected_server_exits_two(self):
        rc, out, err = self.run_main(["add", TOKEN_A, "--yes"], preview_connection=lambda t: {"server": "s"})
        self.assertEqual(rc, 2)
        self.assertIn("--expected-server", err)

    def test_list_and_plan_print_rows_without_secrets(self):
        row = {"instance": "dev", "server": "s.space", "primary": True, "added_by": "ui", "token_ref": "AG2_REMOTE_TOKEN_DEV",
               "verified": True, "server_source": "identity"}
        rc, out, _ = self.run_main(["list"], list_connections=lambda ws: [row],
                                   status_for=lambda r, ws: dict(r, status={"last_ok_ts": 5}, state="connected", reason=None))
        self.assertEqual(rc, 0)
        self.assertIn("connected", out)
        self.assertIn("AG2_REMOTE_TOKEN_DEV", out)
        rc, out, _ = self.run_main(["launch-plan"], launch_plan=lambda env, workspace=None: [
            {"instance": "dev", "token_ref": "AG2_REMOTE_TOKEN_DEV", "channel_dir": "dev-ag2space", "primary": True}])
        self.assertEqual(out, "dev\tAG2_REMOTE_TOKEN_DEV\tdev-ag2space\t1\n")

    def test_disconnect_and_known_servers_print_the_unit_result(self):
        rc, out, _ = self.run_main(["disconnect", "dev"], disconnect=lambda inst, workspace=None: {
            "instance": inst, "token_ref": "AG2_REMOTE_TOKEN_DEV", "disabled": True, "stopped_pid": 31337})
        self.assertEqual((rc, out), (0, "disconnected dev (stopped pid 31337; token kept in vault as AG2_REMOTE_TOKEN_DEV; "
                                        "reconnect to resume)\n"))
        servers = [{"server": "s.space", "connected": True, "instance": "s-space", "primary": True, "disabled": False},
                   {"server": "dev.s.space", "connected": False, "instance": None, "primary": False, "disabled": False}]
        rc, out, _ = self.run_main(["known-servers"], known_servers=lambda ws: servers)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0].split(), ["SERVER", "CONNECTED", "INSTANCE", "PRIMARY", "DISABLED"])
        self.assertEqual(out.splitlines()[1].split(), ["s.space", "yes", "s-space", "yes", "no"])
        self.assertEqual(out.splitlines()[2].split(), ["dev.s.space", "no", "-", "no", "no"])
        rc, out, _ = self.run_main(["known-servers", "--json"], known_servers=lambda ws: servers)
        self.assertEqual(json.loads(out), servers)
        rc, out, _ = self.run_main(["known-servers"], known_servers=lambda ws: [])
        self.assertEqual(out, "no known servers\n")

    def test_update_token_prints_the_unit_result_and_exits_one_on_a_mismatch(self):
        def fake(launch):
            return lambda inst, token, repo, logs_dir, workspace=None: {
                "instance": inst, "server": "s.space", "token_ref": "AG2_REMOTE_TOKEN_DEV", "launch": launch}
        args = ["update-token", "dev", "https://chat.s.space|new", "--repo", "/r", "--logs-dir", "/l"]
        rc, out, _ = self.run_main(args, update_token=fake({"ok": True, "pid": 7}))
        self.assertEqual((rc, out), (0, "updated token of dev (s.space; vault key AG2_REMOTE_TOKEN_DEV replaced; relaunched, pid 7)\n"))
        rc, out, _ = self.run_main(args, update_token=fake({"ok": False, "reason": "launch_failed"}))
        self.assertEqual((rc, out), (0, "updated token of dev (s.space; vault key AG2_REMOTE_TOKEN_DEV replaced; "
                                        "relaunch failed: launch_failed)\n"))
        with self.assertRaises(SystemExit) as cm:
            self.run_main(["update-token", "dev", "--repo", "/r", "--logs-dir", "/l"], update_token=fake(None))
        self.assertEqual(cm.exception.code, 2, "the token is a required argument, as for add")

        def boom(*a, **k):
            raise self.cli.connections.TokenAgentMismatch("dev", "the code is for agent @b:s.space, not @a:s.space")
        rc, out, err = self.run_main(args, update_token=boom)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("connections: token_agent_mismatch: ", err)

        def unknown(*a, **k):
            raise self.cli.connections.TokenStateUnknown("token_state_unknown: the update of dev failed after the vault write began (OSError: disk full) and reading AG2_REMOTE_TOKEN_DEV back failed, so it is not known which secret it holds")
        rc, out, err = self.run_main(args, update_token=unknown)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("connections: token_state_unknown: ", err)
        self.assertNotIn("relaunched", err)

    def test_update_token_help_documents_stdin_as_the_form_that_hides_the_code(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(["update-token", "--help"])
        self.assertEqual(cm.exception.code, 0)
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            self.cli.main(["update-token", "--help"])
        self.assertIn("TOKEN|-", out.getvalue())
        self.assertIn("stdin", out.getvalue())
        self.assertIn("process list", out.getvalue())
        self.assertIn("update-token INSTANCE - --repo", self.cli.__doc__, "the verb table shows the stdin form")

    def test_set_primary_prints_the_relaunch_hint_and_exits_one_on_a_disabled_row(self):
        rc, out, _ = self.run_main(["set-primary", "dev"], set_primary=lambda inst, workspace=None: {
            "instance": inst, "server": "s.space", "primary": True, "relaunch_needed": True})
        self.assertEqual((rc, out), (0, "primary is now dev (s.space); its running bridge carries the proactive room "
                                        "after you reconnect it\n"))
        rc, out, _ = self.run_main(["set-primary", "dev"], set_primary=lambda inst, workspace=None: {
            "instance": inst, "server": "s.space", "primary": True, "relaunch_needed": False})
        self.assertEqual((rc, out), (0, "primary is now dev (s.space)\n"))

        def refused(inst, workspace=None):
            raise self.cli.connections.DisabledCannotBePrimary(inst)
        rc, out, err = self.run_main(["set-primary", "dev"], set_primary=refused)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("connections: disabled_cannot_be_primary: dev", err)

    def test_promote_unpark_and_legacy_parked_print_and_exit_by_the_module(self):
        argv = ["--repo", "/r", "--logs-dir", "/l"]
        moved = {"instance": "s-space", "server": "s.space", "changed": True, "previous": "", "stopped_pid": 41}
        rc, out, _ = self.run_main(["promote", "s-space", *argv], promote=lambda inst, **kw: dict(moved))
        self.assertEqual((rc, out), (0, "primary is now s-space (s.space); parked (legacy) (stopped pid 41)\n"))
        rc, out, _ = self.run_main(["promote", "s-space", *argv], promote=lambda inst, **kw: dict(moved, changed=False))
        self.assertEqual((rc, out), (0, "s-space is already primary\n"))

        def refused(inst, **kw):
            raise self.cli.connections.BridgeStillRunning("", 41)
        rc, _, err = self.run_main(["promote", "s-space", *argv], promote=refused)
        self.assertEqual(rc, 1)
        self.assertIn("connections: bridge_still_running", err)
        rc, out, _ = self.run_main(["unpark", "", *argv], unpark=lambda inst, **kw: {"instance": "", "launch": None})
        self.assertEqual((rc, out), (0, "unparked (legacy) (its launchers resume it)\n"))
        rc, out, _ = self.run_main(["unpark", "dev", *argv], unpark=lambda inst, **kw: {
            "instance": "dev", "launch": {"ok": False, "reason": "no_token"}})
        self.assertEqual((rc, out), (1, "unparked dev (start failed: no_token)\n"))
        self.assertEqual(self.run_main(["legacy-parked"], legacy_parked=lambda ws=None: True)[0], 0)
        self.assertEqual(self.run_main(["legacy-parked"], legacy_parked=lambda ws=None: False)[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
