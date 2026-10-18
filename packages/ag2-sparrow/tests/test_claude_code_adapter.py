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
import shutil
import stat
import subprocess
import sys
import tempfile
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




# ── `ag2-sparrow-connect claude-local`: the installed local shape ─────────────

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
    print(read("claude-version", "2.1.296") + " (Claude Code)"); sys.exit(0)
if sys.argv[1:3] == ["auth", "status"]:
    print(read("claude-auth", '{"loggedIn": true}')); sys.exit(0)
json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd()},
          open(os.path.join(home, "claude-session.json"), "w"))
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
               "SUTANDO_WORKSPACE_DIR": "/secret/ws", "REMOTE_TASK_TOKEN": TOKEN}
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
        runner = mock.patch.object(cli, "_run", side_effect=lambda cmd: self.calls.append(cmd))
        runner.start()
        self.addCleanup(runner.stop)
        mac = mock.patch.object(cli, "is_macos", return_value=False)
        mac.start()
        self.addCleanup(mac.stop)
        script = mock.patch.object(cli, "console_script", return_value=str(self.bin / cli.COMMAND))
        script.start()
        self.addCleanup(script.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.main("claude-local", "install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox),
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
        self.assertIn(f"ExecStart={exec_start} claude-local run\n", unit)
        for line in ("Restart=always", "RestartSec=10", "RestartPreventExitStatus=75", "WantedBy=default.target"):
            self.assertIn(line + "\n", unit)
        plist = cli.render_plist(exec_start, "/opt/bin:/usr/bin", "/var/log/x.log")
        self.assertTrue(cli.marked(plist, cli.PLIST_MARKER), plist[:200])
        self.assertTrue(plist.startswith("<?xml"), "the declaration stays first")
        body = plistlib.loads(plist.encode())
        self.assertEqual(body["ProgramArguments"], [exec_start, "claude-local", "run"])
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
        self.assertEqual(body["ProgramArguments"][1:], ["claude-local", "run"])
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
        self.assertEqual(cli.version_problem(str(self.claude)), "")
        (self.home / "claude-version").write_text("2.1.286")
        self.assertIn("older than the 2.1.287", cli.version_problem(str(self.claude)))
        (self.home / "claude-version").write_text("3.0.0")
        self.assertEqual(cli.version_problem(str(self.claude)), "")
        self.assertIn("did not answer", cli.version_problem(str(self.home / "no-such-claude")))


class ConnectRun(ConnectBase):
    def test_run_starts_the_session_with_the_isolated_env_and_the_autoarm_marker(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        rc, _, err = self.main("claude-local", "run")
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
        self.assertNotIn("REMOTE_TASK_TOKEN", env)
        self.assertEqual(rec["cwd"], str(self.inbox))
        self.assertIsNone(getattr(holder_lock.read_holder(str(self.inbox)), "record", None), "released after the child")

    def test_run_refuses_an_old_cli_and_starts_no_session(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        (self.home / "claude-version").write_text("2.0.0")
        rc, _, err = self.main("claude-local", "run")
        self.assertEqual(rc, 2)
        self.assertIn("claude 2.0.0 is older than the 2.1.287", err)
        self.assertIsNone(self.session())

    def test_a_second_launcher_exits_75_and_starts_no_session(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        # holder_lock allows one acquisition per process, so the first launcher is another process.
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
                                   "o = task_io.acquire_lock(%r, 'first-launcher'); print('held', flush=True); sys.stdin.read()\n"
                                   "task_io.release_lock(o)" % (str(PKG), str(self.inbox))],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            rc, _, err = self.main("claude-local", "run")
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
        self.assertEqual(rc, 75, err)
        self.assertIn("refusing to start", err)
        self.assertIsNone(self.session())

    def test_the_lock_is_held_while_the_child_runs(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        seen, inbox = {}, str(self.inbox)

        class Child:
            def __init__(self, argv, **kw):
                rec = holder_lock.read_holder(inbox)
                seen["during"] = getattr(rec, "record", None)
                self.stdin = io.StringIO()
                seen["argv"], seen["env"] = argv, kw["env"]

            def wait(self):
                seen["stdin"] = self.stdin.getvalue()
                return 0
        with mock.patch.object(cli, "config_path", return_value=cli.config_path()):
            args = type("A", (), {})()
            rc = cli.run(args, popen=lambda argv, **kw: Child(argv, **kw))
        self.assertEqual(rc, 0)
        self.assertEqual(seen["during"]["pid"], os.getpid(), "held by this launcher for the whole child")
        self.assertEqual(seen["stdin"], "", "nothing is written to stdin: the plugin arms itself")
        self.assertEqual(seen["env"]["AG2_AUTOARM"], "1")
        self.assertEqual(seen["argv"][0], str(self.claude))
        self.assertIsNone(getattr(holder_lock.read_holder(str(self.inbox)), "record", None), "released afterwards")

    def test_the_childs_exit_code_is_the_launchers(self):
        self.assertEqual(self.install("--no-service")[0], 0)
        (self.home / "claude-exit").write_text("3")
        self.assertEqual(self.main("claude-local", "run")[0], 3)


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


class ConnectVerifyUninstall(ConnectBase):
    def checks(self, out):
        return {line.split()[1].rstrip(":"): line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "FAIL")}

    def test_verify_passes_every_part_and_fails_the_broken_one(self):
        self.assertEqual(self.install()[0], 0)
        (self.home / "task-hmac.key").write_text("ab" * 32)
        rc, out, _ = self.main("claude-local", "verify")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.checks(out).values()), {"PASS"}, out)
        self.assertEqual(set(self.checks(out)), {"config", "claude", "login", "plugin", "inbox", "results", "hmac", "relay",
                                                 "supervisor"})
        self.assertIn("no session holds the inbox now", out)
        (self.plugin / ".claude-plugin" / "plugin.json").unlink()
        self.assertEqual(self.checks(self.main("claude-local", "verify")[1])["plugin"], "FAIL")
        (self.plugin / ".claude-plugin" / "plugin.json").write_text("{}")
        self.relay.chmod(0o644)
        self.assertEqual(self.checks(self.main("claude-local", "verify")[1])["relay"], "FAIL")
        self.relay.chmod(0o600)
        (self.home / "claude-auth").write_text('{"loggedIn": false}')
        out = self.main("claude-local", "verify")[1]
        self.assertEqual(self.checks(out)["login"], "FAIL")
        self.assertIn("not logged in under CLAUDE_CONFIG_DIR=", out)
        (self.home / "claude-auth").unlink()
        (self.home / "claude-version").write_text("2.1.1")
        out = self.main("claude-local", "verify")[1]
        self.assertEqual(self.checks(out)["claude"], "FAIL")
        self.assertNotIn("login", self.checks(out), "no login check without a usable CLI")
        (self.home / "claude-version").unlink()
        cli.service_path().unlink()
        rc, out, _ = self.main("claude-local", "verify")
        self.assertEqual((rc, self.checks(out)["supervisor"]), (1, "FAIL"))

    def test_verify_without_a_config_says_so(self):
        rc, out, _ = self.main("claude-local", "verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config", out)

    def test_uninstall_removes_only_what_install_wrote(self):
        self.assertEqual(self.install()[0], 0)
        unit = cli.service_path()
        unit.write_text("[Unit]\nDescription=the owner's own unit\n")
        rc, out, err = self.main("claude-local", "uninstall")
        self.assertEqual(rc, 1)
        self.assertTrue(unit.exists(), "an unmarked unit is kept")
        self.assertIn("was not written by install", err)
        self.assertFalse(cli.config_path().exists(), "the marked config still goes")
        self.assertEqual(self.install("--force")[0], 0)
        self.calls.clear()
        rc, out, err = self.main("claude-local", "uninstall")
        self.assertEqual(rc, 0, err)
        self.assertFalse(unit.exists())
        self.assertFalse(cli.config_path().exists())
        self.assertEqual(self.calls[0][:4], ["systemctl", "--user", "--no-ask-password", "disable"])
        self.assertEqual(self.relay.read_text(), f"REMOTE_TASK_TOKEN={TOKEN}\n", "Sparrow's token file is untouched")
        self.assertTrue((self.plugin / ".claude-plugin" / "plugin.json").exists())
        self.assertIn("nothing to remove", self.main("claude-local", "uninstall")[1])

    def test_device_env_path_is_the_shared_owners(self):
        from ag2_sparrow.adapters.hatch import cli as hatch_cli
        self.assertEqual(hatch_cli.device_env_path(), _dirs.device_env_path())
        self.assertEqual(_dirs.device_env_path(), self.relay)
        with mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": "/elsewhere/relay.env"}):
            self.assertEqual(_dirs.device_env_path(), Path("/elsewhere/relay.env"))


if __name__ == "__main__":
    unittest.main()
