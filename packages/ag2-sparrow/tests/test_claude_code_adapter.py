#!/usr/bin/env python3
"""The claude-code adapter ships a well-formed Claude Code mod whose embedded
Python mirrors Sparrow's rules, its helper scripts write what Sparrow's parser
reads, and nothing in it names a host.

The mod's own `*.test.ts` needs the `claude` binary; this file is the part CI
can run with stdlib Python alone."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import plistlib
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
from ag2_sparrow.local_task_protocol import parse_task_headers  # noqa: E402
from ag2_sparrow.team_result_guard import resolve_access_tier  # noqa: E402

from ag2_sparrow import task_io  # noqa: E402

ADAPTER = PKG / "adapters" / "claude-code"
MOD = ADAPTER / "ag2space-inbox"
REGISTER = MOD / "hooks" / "register.ts"
SRC = PKG.parents[1] / "src"
HOST_PATH_RE = re.compile(r"/Users/|/home/[a-z]|/private/tmp|~/\.claude|~/\.sutando")
STAMP = "envelope_hmac: v1:"


def verify_py(source: str = "") -> str:
    """The verifier as the mod runs it: the template literal, JS escapes undone."""
    m = re.search(r"const VERIFY_PY = `\n(.*?)`", source or REGISTER.read_text(), re.S)
    return m.group(1).replace("\\\\n", "\\n")


def run_verifier(script: str, text: str, key_file: str) -> tuple[str, str, str]:
    out = subprocess.run([sys.executable, "-c", script], input=text, capture_output=True, text=True,
                         env=hermetic_env(**{"AG2_HMAC_KEY_FILE": key_file}), check=True).stdout.split()
    return (out[0], out[1], out[2])


def stamp(text: str, key_hex: str) -> str:
    mac = hmac.new(bytes.fromhex(key_hex), text.encode(), hashlib.sha256).hexdigest()
    lines = text.split("\n")
    lines.insert(1 if lines[0].startswith("id:") else 0, STAMP + mac)
    return "\n".join(lines)


class PluginShape(unittest.TestCase):
    def test_manifest_and_hooks_point_at_the_module(self):
        manifest = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "ag2space-inbox")
        for key in ("version", "description"):
            self.assertTrue(manifest.get(key), key)
        hooks = json.loads((MOD / "hooks" / "hooks.json").read_text())
        self.assertEqual(hooks["modules"], ["./register.ts"])
        self.assertTrue(REGISTER.is_file())
        src = REGISTER.read_text()
        for name in ("ag2space_inbox_watch", "ag2space_reply", STAMP):
            self.assertIn(name, src)

    def test_no_hardcoded_host_paths_in_added_files(self):
        for path in sorted(p for p in ADAPTER.rglob("*") if p.is_file()):
            self.assertIsNone(HOST_PATH_RE.search(path.read_text(errors="replace")), path)

    def test_mod_tests_exist_for_the_claude_harness(self):
        self.assertTrue(list((MOD / "tests").glob("*.test.ts")))

    def test_pinned_non_owner_note_is_the_shared_owners(self):
        mod = re.search(r"^const NON_OWNER_NOTE = `(.*)`$", REGISTER.read_text(), re.M).group(1)
        self.assertEqual(mod, task_io.NON_OWNER_NOTE)
        self.assertNotIn("adapters/hatch", REGISTER.read_text())


class VerifierMirrorsSparrow(unittest.TestCase):
    """VERIFY_PY must answer exactly what src/task_envelope.verify_text and
    team_result_guard.resolve_access_tier answer, vector by vector."""

    def setUp(self):
        if not (SRC / "task_envelope.py").is_file():
            self.skipTest("src/task_envelope.py not beside the package (standalone checkout)")
        sys.path.insert(0, str(SRC))
        import task_envelope
        self.te = task_envelope
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.key = secrets.token_hex(32)
        self.ws.joinpath("state", "auth").mkdir(parents=True)
        self.key_file = self.ws / "state" / "auth" / "task-hmac.key"
        self.key_file.write_text(self.key)

    def vectors(self):
        base = "id: task-v1\nsource: ag2space\naccess_tier: owner\ntask: hi\n"
        signed = stamp(base, self.key)
        flipped = signed.replace("access_tier: owner", "access_tier: guest")
        crlf = stamp(base.replace("\n", "\r\n"), self.key)
        no_id = stamp("source: ag2space\naccess_tier: team\ntask: hi\n", self.key)
        # Tier shapes, in order: post-task only, pre-task wins over post-task, pre-task
        # conflict, the legacy `other` alias, an unknown tier, no tier at all, collaborator.
        tiers = [
            "id: t\ntask: hi\naccess_tier: team\n",
            "id: t\naccess_tier: owner\ntask: hi\naccess_tier: guest\n",
            "id: t\naccess_tier: owner\naccess_tier: guest\ntask: hi\n",
            "id: t\naccess_tier: other\ntask: hi\n",
            "id: t\naccess_tier: admin\ntask: hi\n",
            "id: t\ntask: hi\n",
            "id: t\ncollaborator: true\naccess_tier: team\ntask: hi\n",
        ]
        return [signed, flipped, base, crlf, no_id] + [stamp(t, self.key) for t in tiers]

    def check(self, script: str, key_file: str, workspace: Path) -> list[str]:
        """Every vector where the mod's verifier and Sparrow's own readers disagree."""
        diffs = []
        for i, text in enumerate(self.vectors()):
            verdict, tier, collab = run_verifier(script, text, key_file)
            want_verdict = self.te.verify_text(text, workspace)["verdict"]
            task_file = workspace / f"vec{i}.txt"; task_file.write_text(text)
            want_tier = resolve_access_tier(task_file)
            head = text.split("\ntask:", 1)[0].split("\n")
            want_collab = str("collaborator: true" in head).lower()
            if (verdict, tier, collab) != (want_verdict, want_tier, want_collab):
                diffs.append(f"vector {i}: mod {verdict} {tier} {collab} != sparrow {want_verdict} {want_tier} {want_collab}")
        return diffs

    def test_verdicts_and_tiers_agree_with_a_good_key(self):
        self.assertEqual(self.check(verify_py(), str(self.key_file), self.ws), [])

    def test_verdicts_agree_without_a_key_and_with_malformed_keys(self):
        empty = Path(self.tmp.name) / "nokey"; empty.mkdir()
        self.assertEqual(self.check(verify_py(), str(empty / "missing"), empty), [])
        spaced = " ".join(self.key[i:i + 2] for i in range(0, 64, 2))
        for bad in (spaced, self.key[:62], "zz" * 32, ""):
            ws = Path(tempfile.mkdtemp(dir=self.tmp.name))
            ws.joinpath("state", "auth").mkdir(parents=True)
            kf = ws / "state" / "auth" / "task-hmac.key"; kf.write_text(bad)
            self.assertEqual(self.check(verify_py(), str(kf), ws), [], repr(bad[:8]))
            self.assertEqual(run_verifier(verify_py(), self.vectors()[0], str(kf))[0], "unverifiable", repr(bad[:8]))

    def test_mutants_are_caught(self):
        always_verified = verify_py().replace('out("verified" if', 'out("verified" if True or')
        self.assertIn('if True or', always_verified)
        self.assertTrue(self.check(always_verified, str(self.key_file), self.ws))
        owner_only = verify_py().replace('def out(v): print(v,tier,collab)', 'def out(v): print(v,"owner",collab)')
        self.assertIn('"owner",collab', owner_only)
        self.assertTrue(self.check(owner_only, str(self.key_file), self.ws))


class DropTask(unittest.TestCase):
    def test_dropper_writes_a_task_last_file_sparrow_parses_and_the_mod_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"; inbox.mkdir()
            key = Path(tmp) / "task-hmac.key"; key.write_text(secrets.token_hex(32))
            out = subprocess.run(["bash", str(ADAPTER / "drop-task.sh"), str(inbox), str(key), "t1", "What is 2+2?"],
                                 capture_output=True, text=True, check=True)
            dest = inbox / "task-t1.txt"
            self.assertEqual(out.stdout.strip(), str(dest))
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), ["task-t1.txt"])  # no temp left
            text = dest.read_text()
            parsed = parse_task_headers(text)
            self.assertEqual(parsed.get("id"), "task-t1")
            self.assertEqual(parsed.get("source"), "ag2space")
            self.assertEqual(parsed.get("access_tier"), "owner")
            self.assertEqual(parsed.body.rstrip("\n"), "What is 2+2?")
            lines = text.rstrip("\n").split("\n")
            self.assertTrue(lines[-1].startswith("task: "), "task: is the last header")
            self.assertTrue(lines[0].startswith("id: ") and lines[1].startswith(STAMP))
            self.assertEqual(run_verifier(verify_py(), text, str(key)), ("verified", "owner", "false"))
            flipped = text.replace("access_tier: owner", "access_tier: guest")
            self.assertEqual(run_verifier(verify_py(), flipped, str(key))[0], "invalid")


class Measure(unittest.TestCase):
    def test_measure_reports_the_mtime_delta_in_ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task-t1.txt"; task.write_text("id: task-t1\ntask: x\n")
            result = Path(tmp) / "result.txt"; result.write_text("4\n")
            base = 1_700_000_000_000_000_000
            os.utime(task, ns=(base, base)); os.utime(result, ns=(base, base + 1_500_000_000))
            out = subprocess.run(["bash", str(ADAPTER / "measure.sh"), str(task), str(result), "2"],
                                 capture_output=True, text=True, check=True)
            self.assertIn("latency_ms=1500", out.stdout)
            self.assertIn("result body: 4", out.stdout)

    def test_measure_times_out_without_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task-t1.txt"; task.write_text("task: x\n")
            out = subprocess.run(["bash", str(ADAPTER / "measure.sh"), str(task), str(Path(tmp) / "none.txt"), "1"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertIn("TIMEOUT", out.stdout)




# ── `ag2-sparrow-claude-local claude-local`: the installed local shape ─────────────

from ag2_sparrow import _dirs, holder_lock  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli  # noqa: E402

FAKE_CLAUDE = r'''#!%(py)s
import json, os, sys
home = os.environ["HOME"]
def read(name, default):
    try:
        return open(os.path.join(home, name)).read().strip()
    except OSError:
        return default
if sys.argv[1:] == ["--version"]:
    print(read("claude-version-banner", "") or read("claude-version", "2.1.296") + " (Claude Code)")
    sys.exit(int(read("claude-version-rc", "0")))
if sys.argv[1:3] == ["auth", "status"]:
    print(read("claude-auth", '{"loggedIn": true}')); sys.exit(0)
json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()},
          open(os.path.join(home, "claude-session.json"), "w"))
import time
time.sleep(float(read("claude-linger", "0")))
sys.exit(int(read("claude-exit", "0")))
'''
TOKEN = "tok-FAKE-RELAY-TOKEN-never-copied"


class ConnectBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()   # macOS: /var is /private/var once resolved
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.claude = self.bin / "claude"
        self.claude.write_text(FAKE_CLAUDE % {"py": sys.executable})
        self.claude.chmod(0o755)
        self.inbox, self.results = self.home / "tasks", self.home / "results"
        self.plugin = self.home / "plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "ag2space-inbox"}')
        relay = self.home / ".config" / "ag2-sparrow" / "relay.env"
        relay.parent.mkdir(parents=True)
        relay.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\n")
        relay.chmod(0o600)
        self.relay = relay
        env = {"HOME": str(self.home), "USER": "owner", "PATH": f"{self.bin}:/usr/bin:/bin",
               "SUTANDO_WORKSPACE_DIR": "/secret/ws", "REMOTE_TASK_TOKEN": TOKEN,
               "ANTHROPIC_API_KEY": "sk-ant-should-not-pass", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-should-not-pass",
               "AWS_SECRET_ACCESS_KEY": "aws-should-not-pass"}
        for key in ("AG2_DEVICE_ENV", "AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR", "CLAUDE_CONFIG_DIR",
                    "ANTHROPIC_BASE_URL", "AG2_AUTOARM"):
            env[key] = ""
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in ("AG2_DEVICE_ENV", "AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR", "CLAUDE_CONFIG_DIR",
                    "ANTHROPIC_BASE_URL", "AG2_AUTOARM"):
            os.environ.pop(key, None)
        # No test reaches the host's systemctl, loginctl or launchctl.
        self.calls = []
        self.patches = [mock.patch.object(cli, "_run", side_effect=lambda cmd: self.calls.append(cmd)),
                        mock.patch.object(cli, "is_macos", return_value=False),
                        mock.patch.object(cli, "console_script", return_value=str(self.bin / cli.COMMAND))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox),
                         "--results", str(self.results), "--claude-config-dir", str(self.home / ".claude-sutando"),
                         "--hmac-key-file", str(self.home / "task-hmac.key"), *extra)

    def session(self):
        p = self.home / "claude-session.json"
        return json.loads(p.read_text()) if p.exists() else None


class ConnectRender(ConnectBase):
    def test_unit_and_plist_carry_the_marker_and_paths_only(self):
        exec_start = str(self.bin / cli.COMMAND)
        unit = cli.render_service(exec_start)
        self.assertEqual(unit.split("\n")[0], cli.MARKER)
        self.assertIn(f"ExecStart={exec_start} run\n", unit)
        for line in ("Restart=always", "RestartSec=10", "RestartPreventExitStatus=75", "WantedBy=default.target"):
            self.assertIn(line + "\n", unit)
        plist = cli.render_plist(exec_start, "/opt/bin:/usr/bin", "/var/log/x.log")
        self.assertTrue(cli.marked(plist, cli.PLIST_MARKER), plist[:200])
        self.assertTrue(plist.startswith("<?xml"), "the declaration stays first")
        body = plistlib.loads(plist.encode())
        self.assertEqual(body["ProgramArguments"], [exec_start, "run"])
        self.assertEqual((body["Label"], body["KeepAlive"], body["RunAtLoad"]), (cli.AGENT_LABEL, True, True))
        self.assertEqual(body["EnvironmentVariables"], {"PATH": "/opt/bin:/usr/bin"})
        for text in (unit, plist, cli.render_config({"CLAUDE": "/x/claude", "INBOX_DIR": "/i"})):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("REMOTE_TASK_TOKEN", text)
        self.assertNotIn("Environment=", unit, "the unit carries paths in ExecStart, no values")
        self.assertTrue(cli.marked("<?xml?>\n" + cli.MARKER + "\nx", cli.MARKER), "a marker on the second line counts")
        self.assertFalse(cli.marked("a\nb\n" + cli.MARKER, cli.MARKER), "the marker is a leading line")


class ConnectInstall(ConnectBase):
    def test_install_writes_the_config_and_the_unit_and_starts_it(self):
        rc, out, err = self.install()
        self.assertEqual(rc, 0, err)
        cfg_path = cli.config_path()
        self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
        cfg = cli.read_config(cfg_path)
        self.assertEqual((cfg["CLAUDE"], cfg["PLUGIN_DIR"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"]),
                         (str(self.claude), str(self.plugin), str(self.inbox), str(self.results)))
        self.assertEqual((cfg["PERMISSION_MODE"], cfg["HMAC_STRICT"]), ("bypassPermissions", ""))
        self.assertTrue(self.inbox.is_dir() and self.results.is_dir())
        unit = cli.service_path().read_text()
        self.assertTrue(unit.startswith(cli.MARKER))
        self.assertNotIn(TOKEN, unit + cfg_path.read_text())
        self.assertIn("NOTE supervisor: systemctl --user is not available", out)
        self.assertIsNone(self.session(), "install starts no session itself")

    def test_install_on_macos_writes_a_launchd_agent(self):
        with mock.patch.object(cli, "is_macos", return_value=True):
            rc, out, err = self.install("--permission-mode", "default")
        self.assertEqual(rc, 0, err)
        plist = cli.plist_path().read_text()
        self.assertTrue(cli.marked(plist, cli.PLIST_MARKER))
        body = plistlib.loads(plist.encode())
        self.assertEqual(body["ProgramArguments"][1:], ["run"])
        self.assertNotIn(TOKEN, plist)
        self.assertEqual(cli.read_config(cli.config_path())["PERMISSION_MODE"], "default")
        self.assertEqual([c[:2] for c in self.calls], [["launchctl", "bootstrap"]])
        self.assertIn("launchctl bootstrap failed", out)
        self.assertFalse(cli.service_path().exists())

    def test_install_refuses_an_old_cli_before_writing_anything(self):
        (self.home / "claude-version").write_text("2.1.200")
        rc, out, err = self.install()
        self.assertEqual(rc, 2)
        self.assertIn("claude 2.1.200 is older than the 2.1.287 this adapter needs", err)
        self.assertFalse(cli.config_path().exists())
        self.assertFalse(cli.service_path().exists())
        self.assertIsNone(self.session())

    def test_install_keeps_a_supervisor_it_did_not_write(self):
        cli.service_path().parent.mkdir(parents=True)
        cli.service_path().write_text("[Unit]\nDescription=mine\n")
        rc, out, _ = self.install()
        self.assertEqual(rc, 0)
        self.assertEqual(cli.service_path().read_text(), "[Unit]\nDescription=mine\n")
        self.assertIn("was not written by install, so it was kept", out)
        rc, out, _ = self.install("--force")
        self.assertTrue(cli.service_path().read_text().startswith(cli.MARKER))

    def test_the_version_pin(self):
        self.assertEqual(cli.parse_version("2.1.296 (Claude Code)"), (2, 1, 296))
        self.assertIsNone(cli.parse_version("nope"))
        self.assertEqual(cli.parse_version("Update available: 2.1.310\n2.1.200 (Claude Code)\n"), (2, 1, 200),
                         "the CLI's own line, not an update notice")
        self.assertIsNone(cli.parse_version("2.1.310\n"), "no (Claude Code) line, no version")
        (self.home / "claude-version-banner").write_text("Update available: 2.1.310\n2.1.200 (Claude Code)")
        self.assertIn("claude 2.1.200 is older", cli.version_problem(str(self.claude)))
        (self.home / "claude-version-banner").unlink()
        (self.home / "claude-version-rc").write_text("1")
        self.assertIn("did not answer", cli.version_problem(str(self.claude)), "a failing --version is no version")
        (self.home / "claude-version-rc").unlink()
        self.assertEqual(cli.version_problem(str(self.claude)), "")
        (self.home / "claude-version").write_text("2.1.286")
        self.assertIn("older than the 2.1.287", cli.version_problem(str(self.claude)))
        (self.home / "claude-version").write_text("3.0.0")
        self.assertEqual(cli.version_problem(str(self.claude)), "")
        self.assertIn("did not answer", cli.version_problem(str(self.home / "no-such-claude")))


class ConnectRun(ConnectBase):
    def test_run_starts_the_session_with_the_isolated_env_and_the_autoarm_marker(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        rc, _, err = self.main("run")
        self.assertEqual(rc, 0, err)
        rec = self.session()
        self.assertEqual(rec["argv"][1:], ["--plugin-dir", str(self.plugin), "--permission-mode", "bypassPermissions",
                                           "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                                           "--verbose"])
        # macOS stamps __CF_USER_TEXT_ENCODING on every child; the fake's own Python coerces LC_CTYPE.
        env = {k: v for k, v in rec["env"].items() if k not in ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")}
        self.assertEqual(env["AG2_AUTOARM"], "1", "the plugin arms itself from this marker at session.start")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home / ".claude-sutando"))
        self.assertEqual((env["AG2_INBOX_DIR"], env["AG2_RESULTS_DIR"], env["AG2_HMAC_KEY_FILE"]),
                         (str(self.inbox), str(self.results), str(self.home / "task-hmac.key")))
        self.assertEqual(env["USER"], "owner", "macOS needs USER for the Keychain login")
        self.assertTrue(set(env) <= {"PATH", "HOME", "USER", "CLAUDE_CONFIG_DIR", "AG2_INBOX_DIR", "AG2_RESULTS_DIR",
                                     "AG2_HMAC_KEY_FILE", "AG2_AUTOARM"}, sorted(env))
        for secret in ("REMOTE_TASK_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_SECRET_ACCESS_KEY",
                       "SUTANDO_WORKSPACE_DIR", "AG2_DEVICE_ENV"):
            self.assertNotIn(secret, env)
        self.assertEqual(rec["cwd"], str(self.inbox))
        self.assertIsNone(getattr(holder_lock.read_holder(str(self.inbox.parent)), "record", None), "released after the child")

    def test_run_refuses_an_old_cli_and_starts_no_session(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        (self.home / "claude-version").write_text("2.0.0")
        rc, _, err = self.main("run")
        self.assertEqual(rc, 2)
        self.assertIn("claude 2.0.0 is older than the 2.1.287", err)
        self.assertIsNone(self.session())

    def test_a_second_launcher_exits_75_and_starts_no_session(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        # holder_lock allows one acquisition per process, so the first launcher is another process.
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
                                   "o = task_io.acquire_lock(%r, 'first-launcher'); print('held', flush=True); sys.stdin.read()\n"
                                   "task_io.release_lock(o)" % (str(PKG), str(self.inbox.parent))],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            rc, _, err = self.main("run")
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
        self.assertEqual(rc, 75, err)
        self.assertIn("refusing to start", err)
        self.assertIsNone(self.session())

    def test_the_lock_is_held_while_the_child_runs(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        from ag2_sparrow import task_io
        seen, work = {}, str(self.inbox.parent)

        class Child:
            def __init__(self, argv, **kw):
                self.stdin = io.StringIO()
                seen["argv"], seen["env"], seen["pass_fds"] = argv, kw["env"], kw["pass_fds"]

            def poll(self):
                return None

            def wait(self, timeout=None):
                seen["during_wait"] = task_io.lock_holder_pid(work)     # sampled while the child "runs"
                seen["stdin"] = self.stdin.getvalue()
                return 0
        args = type("A", (), {})()
        rc = cli.run(args, popen=lambda argv, **kw: Child(argv, **kw))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["during_wait"], os.getpid(), "held by this launcher while the child runs, not only at spawn")
        self.assertEqual(len(seen["pass_fds"]), 1, "the lock descriptor goes to the child")
        self.assertEqual(seen["stdin"], "", "nothing is written to stdin: the plugin arms itself")
        self.assertEqual(seen["env"]["AG2_AUTOARM"], "1")
        self.assertEqual(seen["argv"][0], str(self.claude))
        self.assertIsNone(task_io.lock_holder_pid(work), "released afterwards")

    def test_the_childs_exit_code_is_the_launchers(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        (self.home / "claude-exit").write_text("3")
        self.assertEqual(self.main("run")[0], 3)


class ConnectMod(unittest.TestCase):
    def test_the_mod_auto_arms_only_on_the_launchers_marker(self):
        src = REGISTER.read_text()
        start = src.index("on('session.start'")
        block = src[start:src.index("on('tool.call'")]
        self.assertIn("await $.env.get('AG2_AUTOARM')) === '1'", block, "arming at session.start is gated on the marker")
        self.assertIn("await arm($, s, String((await $.env.get('AG2_INBOX_DIR'))", block)
        self.assertEqual(src.count("async function arm("), 1, "one top-level arm path")
        self.assertEqual(src.count("arm($, s,"), 2, "shared by session.start and the ag2space_inbox_watch tool")
        self.assertEqual(cli.AUTOARM_VAR, "AG2_AUTOARM")
        harness = (MOD / "tests" / "ag2space-inbox.test.ts").read_text()
        self.assertIn("AG2_AUTOARM: '1'", harness)
        self.assertIn("without the marker nothing is armed", harness)


class Runner:
    """A supervisor command table for cli._run: (program, verb) -> exit code; None = could not run."""

    def __init__(self, table=None, default=0):
        self.table, self.default, self.calls = dict(table or {}), default, []

    def verb(self, cmd):
        return cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        rc = self.table.get((cmd[0], self.verb(cmd)), self.default)
        return None if rc is None else subprocess.CompletedProcess(cmd, rc, "", "exit %s" % rc)

    def verbs(self, program):
        return [self.verb(c) for c in self.calls if c[0] == program]


class ConnectStop(ConnectBase):
    """Uninstall fails closed: the files go only once the supervisor is confirmed stopped or absent."""

    def installed(self, mac=False):
        with mock.patch.object(cli, "is_macos", return_value=mac):
            self.assertEqual(self.install()[0], 0)
        return cli.plist_path() if mac else cli.service_path()

    def uninstall_with(self, runner, mac=False):
        with mock.patch.object(cli, "_run", side_effect=runner), mock.patch.object(cli, "is_macos", return_value=mac):
            return self.main("uninstall")

    def test_systemd_stop_confirmed_removes_and_a_stop_that_leaves_it_active_keeps(self):
        unit = self.installed()
        rc, out, err = self.uninstall_with(Runner({("systemctl", "disable"): 0, ("systemctl", "is-active"): 3}))
        self.assertEqual(rc, 0, err)
        self.assertFalse(unit.exists() or cli.config_path().exists())
        unit = self.installed()
        for table in ({("systemctl", "disable"): 0, ("systemctl", "is-active"): 0},
                      {("systemctl", "disable"): 1, ("systemctl", "is-active"): 0},
                      {("systemctl", "disable"): None, ("systemctl", "is-active"): None}):
            rc, out, err = self.uninstall_with(Runner(table))
            self.assertEqual(rc, 1, table)
            self.assertIn("were kept so the session stays managed", err)
            self.assertTrue(unit.exists() and cli.config_path().exists(), "nothing removed after an unconfirmed stop")
        rc, _, err = self.uninstall_with(Runner({("systemctl", "disable"): 1, ("systemctl", "is-active"): 3}))
        self.assertEqual(rc, 0, "disable failed but the unit is confirmed inactive: absent counts as stopped")
        self.assertFalse(unit.exists())

    def test_launchd_stop_confirmed_removes_and_an_agent_still_loaded_keeps(self):
        plist = self.installed(mac=True)
        rc, _, err = self.uninstall_with(Runner({("launchctl", "bootout"): 0, ("launchctl", "print"): 113}), mac=True)
        self.assertEqual(rc, 0, err)
        self.assertFalse(plist.exists() or cli.config_path().exists())
        plist = self.installed(mac=True)
        for table in ({("launchctl", "bootout"): 0, ("launchctl", "print"): 0},
                      {("launchctl", "bootout"): 3, ("launchctl", "print"): 0},
                      {("launchctl", "bootout"): None, ("launchctl", "print"): None}):
            rc, _, err = self.uninstall_with(Runner(table), mac=True)
            self.assertEqual(rc, 1, table)
            self.assertTrue(plist.exists() and cli.config_path().exists())
        rc, _, _ = self.uninstall_with(Runner({("launchctl", "bootout"): 3, ("launchctl", "print"): 113}), mac=True)
        self.assertEqual(rc, 0, "bootout said no such process and print confirms it: absent")
        self.assertFalse(plist.exists())


class ConnectReinstall(ConnectBase):
    """A changed config reaches the live child through a confirmed restart; an unchanged one restarts nothing."""

    def reinstall(self, runner, mac=False, *extra):
        with mock.patch.object(cli, "_run", side_effect=runner), mock.patch.object(cli, "is_macos", return_value=mac):
            return self.install(*extra)

    def test_systemd_restarts_on_a_changed_permission_mode_only(self):
        first = Runner()
        self.assertEqual(self.reinstall(first)[0], 0)
        self.assertNotIn("restart", first.verbs("systemctl"), "a fresh install starts, it does not restart")
        same = Runner()
        rc, out, _ = self.reinstall(same)
        self.assertEqual(rc, 0)
        self.assertNotIn("restart", same.verbs("systemctl"), "unchanged config: no restart")
        self.assertIn("installed: ", out)
        changed = Runner()
        rc, out, _ = self.reinstall(changed, False, "--permission-mode", "default")
        self.assertEqual(rc, 0)
        self.assertEqual(changed.verbs("systemctl")[-2:], ["restart", "is-active"], "restart issued and confirmed")
        self.assertIn("restarted: the session", out)
        self.assertEqual(cli.read_config(cli.config_path())["PERMISSION_MODE"], "default")
        failing = Runner({("systemctl", "restart"): 1})
        rc, _, err = self.reinstall(failing, False, "--permission-mode", "plan")
        self.assertEqual(rc, 1)
        self.assertIn("still has the old config", err)
        not_active = Runner({("systemctl", "is-active"): 3})
        rc, _, err = self.reinstall(not_active, False, "--permission-mode", "acceptEdits")
        self.assertEqual(rc, 1, "a restart that leaves the unit inactive is not confirmed")

    def test_launchd_kickstarts_on_a_changed_config_only(self):
        self.assertEqual(self.reinstall(Runner(), True)[0], 0)
        same = Runner()
        self.assertEqual(self.reinstall(same, True)[0], 0)
        self.assertNotIn("kickstart", same.verbs("launchctl"))
        changed = Runner()
        rc, out, _ = self.reinstall(changed, True, "--permission-mode", "default")
        self.assertEqual(rc, 0)
        self.assertIn("kickstart", changed.verbs("launchctl"))
        self.assertIn("-k", next(c for c in changed.calls if c[1] == "kickstart"))
        failing = Runner({("launchctl", "kickstart"): 1})
        rc, _, err = self.reinstall(failing, True, "--permission-mode", "plan")
        self.assertEqual((rc, "still has the old config" in err), (1, True))

    def test_a_changed_supervisor_text_restarts_the_unit_too(self):
        self.assertEqual(self.reinstall(Runner())[0], 0)
        unit = cli.service_path()
        unit.write_text(unit.read_text().replace("RestartSec=10", "RestartSec=11"))
        runner = Runner()
        self.assertEqual(self.reinstall(runner)[0], 0)
        self.assertIn("restart", runner.verbs("systemctl"), "a reloaded unit restarts its child")

    def test_no_supervisor_and_a_live_session_fails_a_changed_config(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
                                   "o = task_io.acquire_lock(%r, 'live'); print('held', flush=True); sys.stdin.read()\n"
                                   "task_io.release_lock(o)" % (str(PKG), str(self.inbox.parent))],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            rc, _, err = self.install("--no-service", "--permission-mode", "default")
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
        self.assertEqual(rc, 1)
        self.assertIn("still runs with the old config", err)
        self.assertEqual(self.install("--no-service", "--permission-mode", "default")[0], 0, "no session: a note is enough")


class ConnectStrict(ConnectBase):
    def strict(self, key_text=None, *extra):
        key = self.home / "task-hmac.key"
        if key_text is not None:
            key.write_text(key_text)
        return self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox),
                         "--results", str(self.results), "--hmac-strict", "--hmac-key-file", str(key), *extra)

    def test_strict_mode_needs_a_key_the_verifier_accepts(self):
        rc, _, err = self.strict(None)
        self.assertEqual(rc, 2)
        self.assertIn("--hmac-strict needs a usable key", err)
        self.assertFalse(cli.config_path().exists())
        for bad in ("not a key", "ab" * 31, "zz" * 32, ""):
            rc, _, err = self.strict(bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn("not a 64-hex-character key", err)
        rc, _, err = self.strict("AB" * 32 + "\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(cli.read_config(cli.config_path())["HMAC_STRICT"], "1")
        rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--hmac-strict")
        self.assertEqual(rc, 2, "strict without any key file")
        self.assertIn("no key file", err)

    def checks(self, out):
        return {line.split(":")[0][5:]: line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "FAIL")}

    def test_verify_fails_the_hmac_part_on_a_missing_or_malformed_key(self):
        self.assertEqual(self.strict("ab" * 32)[0], 0)
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["hmac key"], "PASS")
        (self.home / "task-hmac.key").write_text("garbage")
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, self.checks(out)["hmac key"]), (1, "FAIL"))
        self.assertIn("strict mode: ", out)
        self.assertIn("not a 64-hex-character key", out)
        (self.home / "task-hmac.key").unlink()
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, self.checks(out)["hmac key"]), (1, "FAIL"))
        self.assertIn("unreadable", out)
        # Not strict: a malformed key still fails (it reads unverifiable), no key at all is only a note.
        self.assertEqual(self.install()[0], 0)
        (self.home / "task-hmac.key").write_text("garbage")
        self.assertEqual(self.checks(self.main("verify")[1])["hmac key"], "FAIL")
        self.assertEqual(self.main("install", "--plugin-dir", str(self.plugin))[0], 0)
        self.assertEqual(self.checks(self.main("verify")[1])["hmac key"], "PASS")


class ConnectOwnership(ConnectBase):
    def test_an_unowned_config_is_kept_without_force_and_replaced_with_it(self):
        cfg = cli.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("CLAUDE=/somebody/elses\n")
        rc, _, err = self.install()
        self.assertEqual(rc, 1)
        self.assertIn("was not written by install", err)
        self.assertEqual(cfg.read_text(), "CLAUDE=/somebody/elses\n")
        self.assertFalse(cli.service_path().exists(), "nothing else was written either")
        rc, _, err = self.install("--force")
        self.assertEqual(rc, 0, err)
        self.assertTrue(cfg.read_text().startswith(cli.MARKER))

    def test_verify_stops_cleanly_on_a_config_missing_a_required_key(self):
        self.assertEqual(self.install()[0], 0)
        cfg = cli.config_path()
        cfg.write_text(cfg.read_text().replace("CLAUDE=", "CLAUDE_WAS="))
        rc, out, err = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config:", out)
        self.assertIn("lacks CLAUDE", out)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn("PASS", out, "nothing else is judged on a config this broken")


class ConnectBranches(ConnectBase):
    """The small branches the command tests do not reach, called in-process."""

    def test_console_script_run_and_platform_helpers(self):
        for p in self.patches:       # the real helpers, not the fixture's stand-ins
            p.stop()
        exe = self.bin / cli.COMMAND
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        with mock.patch.object(cli.sysconfig, "get_path", return_value=str(self.bin)):
            self.assertEqual(cli.console_script(), str(exe))
        with mock.patch.object(cli.sysconfig, "get_path", return_value=str(self.home / "nowhere")), \
                mock.patch.object(cli.shutil, "which", return_value=None):
            self.assertEqual(cli.console_script(), "")
        self.assertTrue(cli._ok(cli._run(["true"])))
        self.assertIsNone(cli._run([str(self.home / "no-such-program")]))
        self.assertEqual(cli._why(None), "could not run")
        self.assertEqual(cli._why(subprocess.CompletedProcess([], 4, "", "")), "exit 4")
        self.assertEqual(cli._why(subprocess.CompletedProcess([], 1, "", "a\nlast line\n")), "last line")
        self.assertIsInstance(cli.is_macos(), bool)
        self.assertTrue(cli._launchd_target().startswith("gui/"))
        self.assertTrue(str(cli.log_path()).endswith("claude-local.log"))
        self.assertEqual(cli.read_config(self.write_conf("# c\nA='x y'\nB=\nbare\n")), {"A": "x y", "B": ""})

    def write_conf(self, text):
        p = self.home / "some.conf"
        p.write_text(text)
        return p

    def test_install_supervisor_note_paths(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        with mock.patch.object(cli, "console_script", return_value=""):
            self.assertEqual(cli.install_supervisor(False), "none")
        for table, want in (({("systemctl", "show-environment"): 1}, "none"),
                            ({("systemctl", "daemon-reload"): 1}, "none"),
                            ({("systemctl", "enable"): 1}, "none"),
                            ({("loginctl", "enable-linger"): 1}, "started")):
            cli.service_path().unlink(missing_ok=True)
            with mock.patch.object(cli, "_run", side_effect=Runner(table)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.install_supervisor(False), want, table)
        with mock.patch.object(cli, "_run", side_effect=Runner()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.install_supervisor(False), "unchanged")
        with mock.patch.object(cli, "is_macos", return_value=True):
            with mock.patch.object(cli, "_run", side_effect=Runner()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.install_supervisor(False), "started")
                self.assertEqual(cli.install_supervisor(False), "unchanged")
            loaded = Runner({("launchctl", "bootstrap"): 1, ("launchctl", "print"): 0})
            with mock.patch.object(cli, "_run", side_effect=loaded), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.install_supervisor(False), "unchanged")
            self.assertIn("loaded already", out.getvalue())
            cli.plist_path().write_text(cli.plist_path().read_text().replace("<integer>10</integer>", "<integer>11</integer>"))
            with mock.patch.object(cli, "_run", side_effect=Runner()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.install_supervisor(False), "restarted")
            with mock.patch.object(cli, "_run", side_effect=Runner({("launchctl", "bootstrap"): 1, ("launchctl", "print"): 1})), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.install_supervisor(False), "none")
            with mock.patch.object(cli, "_run", side_effect=Runner()):
                self.assertEqual(cli.restart_supervisor(), "")
            with mock.patch.object(cli, "_run", side_effect=Runner({("launchctl", "print"): 1})):
                self.assertIn("kickstart", cli.restart_supervisor())

    def test_a_non_plugin_dir_an_unowned_config_at_uninstall_and_a_failed_write(self):
        rc, _, err = self.main("install", "--plugin-dir", str(self.home))
        self.assertEqual((rc, "is not a Claude Code plugin" in err), (2, True))
        cfg = cli.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("CLAUDE=/theirs\n")
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, cfg.read_text()), (1, "CLAUDE=/theirs\n"))
        self.assertIn("remove it yourself", err)
        with mock.patch.object(cli.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cli._write(self.home / "x.conf", "text", 0o600)
        self.assertEqual([p.name for p in self.home.glob(".x.conf.*")], [], "the temp file is gone")

    def test_run_and_verify_edge_branches(self):
        rc, _, err = self.main("run")
        self.assertEqual((rc, "unreadable" in err), (2, True), "no config yet")
        self.assertEqual(self.install("--no-service")[0], 0)
        (self.home / "claude-exit").write_text("0")
        cfg = cli.read_config(cli.config_path())
        self.assertEqual(cli.session_argv(dict(cfg, PERMISSION_MODE=""))[3:5], ["--permission-mode", "bypassPermissions"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", cli.child_env(dict(cfg, CLAUDE_CONFIG_DIR=""), {"PATH": "/bin"}))
        self.assertIn("auth status did not run", cli.auth_problem(dict(cfg, CLAUDE=str(self.home / "none"))))
        self.assertIsNone(cli.cli_version(str(self.home / "none")))
        self.relay.unlink()
        rc, out, _ = self.main("verify")
        self.assertIn("FAIL relay token", out)
        self.assertIn("missing: Sparrow has no token", out)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["nonsense"])
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])


class LockHolderPid(unittest.TestCase):
    def test_no_root_no_holder_live_holder_and_an_invalid_record(self):
        from ag2_sparrow import holder_lock, task_io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "inbox"
            self.assertIsNone(task_io.lock_holder_pid(str(root)), "no root")
            root.mkdir()
            self.assertIsNone(task_io.lock_holder_pid(str(root)), "no holder")
            owner = task_io.acquire_lock(str(root), "test")
            try:
                self.assertEqual(task_io.lock_holder_pid(str(root)), os.getpid())
            finally:
                task_io.release_lock(owner)
            self.assertIsNone(task_io.lock_holder_pid(str(root)))
            dead = {"record": {"kind": "valid", "record": {"pid": 424242}}, "holder_lineage": False}
            with mock.patch.object(holder_lock, "status", return_value=dead):
                self.assertIsNone(task_io.lock_holder_pid(str(root)), "a dead holder's record names nobody")
            with mock.patch.object(holder_lock, "status", return_value=dict(dead, holder_lineage=True)):
                self.assertEqual(task_io.lock_holder_pid(str(root)), 424242)
            with mock.patch.object(holder_lock, "status", return_value={"record": {"kind": "invalid", "reason": "x"}}):
                self.assertIsNone(task_io.lock_holder_pid(str(root)), "an invalid record names nobody")
            for exc in (OSError("boom"), holder_lock.LockError("boom")):
                with mock.patch.object(holder_lock, "status", side_effect=exc):
                    self.assertIsNone(task_io.lock_holder_pid(str(root)))


class ConnectLauncherProcess(ConnectBase):
    """The launcher as a real process: the lock across the child, SIGTERM reaping, a dead launcher."""

    def launcher(self, linger=30):
        (self.home / "claude-linger").write_text(str(linger))
        env = dict(os.environ, PYTHONPATH=str(PKG))
        proc = subprocess.Popen([sys.executable, "-c", "import sys; from ag2_sparrow.adapters.claude_code import cli\n"
                                 "sys.exit(cli.main(['run']))"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            if self.session():
                break
            time.sleep(0.05)
        self.assertIsNotNone(self.session(), "the session started")
        return proc, self.session()["pid"]

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait_gone(self, pid, seconds=10):
        for _ in range(int(seconds / 0.05)):
            if not self.alive(pid):
                return True
            try:
                os.waitpid(pid, os.WNOHANG)     # a reparented, already-dead child may need reaping by us
            except ChildProcessError:
                pass
            time.sleep(0.05)
        return not self.alive(pid)

    def test_sigterm_reaps_the_child_before_the_lock_is_released(self):
        from ag2_sparrow import task_io
        self.assertEqual(self.install("--no-service")[0], 0)
        launcher, child = self.launcher()
        work = str(self.inbox.parent)
        self.assertEqual(task_io.lock_holder_pid(work), launcher.pid, "verify would show the live launcher")
        os.kill(launcher.pid, signal.SIGTERM)
        _, err = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 143, err)
        self.assertTrue(self.wait_gone(child), "the child was terminated and reaped")
        self.assertIsNone(task_io.lock_holder_pid(work))
        self.assertIsInstance(holder_lock.read_holder(work), holder_lock.Missing, "no stale record")

    def test_a_dead_launcher_leaves_the_lock_with_its_child(self):
        from ag2_sparrow import task_io
        self.assertEqual(self.install("--no-service")[0], 0)
        launcher, child = self.launcher()
        work = str(self.inbox.parent)
        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=30)
        try:
            self.assertTrue(self.alive(child), "the session outlives its launcher")
            rc, _, err = self.main("run")
            self.assertEqual(rc, 75, "the child still holds the flock, so a second launcher is refused: " + err)
            self.assertIsNone(self.session_after(child), "and it started no second session")
        finally:
            os.kill(child, signal.SIGKILL)
            self.wait_gone(child)
        owner = task_io.acquire_lock(work, "cleanup")
        task_io.release_lock(owner)

    def session_after(self, first_pid):
        rec = self.session()
        return None if rec is None or rec["pid"] == first_pid else rec


class ConnectEnvAndPaths(ConnectBase):
    def test_auth_status_runs_under_the_same_allowlist(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        cfg = cli.read_config(cli.config_path())
        calls = []

        def spy(argv, **kw):
            calls.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true}', "")
        self.assertEqual(cli.auth_problem(cfg, spy), "")
        self.assertEqual(calls[0], cli.child_env(cfg), "claude auth status sees the child's allowlist, not os.environ")
        for secret in ("REMOTE_TASK_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(secret, calls[0])

    def test_a_directory_or_dangling_symlink_at_the_supervisor_path_is_one_line(self):
        unit = cli.service_path()
        unit.parent.mkdir(parents=True)
        unit.mkdir()
        rc, out, err = self.install()
        self.assertEqual(rc, 0)
        self.assertIn("is a directory, not a supervisor file", out)
        out = self.main("verify")[1]
        self.assertIn("FAIL supervisor", out)
        self.assertIn("is a directory", out)
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, "is a directory" in err), (1, True))
        self.assertTrue(unit.is_dir(), "left alone")
        unit.rmdir()
        unit.symlink_to(self.home / "gone")
        self.assertEqual(self.install()[0], 0)
        self.assertIn("dangling symlink", self.main("verify")[1])
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, "dangling symlink" in err), (1, True))
        self.assertTrue(unit.is_symlink(), "left alone")

    def test_an_unparsable_or_key_less_config_is_one_line_in_run_and_verify(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        cfg = cli.config_path()
        good = cfg.read_text()
        cfg.write_text(good + "PERMISSION_MODE='unclosed\n")
        rc, _, err = self.main("run")
        self.assertEqual((rc, "is not parsable" in err), (2, True))
        rc, out, err = self.main("verify")
        self.assertEqual((rc, "is not parsable" in out), (1, True))
        cfg.write_text(good.replace("INBOX_DIR=", "INBOX_WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks INBOX_DIR" in err), (2, True))
        self.assertNotIn("Traceback", err)
        self.assertIsNone(self.session())

    def test_a_childs_own_exit_75_is_reported_as_1(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        (self.home / "claude-exit").write_text("75")
        rc, _, err = self.main("run")
        self.assertEqual(rc, 1)
        self.assertIn("75 is reserved for another holder", err)

    def test_an_idempotent_macos_reinstall_does_not_rebootstrap_a_loaded_agent(self):
        loaded = Runner({("launchctl", "print"): 0})
        with mock.patch.object(cli, "is_macos", return_value=True), mock.patch.object(cli, "_run", side_effect=loaded):
            self.assertEqual(self.install()[0], 0)
            self.assertEqual(loaded.verbs("launchctl"), ["bootstrap"], "a fresh install bootstraps")
            loaded.calls.clear()
            rc, out, _ = self.install()
            self.assertEqual(rc, 0)
            self.assertEqual(loaded.verbs("launchctl"), ["print"], "unchanged and loaded: checked, not re-bootstrapped")
            self.assertIn("loaded already", out)


class ConnectVerifyUninstall(ConnectBase):
    def checks(self, out):
        return {line.split()[1].rstrip(":"): line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "FAIL")}

    def test_verify_passes_every_part_and_fails_the_broken_one(self):
        self.assertEqual(self.install()[0], 0)
        (self.home / "task-hmac.key").write_text("ab" * 32)
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.checks(out).values()), {"PASS"}, out)
        self.assertEqual(set(self.checks(out)), {"config", "claude", "login", "plugin", "inbox", "results", "hmac", "relay",
                                                 "supervisor"})
        self.assertIn("no live session holds the work root now", out)
        (self.plugin / ".claude-plugin" / "plugin.json").unlink()
        self.assertEqual(self.checks(self.main("verify")[1])["plugin"], "FAIL")
        (self.plugin / ".claude-plugin" / "plugin.json").write_text("{}")
        self.relay.chmod(0o644)
        self.assertEqual(self.checks(self.main("verify")[1])["relay"], "FAIL")
        self.relay.chmod(0o600)
        (self.home / "claude-auth").write_text('{"loggedIn": false}')
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["login"], "FAIL")
        self.assertIn("not logged in under CLAUDE_CONFIG_DIR=", out)
        (self.home / "claude-auth").unlink()
        (self.home / "claude-version").write_text("2.1.1")
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["claude"], "FAIL")
        self.assertNotIn("login", self.checks(out), "no login check without a usable CLI")
        (self.home / "claude-version").unlink()
        cli.service_path().unlink()
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, self.checks(out)["supervisor"]), (1, "FAIL"))

    def test_verify_without_a_config_says_so(self):
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config", out)

    def test_uninstall_removes_only_what_install_wrote(self):
        self.assertEqual(self.install()[0], 0)
        unit = cli.service_path()
        unit.write_text("[Unit]\nDescription=the owner's own unit\n")
        rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertTrue(unit.exists(), "an unmarked unit is kept")
        self.assertIn("was not written by install", err)
        self.assertFalse(cli.config_path().exists(), "the marked config still goes")
        self.assertEqual(self.install("--force")[0], 0)
        runner = Runner({("systemctl", "is-active"): 3})      # disable ok, then confirmed inactive
        with mock.patch.object(cli, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertFalse(unit.exists())
        self.assertFalse(cli.config_path().exists())
        self.assertEqual(runner.verbs("systemctl"), ["disable", "is-active"])
        self.assertEqual(self.relay.read_text(), f"REMOTE_TASK_TOKEN={TOKEN}\n", "Sparrow's token file is untouched")
        self.assertTrue((self.plugin / ".claude-plugin" / "plugin.json").exists())
        self.assertIn("nothing to remove", self.main("uninstall")[1])

    def test_device_env_path_is_the_shared_owners(self):
        from ag2_sparrow.adapters.hatch import cli as hatch_cli
        self.assertEqual(hatch_cli.device_env_path(), _dirs.device_env_path())
        self.assertEqual(_dirs.device_env_path(), self.relay)
        with mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": "/elsewhere/relay.env"}):
            self.assertEqual(_dirs.device_env_path(), Path("/elsewhere/relay.env"))


if __name__ == "__main__":
    unittest.main()
