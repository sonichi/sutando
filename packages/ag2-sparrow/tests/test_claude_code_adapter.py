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




# ── `ag2-sparrow-claude-local`: the installed local shape ─────────────

from ag2_sparrow import _dirs, connect_common as cm, holder_lock  # noqa: E402
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
L = cli.LOCAL
STAND_IN = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
            "o = task_io.acquire_lock(%r, %r); print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
OP_HOLDER = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm\n"
             "from ag2_sparrow.adapters.claude_code.cli import LOCAL\n"
             "with cm.operation_lock(LOCAL): print('held', flush=True); sys.stdin.read()")
INACTIVE = (3, "inactive\n", "")
ACTIVE = (0, "active\n", "")
NOT_LOADED = (113, "", 'Could not find service "ai.ag2.sparrow.claude-local" in domain for uid: 501\n')
START_VERBS = ("restart", "bootstrap", "kickstart")
STOP_VERBS = ("stop", "bootout")


def hold(root, name="first-launcher", script=STAND_IN):
    """A lock holder in another process (holder_lock allows one acquisition per process)."""
    p = subprocess.Popen([sys.executable, "-c", script % ((str(PKG), str(root), name) if script is STAND_IN
                                                             else (str(PKG),))],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def unhold(p):
    p.stdin.close()
    p.wait(timeout=30)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(pid, seconds=10):
    for _ in range(int(seconds / 0.05)):
        if not alive(pid):
            return True
        try:
            os.waitpid(pid, os.WNOHANG)     # a reparented, already-dead child may need reaping by us
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return not alive(pid)


def oplock_free():
    import fcntl
    fd = os.open(cm.oplock_path(L), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class Runner:
    """A manager command table for connect_common._run: (program, verb) -> rc or (rc, out, err); None =
    could not run. With a root, a start verb runs a stand-in launcher (adapter `adapter`) holding the
    root's lock, a stop verb ends it, and the probes answer from that stand-in and the unit on disk unless
    the table says otherwise: is-active / print from whether it is alive, is-enabled from enable/disable,
    show LoadState from whether the unit file exists."""

    def __init__(self, table=None, default=0, root=None, adapter="claude-local"):
        self.table, self.default, self.calls, self.root = dict(table or {}), default, [], root
        self.session, self.adapter, self.enabled = None, adapter, False

    def start_session(self):
        self.stop_session()
        self.session = hold(self.root, self.adapter)

    def stop_session(self):
        if self.session is not None:
            unhold(self.session)
            self.session = None

    def verb(self, cmd):
        return cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))

    def answer(self, verb):
        if verb in ("is-active", "print"):
            return (ACTIVE if verb == "is-active" else 0) if self.session else (INACTIVE if verb == "is-active" else NOT_LOADED)
        if verb == "is-enabled":
            return (0, "enabled\n", "") if self.enabled else (1, "disabled\n", "")
        if verb == "show":
            return (0, ("loaded" if cm.service_path(L).exists() else "not-found") + "\n", "")
        return self.default

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        verb = self.verb(cmd)
        got = self.table[(cmd[0], verb)] if (cmd[0], verb) in self.table else self.answer(verb)
        if got is None:
            return None
        rc, out, err = got if isinstance(got, tuple) else (got, "", "exit %s" % got)
        if rc == 0 and verb in ("enable", "disable"):
            self.enabled = verb == "enable"
        if self.root and rc == 0:
            if verb in START_VERBS:
                self.start_session()
            elif verb in STOP_VERBS:
                self.stop_session()
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def verbs(self, program):
        return [self.verb(c) for c in self.calls if c[0] == program]

    def pid(self):
        return self.session.pid if self.session else None


class ConnectBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()   # macOS: /var is /private/var once resolved
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.claude = self.bin / "claude"
        self.claude.write_text(FAKE_CLAUDE % {"py": sys.executable})
        self.claude.chmod(0o755)
        self.work = self.home / "work"
        self.inbox, self.results = self.work / "tasks", self.work / "results"
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
        # No test reaches the host's systemctl, loginctl or launchctl; the bounds are 2 s and 1 s, not 30.
        self.calls = []
        self.patches = [mock.patch.object(cm, "_run", side_effect=lambda cmd: self.calls.append(cmd)),
                        mock.patch.object(cm, "is_macos", return_value=False),
                        mock.patch.object(cm, "console_script", return_value=str(self.bin / cli.COMMAND)),
                        mock.patch.object(cm, "CONFIRM_TIMEOUT_S", 2.0), mock.patch.object(cm, "OP_WAIT_S", 1.0)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.runners = []
        self.addCleanup(lambda: [r.stop_session() for r in self.runners])

    def runner(self, table=None, default=0, root=True, adapter="claude-local"):
        r = Runner(table, default, str(self.work) if root else None, adapter)
        self.runners.append(r)
        return r

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox),
                         "--results", str(self.results), "--claude-config-dir", str(self.home / ".claude-sutando"),
                         "--hmac-key-file", str(self.home / "task-hmac.key"), *extra)

    def install_with(self, runner, mac=False, *extra):
        with mock.patch.object(cm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=mac):
            return self.install(*extra)

    def values(self, **over):
        v = {"CLAUDE": str(self.claude), "PLUGIN_DIR": str(self.plugin), "INBOX_DIR": str(self.inbox),
             "RESULTS_DIR": str(self.results), "CLAUDE_CONFIG_DIR": str(self.home / ".claude-sutando"),
             "HMAC_KEY_FILE": str(self.home / "task-hmac.key"), "HMAC_STRICT": "", "PERMISSION_MODE": "bypassPermissions",
             "ROOT": str(self.work)}
        v.update(over)
        return v

    def configure(self, **over):
        """The config as install writes it, with no manager involved: what run, verify and uninstall read."""
        for d in (self.inbox, self.results):
            d.mkdir(parents=True, exist_ok=True)
        cm._write(cli.config_path(), cli.render_config(self.values(**over)), 0o600)
        return cli.config_path()

    def session(self):
        p = self.home / "claude-session.json"
        return json.loads(p.read_text()) if p.exists() else None


class ConnectRender(ConnectBase):
    def test_unit_and_plist_carry_the_marker_and_paths_only(self):
        exec_start = str(self.bin / cli.COMMAND)
        unit = cm.render_service(L, exec_start)
        self.assertEqual(unit.split("\n")[0], cli.MARKER)
        self.assertIn(f"ExecStart={exec_start} run\n", unit)
        for line in ("Restart=always", "RestartSec=10", "RestartPreventExitStatus=75", "WantedBy=default.target"):
            self.assertIn(line + "\n", unit)
        plist = cm.render_plist(L, exec_start, "/opt/bin:/usr/bin", "/var/log/x.log")
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER), plist[:200])
        self.assertTrue(plist.startswith("<?xml"), "the declaration stays first")
        body = plistlib.loads(plist.encode())
        self.assertEqual(body["ProgramArguments"], [exec_start, "run"])
        self.assertEqual((body["Label"], body["KeepAlive"], body["RunAtLoad"]), (cli.AGENT_LABEL, True, True))
        self.assertEqual(body["EnvironmentVariables"], {"PATH": "/opt/bin:/usr/bin"})
        for text in (unit, plist, cli.render_config({"CLAUDE": "/x/claude", "INBOX_DIR": "/i"})):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("REMOTE_TASK_TOKEN", text)
        self.assertNotIn("Environment=", unit, "the unit carries paths in ExecStart, no values")
        self.assertTrue(cm.marked("<?xml?>\n" + cli.MARKER + "\nx", cli.MARKER), "a marker on the second line counts")
        self.assertFalse(cm.marked("a\nb\n" + cli.MARKER, cli.MARKER), "the marker is a leading line")


class ConnectInstall(ConnectBase):
    """Install is a quiescent replacement: stop, prove absent and free, write, start, require a fresh
    holder of this shape; anything less fails closed with the files as written, and the retry is the
    same install."""

    ACQUIRED = "the managed launcher acquired the expected root"

    def test_a_fresh_systemd_install_writes_starts_and_requires_a_fresh_holder(self):
        runner = self.runner()
        rc, out, err = self.install_with(runner)
        self.assertEqual(rc, 0, err)
        cfg_path = cli.config_path()
        self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
        cfg = cli.read_config(cfg_path)
        self.assertEqual((cfg["CLAUDE"], cfg["PLUGIN_DIR"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"], cfg["ROOT"]),
                         (str(self.claude), str(self.plugin), str(self.inbox), str(self.results), str(self.work)))
        self.assertEqual((cfg["PERMISSION_MODE"], cfg["HMAC_STRICT"]), ("bypassPermissions", ""))
        self.assertTrue(self.inbox.is_dir() and self.results.is_dir())
        unit = cli.service_path().read_text()
        self.assertTrue(unit.startswith(cli.MARKER))
        self.assertNotIn(TOKEN, unit + cfg_path.read_text())
        self.assertEqual(runner.verbs("systemctl"), ["is-active", "show-environment", "daemon-reload", "enable", "restart"],
                         "absent and free: no stop, then register and start")
        self.assertIn(f"{self.ACQUIRED}: {self.work} (launcher pid {runner.pid()})", out)
        self.assertIn("linger: enabled for owner", out)
        self.assertIsNone(self.session(), "install starts no session itself; the manager does")

    def test_a_reinstall_stops_proves_free_then_replaces_and_starts(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        old = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner, False, "--permission-mode", "default")
        self.assertEqual(rc, 0)
        verbs = runner.verbs("systemctl")
        self.assertEqual(verbs[:2], ["is-active", "stop"], "the job is stopped before anything is written")
        self.assertEqual(verbs[-3:], ["daemon-reload", "enable", "restart"])
        self.assertIn("changed: ", out)
        self.assertEqual(cli.read_config(cli.config_path())["PERMISSION_MODE"], "default")
        self.assertNotEqual(runner.pid(), old, "a fresh holder, not the old session")
        self.assertIn(f"launcher pid {runner.pid()}", out)

    def test_an_identical_retry_performs_the_whole_apply_again(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        old = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner)
        self.assertEqual(rc, 0)
        self.assertIn("stop", runner.verbs("systemctl"))
        self.assertIn("restart", runner.verbs("systemctl"))
        self.assertNotEqual(runner.pid(), old, "no unchanged shortcut: the old session is replaced")
        self.assertIn("rewritten: ", out)
        self.assertIn(self.ACQUIRED, out)

    def test_install_on_macos_bootstraps_and_a_reinstall_boots_out_with_wait_first(self):
        runner = self.runner()
        rc, out, err = self.install_with(runner, True, "--permission-mode", "default")
        self.assertEqual(rc, 0, err)
        plist = cli.plist_path().read_text()
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER))
        self.assertEqual(plistlib.loads(plist.encode())["ProgramArguments"][1:], ["run"])
        self.assertNotIn(TOKEN, plist)
        self.assertEqual(cli.read_config(cli.config_path())["PERMISSION_MODE"], "default")
        self.assertEqual(runner.verbs("launchctl"), ["print", "bootstrap"], "absent: no bootout before the bootstrap")
        self.assertIn("loaded: ai.ag2.sparrow.claude-local", out)
        self.assertFalse(cli.service_path().exists())
        first = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner, True, "--permission-mode", "plan")
        self.assertEqual(rc, 0)
        self.assertEqual(runner.verbs("launchctl"), ["print", "bootout", "print", "bootstrap"])
        self.assertIn("--wait", next(c for c in runner.calls if c[1] == "bootout"))
        self.assertNotEqual(runner.pid(), first)
        self.assertIn(f"launcher pid {runner.pid()}", out)

    def test_an_unconfirmed_start_fails_closed_keeps_the_files_and_the_same_install_retries(self):
        silent = self.runner(root=False)                      # the manager accepts, no launcher takes the lock
        rc, out, err = self.install_with(silent)
        self.assertEqual(rc, 1)
        self.assertIn(f"the managed launcher did not acquire {self.work} within 2 s (free)", err)
        self.assertIn(f"run {cli.COMMAND} uninstall, then {cli.COMMAND} install", err)
        self.assertTrue(cli.config_path().exists() and cli.service_path().exists(), "as written; nothing rolled back")
        self.assertNotIn(self.ACQUIRED, out)
        rc, out, _ = self.install_with(self.runner())
        self.assertEqual(rc, 0, "the same install again is the retry")
        self.assertIn(self.ACQUIRED, out)

    def test_confirmation_is_a_fresh_claude_local_holder_and_never_claims_the_session_is_alive(self):
        other = self.runner(adapter="stand-in")
        rc, out, err = self.install_with(other)
        self.assertEqual(rc, 1, "a held root is not enough: the holder must be this shape's launcher")
        self.assertIn(f"did not acquire {self.work}", err)
        self.assertIn(f"held by launcher pid {other.pid()}", err)
        other.stop_session()
        rc, out, _ = self.install_with(self.runner())
        self.assertEqual(rc, 0)
        line = next(ln for ln in out.splitlines() if self.ACQUIRED in ln)
        for word in ("alive", "session is running", "came up", "ready"):
            self.assertNotIn(word, line, "the lock is occupancy, the record attribution; readiness is the task's")

    def test_a_failed_start_fails_closed_with_the_same_line(self):
        for table in ({("systemctl", "restart"): 1}, {("systemctl", "daemon-reload"): 1}, {("systemctl", "enable"): 1},
                      {("systemctl", "show-environment"): None}):
            rc, _, err = self.install_with(self.runner(table))
            self.assertEqual(rc, 1, table)
            self.assertIn(f"the managed launcher did not acquire {self.work}", err)
            self.assertIn("uninstall, then", err)
        rc, _, err = self.install_with(self.runner({("launchctl", "bootstrap"): 1}), True)
        self.assertEqual((rc, "bootstrap failed" in err), (1, True))

    def test_a_stop_that_is_not_confirmed_applies_nothing(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        before = cli.config_path().read_text()
        for table in ({("systemctl", "stop"): 0, ("systemctl", "is-active"): ACTIVE},
                      {("systemctl", "is-active"): (4, "", "Failed to connect to bus\n")},
                      {("systemctl", "stop"): None}):
            rc, _, err = self.install_with(self.runner(table), False, "--permission-mode", "plan")
            self.assertEqual(rc, 1, table)
            self.assertIn("nothing was applied", err)
            self.assertEqual(cli.config_path().read_text(), before, "the old config stands")

    def test_install_refuses_an_old_cli_before_writing_anything(self):
        (self.home / "claude-version").write_text("2.1.200")
        rc, out, err = self.install()
        self.assertEqual(rc, 2)
        self.assertIn("claude 2.1.200 is older than the 2.1.287 this adapter needs", err)
        self.assertFalse(cli.config_path().exists() or cli.service_path().exists())
        self.assertIsNone(self.session())

    def test_ownership_refusals_are_final_and_name_the_file_to_remove_by_hand(self):
        cli.service_path().parent.mkdir(parents=True)
        cli.service_path().write_text("[Unit]\nDescription=mine\n")
        rc, out, err = self.install_with(self.runner())
        self.assertEqual(rc, 1)
        self.assertEqual(cli.service_path().read_text(), "[Unit]\nDescription=mine\n")
        self.assertFalse(cli.config_path().exists(), "nothing written before the refusal")
        self.assertIn(f"{cli.service_path()} was not written by install; remove it by hand (rm {cli.service_path()})", err)
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir", str(self.plugin), "--force"])
        self.assertEqual(e.exception.code, 2, "no --force on install")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["uninstall", "--force"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir", str(self.plugin), "--no-service"])

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


class ConnectRoot(ConnectBase):
    """The canonical root is persisted at install from --work or the lexical inbox parent, never re-derived;
    a root change is refused; the child runs under the held root inode."""

    def test_the_root_comes_from_work_or_the_lexical_inbox_parent_and_the_inbox_must_be_tasks(self):
        rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.home / "queue"))
        self.assertEqual(rc, 2)
        self.assertIn("the inbox must be <work>/tasks", err)
        self.assertFalse(cli.config_path().exists())
        runner = self.runner()
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--work", str(self.work))
        self.assertEqual(rc, 0, err)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual((cfg["ROOT"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"]),
                         (str(self.work), str(self.inbox), str(self.results)))
        self.assertEqual(cm.work_root(cfg), str(self.work))
        self.assertEqual(cm.canonical_root("", str(self.home / "w" / "sub" / ".." / "tasks")), (str(self.home / "w"), ""))

    def test_a_symlinked_inbox_locks_the_persisted_root_which_a_sibling_shares(self):
        real = self.home / "elsewhere"
        (real / "tasks").mkdir(parents=True)
        link = self.work
        link.symlink_to(real)
        runner = Runner(root=str(real))                        # the stand-in locks the real root, as the launcher will
        self.runners.append(runner)
        rc, _, err = self.install_with(runner, False, "--inbox", str(link / "tasks"))
        self.assertEqual(rc, 0, err)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual(cfg["ROOT"], str(real), "the lexical parent, canonicalized once at install")
        self.assertEqual(cfg["INBOX_DIR"], str(link / "tasks"), "the inbox path itself is kept as given")
        self.runners[-1].stop_session()
        holder = hold(str(real), "codex")                      # a sibling adapter on the same root
        try:
            rc, _, err = self.main("run")
            self.assertEqual(rc, 75, "the persisted root is what run locks: " + err)
            self.assertIsNone(self.session())
        finally:
            unhold(holder)
        cfg_path = cli.config_path()
        cfg_path.write_text(cfg_path.read_text().replace(f"ROOT={cfg['ROOT']}", "ROOT=" + str(self.home / "moved")))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "refusing to start" in err), (2, True), "ROOT is read, never re-derived from the inbox")

    def test_a_work_root_change_is_refused_with_uninstall_then_install(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        other = self.home / "other"
        (other / "tasks").mkdir(parents=True)
        before = cli.config_path().read_text()
        runner.calls.clear()
        rc, _, err = self.install_with(runner, False, "--work", str(other))
        self.assertEqual(rc, 1)
        self.assertIn(f"the work root is {self.work}, not {other}; run {cli.COMMAND} uninstall, then {cli.COMMAND} install", err)
        self.assertEqual(cli.config_path().read_text(), before)
        self.assertEqual(runner.calls, [], "refused before the manager is touched")

    def test_the_child_runs_against_tasks_under_the_held_root_inode_not_the_path(self):
        self.configure()
        from ag2_sparrow import task_io
        held_tasks = os.stat(self.inbox)
        seen, moved, real_acquire = {}, self.home / "moved-away", cm.acquire_lock

        def acquire_then_replace_the_path(root, adapter):
            owner = real_acquire(root, adapter)
            os.rename(self.work, moved)                       # the path is replaced right after the lock check
            self.work.mkdir()
            (self.work / "tasks").mkdir()
            return owner

        class Child:
            def __init__(self, argv, **kw):
                self.stdin = io.StringIO()
                fd = int(argv[3])
                st = os.fstat(fd)
                seen["cwd_is_held"] = (st.st_dev, st.st_ino) == (held_tasks.st_dev, held_tasks.st_ino)
                seen["argv"], seen["env"], seen["pass_fds"] = argv, kw["env"], kw["pass_fds"]
                seen["sibling"] = task_io.lock_held(str(moved))
                p = subprocess.Popen(["true"])
                p.wait()
                self.pid = p.pid
                self.rc = None

            work = self.work

            def poll(self):
                return self.rc

            def wait(self, timeout=None):
                self.rc = 0
                return 0
        with mock.patch.object(cm, "acquire_lock", side_effect=acquire_then_replace_the_path):
            rc = cli.run(type("A", (), {})(), popen=lambda argv, **kw: Child(argv, **kw))
        self.assertEqual(rc, 0)
        self.assertTrue(seen["cwd_is_held"], "the trampoline's fd is tasks/ under the held root inode, not the new path")
        self.assertEqual(seen["argv"][:3], [sys.executable, "-c", cm.TRAMPOLINE])
        self.assertEqual(seen["argv"][4], str(self.claude))
        self.assertEqual(seen["env"]["AG2_INBOX_DIR"], str(moved / "tasks"), "the held inode's path at spawn")
        self.assertEqual(len(seen["pass_fds"]), 2, "the lease and the tasks fd")
        self.assertEqual(seen["sibling"], "held", "the moved-away root (the held inode) is what a sibling contends on")
        self.assertEqual(task_io.lock_held(str(moved)), "free", "released after the confirmed reap")

    def test_a_config_without_a_root_is_one_line(self):
        self.configure()
        cfg = cli.config_path()
        cfg.write_text(cfg.read_text().replace("ROOT=", "WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks ROOT" in err), (2, True))


class ConnectOrphan(ConnectBase):
    """A held root with no registered job is an orphan: install refuses before any file is written."""

    def test_a_sigkill_orphan_reinstall_refuses_before_any_write_and_names_the_pid(self):
        self.configure()
        from ag2_sparrow import task_io
        before = cli.config_path().read_text()
        launcher, child = ConnectLauncherProcess.launcher(self)
        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=30)
        try:
            self.assertEqual(task_io.lock_held(str(self.work)), "held", "the leased child keeps the fd")
            self.assertFalse(task_io.lock_holder(str(self.work))["live"])
            runner = self.runner({("systemctl", "is-active"): INACTIVE})
            rc, out, err = self.install_with(runner, False, "--permission-mode", "default")
            self.assertEqual(rc, 1)
            self.assertIn(f"{self.work} is held by a session no job of {cli.SERVICE_NAME} manages", err)
            self.assertIn(f"launcher pid {launcher.pid} gone, the child alive", err)
            self.assertIn("nothing was applied", err)
            self.assertNotIn(ConnectInstall.ACQUIRED, out)
            self.assertEqual(runner.verbs("systemctl"), ["is-active"], "probed, not applied")
            self.assertEqual(cli.config_path().read_text(), before, "config untouched")
            self.assertFalse(cli.service_path().exists(), "no unit written")
        finally:
            os.kill(child, signal.SIGKILL)
            wait_gone(child)

    def test_a_manual_run_holding_the_root_refuses_a_reinstall_by_pid(self):
        self.configure()
        holder = hold(str(self.work))
        try:
            rc, _, err = self.install_with(self.runner({("launchctl", "print"): NOT_LOADED}), True)
            self.assertEqual(rc, 1)
            self.assertIn(f"no job of {cli.AGENT_LABEL} manages (launcher pid {holder.pid})", err)
            rc, _, err = self.install_with(self.runner({("launchctl", "print"): (1, "", "launchctl: hm\n")}), True)
            self.assertEqual((rc, "is unknown; nothing was applied" in err), (1, True), "an unknown probe applies nothing")
        finally:
            unhold(holder)

    def test_a_bootout_that_returns_early_leaves_the_old_holder_and_the_reinstall_reports_failure(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner, True)[0], 0)
        old, before = runner.pid(), cli.config_path().read_text()

        class Early(Runner):
            """bootout returns 0 at once and launchd forgets the job, but the old process keeps the lock."""
            def __call__(self, cmd):
                verb = self.verb(cmd)
                if verb == "print":
                    self.table[("launchctl", "print")] = NOT_LOADED if any(c[1] == "bootout" for c in self.calls) else 0
                if verb in ("bootout", "bootstrap"):
                    self.table[("launchctl", verb)] = 0
                return super().__call__(cmd)
        early = Early(root=None)
        rc, out, err = self.install_with(early, True, "--permission-mode", "default")
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.work} is held by launcher pid {old} after the stop; nothing was applied", err)
        self.assertNotIn(ConnectInstall.ACQUIRED, out)
        self.assertNotIn("bootstrap", early.verbs("launchctl"), "no start over a root that is not free")
        self.assertEqual(cli.config_path().read_text(), before, "nothing written")
        self.assertIsNone(runner.session.poll(), "the old session is still there, untouched")


class ConnectSerialize(ConnectBase):
    """One per-shape operation lock across install, uninstall and debug-run's check-then-acquire."""

    def test_concurrent_installs_serialize_on_the_operation_lock(self):
        other = hold(None, script=OP_HOLDER)
        try:
            rc, _, err = self.install_with(self.runner())
            self.assertEqual(rc, 1)
            self.assertIn(f"another {cli.COMMAND} operation is in progress (lock {cm.oplock_path(L)})", err)
            self.assertFalse(cli.config_path().exists(), "nothing written while the other installer holds the lock")
        finally:
            unhold(other)
        self.assertEqual(self.install_with(self.runner())[0], 0, "once it is released, the install proceeds")
        self.assertTrue(cm.oplock_path(L).exists())

    def test_debug_run_and_uninstall_serialize_on_the_operation_lock(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        other = hold(None, script=OP_HOLDER)
        try:
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
                rc, _, err = self.main("uninstall")
            self.assertEqual((rc, "operation is in progress" in err), (1, True))
            self.assertTrue(cli.config_path().exists())
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
                rc, _, err = self.main("debug-run")
            self.assertEqual((rc, "operation is in progress" in err), (2, True))
            self.assertIsNone(self.session())
        finally:
            unhold(other)
        seen = {}

        class Child:
            def __init__(self, argv, **kw):
                self.stdin = io.StringIO()
                seen["oplock_free_at_spawn"] = oplock_free()
                p = subprocess.Popen(["true"])
                p.wait()
                self.pid, self.rc = p.pid, None

            def poll(self):
                return self.rc

            def wait(self, timeout=None):
                self.rc = 0
                return 0
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
            rc = cli.debug_run(type("A", (), {})(), popen=lambda argv, **kw: Child(argv, **kw))
        self.assertEqual(rc, 0)
        self.assertTrue(seen["oplock_free_at_spawn"], "debug-run releases the operation lock once the root lock is held")
        self.assertTrue(cm.oplock_path(L).exists(), "the mutex file is never unlinked")
        with mock.patch.object(cm, "_run", side_effect=self.runner({("systemctl", "is-active"): INACTIVE})):
            self.assertEqual(self.main("uninstall")[0], 0)
        self.assertTrue(cm.oplock_path(L).exists())


class ConnectRun(ConnectBase):
    def test_run_starts_the_session_with_the_isolated_env_and_the_autoarm_marker(self):
        self.configure()
        rc, _, err = self.main("run")
        self.assertEqual(rc, 0, err)
        rec = self.session()
        self.assertEqual(rec["argv"][1:], ["--plugin-dir", str(self.plugin), "--permission-mode", "bypassPermissions",
                                           "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                                           "--verbose"])
        self.assertEqual(rec["argv"][0], str(self.claude), "the trampoline execs claude itself; argv is claude's")
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
        self.assertIsNone(getattr(holder_lock.read_holder(str(self.work)), "record", None), "released after the child")

    def test_run_refuses_an_old_cli_and_starts_no_session(self):
        self.configure()
        (self.home / "claude-version").write_text("2.0.0")
        rc, _, err = self.main("run")
        self.assertEqual(rc, 2)
        self.assertIn("claude 2.0.0 is older than the 2.1.287", err)
        self.assertIsNone(self.session())

    def test_a_second_launcher_exits_75_and_starts_no_session(self):
        self.configure()
        holder = hold(str(self.work))
        try:
            rc, _, err = self.main("run")
        finally:
            unhold(holder)
        self.assertEqual(rc, 75, err)
        self.assertIn("refusing to start", err)
        self.assertIsNone(self.session())

    def test_the_lock_is_held_and_recorded_before_the_child_and_leased_to_it(self):
        self.configure()
        from ag2_sparrow import task_io
        seen, work = {}, str(self.work)

        class Child:
            def __init__(self, argv, **kw):
                self.stdin = io.StringIO()
                seen["at_spawn"] = task_io.lock_holder(work)        # publish-before-spawn
                seen["argv"], seen["env"], seen["pass_fds"] = argv, kw["env"], kw["pass_fds"]
                p = subprocess.Popen(["true"])
                p.wait()
                self.pid, self.rc = p.pid, None

            def poll(self):
                return self.rc

            def wait(self, timeout=None):
                seen["during_wait"] = task_io.lock_holder_pid(work)
                seen["stdin"] = self.stdin.getvalue()
                self.rc = 0
                return 0
        rc = cli.run(type("A", (), {})(), popen=lambda argv, **kw: Child(argv, **kw))
        self.assertEqual(rc, 0)
        self.assertEqual((seen["at_spawn"]["pid"], seen["at_spawn"]["live"], seen["at_spawn"]["adapter"]),
                         (os.getpid(), True, "claude-local"))
        self.assertEqual(seen["during_wait"], os.getpid(), "held by this launcher while the child runs")
        self.assertEqual(seen["pass_fds"][0], holder_lock._OWNER.fd if holder_lock._OWNER else seen["pass_fds"][0])
        self.assertEqual(len(seen["pass_fds"]), 2, "the leased lock descriptor and the tasks fd go to the child")
        self.assertEqual(seen["stdin"], "", "nothing is written to stdin: the plugin arms itself")
        self.assertEqual(seen["env"]["AG2_AUTOARM"], "1")
        self.assertIsNone(task_io.lock_holder_pid(work), "released after the confirmed reap")

    def test_a_missing_tasks_dir_under_the_held_root_refuses(self):
        self.configure()
        shutil.rmtree(self.inbox)
        rc, _, err = self.main("run")
        self.assertEqual((rc, "no tasks/ under the held root" in err), (2, True))
        from ag2_sparrow import task_io
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_the_childs_exit_code_is_the_launchers_and_its_own_75_is_1(self):
        self.configure()
        (self.home / "claude-exit").write_text("3")
        self.assertEqual(self.main("run")[0], 3)
        (self.home / "claude-exit").write_text("75")
        rc, _, err = self.main("run")
        self.assertEqual(rc, 1)
        self.assertIn("75 is reserved for another holder", err)


class ConnectDebugRun(ConnectBase):
    """No --no-service; debug-run refuses a registered job, an unknown one or a held root."""

    def test_debug_run_refuses_a_registered_job_an_unknown_one_or_a_held_root_else_runs(self):
        self.configure()
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): ACTIVE})):
            rc, _, err = self.main("debug-run")
        self.assertEqual((rc, f"{cli.SERVICE_NAME} is registered" in err), (2, True))
        self.assertIsNone(self.session())
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): None})):
            rc, _, err = self.main("debug-run")
        self.assertEqual((rc, "unknown state" in err), (2, True))
        holder = hold(str(self.work))
        try:
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
                rc, _, err = self.main("debug-run")
        finally:
            unhold(holder)
        self.assertEqual(rc, 2)
        self.assertIn(f"held by launcher pid {holder.pid}; stop that session first", err)
        self.assertIsNone(self.session())
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
            rc, _, err = self.main("debug-run")
        self.assertEqual(rc, 0, err)
        self.assertIsNotNone(self.session(), "absent and free: the session runs in the foreground")
        rc, _, err = self.main("debug-run")
        self.assertEqual((rc, "unreadable" in err or "lacks" in err), (2, False))


class ConnectMod(unittest.TestCase):
    def test_the_mod_auto_arms_only_on_the_launchers_marker(self):
        src = REGISTER.read_text()
        self.assertIn(f"(await $.env.get('{cli.AUTOARM_VAR}')) === '1'", src)
        start = src[src.index("session.start"):src.index("on('tool.call'")]
        self.assertIn("r = await arm($, s,", start, "session.start arms from the env with no model turn")
        self.assertIn("try {", start, "a failed auto-arm never breaks the session")
        self.assertIn("AG2_INBOX_DIR", src)
        self.assertIn("AG2_RESULTS_DIR", src)


class ConnectUninstall(ConnectBase):
    """Teardown, config last: stop, explicit absent (systemd: inactive and disabled), the root free, the
    unit, reload with LoadState=not-found, then the config; every refusal names the recovery."""

    def test_systemd_stop_disable_absent_free_unit_reload_not_found_then_config(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        runner.calls.clear()
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("systemctl"), ["stop", "is-active", "disable", "is-enabled", "daemon-reload", "show"])
        self.assertFalse(cli.service_path().exists() or cli.config_path().exists())
        self.assertIn(f"stopped: {cli.SERVICE_NAME}", out)
        self.assertLess(out.index(f"removed: {cli.service_path()}"), out.index(f"removed: {cli.config_path()}"), "config last")
        self.assertEqual(self.relay.read_text(), f"REMOTE_TASK_TOKEN={TOKEN}\n", "Sparrow's token file is untouched")
        self.assertTrue((self.plugin / ".claude-plugin" / "plugin.json").exists())
        self.assertIn("nothing to remove", self.main("uninstall")[1])

    def test_a_partial_systemd_teardown_keeps_the_config_and_completes_on_retry(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        for table, why in (({("systemctl", "daemon-reload"): 1}, "daemon-reload failed"),
                           ({("systemctl", "show"): (0, "loaded\n", "")}, "still known to systemd (LoadState loaded)")):
            self.configure()
            cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
            with mock.patch.object(cm, "_run", side_effect=self.runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn(why, err)
            self.assertFalse(cli.service_path().exists(), "the unit went")
            self.assertTrue(cli.config_path().exists(), "the config stays, so the rerun knows the root")
            self.assertIn("kept so a rerun knows the work root", err)
            runner = self.runner()
            with mock.patch.object(cm, "_run", side_effect=runner):
                rc, out, err = self.main("uninstall")
            self.assertEqual(rc, 0, err)
            self.assertEqual(runner.verbs("systemctl"), ["daemon-reload", "show"], "the retry finishes from where it stopped")
            self.assertFalse(cli.config_path().exists())
        for table in ({("systemctl", "is-enabled"): (0, "enabled\n", "")}, {("systemctl", "is-enabled"): None}):
            self.assertEqual(self.install_with(self.runner())[0], 0)
            self.runners[-1].stop_session()
            with mock.patch.object(cm, "_run", side_effect=self.runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual((rc, "not confirmed disabled" in err), (1, True), table)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
            with mock.patch.object(cm, "_run", side_effect=self.runner()):
                self.assertEqual(self.main("uninstall")[0], 0)

    def test_launchd_boots_out_with_wait_and_requires_the_documented_not_found(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner, True)[0], 0)
        runner.calls.clear()
        with mock.patch.object(cm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("launchctl"), ["bootout", "print"])
        self.assertIn("--wait", runner.calls[0])
        self.assertFalse(cli.plist_path().exists() or cli.config_path().exists())
        self.assertLess(out.index(f"removed: {cli.plist_path()}"), out.index(f"removed: {cli.config_path()}"), "config last")
        plist = cli.plist_path()
        for table in ({("launchctl", "bootout"): 0, ("launchctl", "print"): 0},                 # still loaded
                      {("launchctl", "bootout"): 3, ("launchctl", "print"): (1, "", "odd\n")},  # not the documented error
                      {("launchctl", "bootout"): None, ("launchctl", "print"): None}):
            self.assertEqual(self.install_with(self.runner(), True)[0], 0)
            self.runners[-1].stop_session()
            with mock.patch.object(cm, "_run", side_effect=Runner(table)), mock.patch.object(cm, "is_macos", return_value=True):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertTrue(plist.exists() and cli.config_path().exists(), "kept after an unconfirmed stop")
            self.assertIn("Manual recovery: 1. stop the job by hand: launchctl bootout --wait gui/", err)
            self.assertIn(f"3. re-run {cli.COMMAND} uninstall", err)
            with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "bootout"): 3, ("launchctl", "print"): NOT_LOADED})), \
                    mock.patch.object(cm, "is_macos", return_value=True):
                self.assertEqual(self.main("uninstall")[0], 0, "bootout said no such process and print confirms it: absent")

    def test_a_live_holder_refuses_with_the_recovery_steps_verbatim(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        keeper = runner.session
        runner.session = None                                  # the stop verb reaches nobody: the holder stays
        try:
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn(f"{self.work} is held by launcher pid {keeper.pid}; ", err)
            self.assertIn("were kept so a rerun knows the work root", err)
            self.assertIn(f"Manual recovery: 1. stop the job by hand: systemctl --user stop {cli.SERVICE_NAME}", err)
            self.assertIn(f"2. stop what holds the work root {self.work}: kill {keeper.pid} (the launcher pid in the lock record)", err)
            self.assertIn(f"3. re-run {cli.COMMAND} uninstall", err)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        finally:
            unhold(keeper)
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
            self.assertEqual(self.main("uninstall")[0], 0, "re-run once the root is free")
        self.assertFalse(cli.config_path().exists())

    def test_an_orphan_child_names_lsof_on_the_lock_file(self):
        from ag2_sparrow import task_io
        self.configure()
        cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
        launcher, child = ConnectLauncherProcess.launcher(self)
        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=30)
        try:
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): INACTIVE})):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn("held, launcher gone (child alive)", err)
            self.assertIn(f"lsof {task_io.lock_path(str(self.work))}", err)
            self.assertTrue(cli.config_path().exists())
        finally:
            os.kill(child, signal.SIGKILL)
            wait_gone(child)

    def test_an_unknown_stop_probe_or_a_lock_error_refuses(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        for table in ({("systemctl", "stop"): 0, ("systemctl", "is-active"): ACTIVE},
                      {("systemctl", "stop"): 1, ("systemctl", "is-active"): (4, "", "Failed to connect to bus\n")},
                      {("systemctl", "stop"): None, ("systemctl", "is-active"): None}):
            with mock.patch.object(cm, "_run", side_effect=Runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn("Manual recovery", err)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        with mock.patch.object(cm, "_run", side_effect=self.runner()), mock.patch.object(cm, "lock_held", return_value="unknown"):
            rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertIn("unknown (the lock of", err)
        self.assertTrue(cli.config_path().exists(), "a lock error keeps everything")
        with mock.patch.object(cm, "_run", side_effect=self.runner({("systemctl", "stop"): 1})):
            self.assertEqual(self.main("uninstall")[0], 0, "stop failed but the unit is confirmed inactive: absent")

    def test_files_install_did_not_write_are_kept_and_named(self):
        self.configure()
        cm._write(cli.service_path(), "[Unit]\nDescription=the owner's own unit\n", 0o644)
        rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertIn(f"was not written by install; remove it by hand (rm {cli.service_path()})", err)
        self.assertTrue(cli.service_path().exists() and cli.config_path().exists(), "nothing goes while a foreign unit may run it")
        cli.service_path().unlink()
        cli.config_path().write_text("CLAUDE=/theirs\n")
        rc, _, err = self.main("uninstall")
        self.assertEqual((rc, cli.config_path().read_text()), (1, "CLAUDE=/theirs\n"))
        self.assertIn("remove it by hand", err)


class ConnectStrict(ConnectBase):
    def strict(self, key_text=None, *extra):
        key = self.home / "task-hmac.key"
        if key_text is not None:
            key.write_text(key_text)
        return self.install_with(self.runner(), False, "--hmac-strict", *extra)

    def test_strict_mode_needs_a_key_the_verifier_accepts(self):
        rc, _, err = self.strict()
        self.assertEqual(rc, 2)
        self.assertIn("--hmac-strict needs a usable key", err)
        self.assertFalse(cli.config_path().exists())
        rc, _, err = self.strict("not-a-key")
        self.assertEqual((rc, "not a 64-hex-character key" in err), (2, True))
        rc, _, err = self.strict("ab" * 32)
        self.assertEqual(rc, 0, err)
        cfg = cli.read_config(cli.config_path())
        self.assertEqual((cfg["HMAC_STRICT"], cfg["HMAC_KEY_FILE"]), ("1", str(self.home / "task-hmac.key")))
        self.runners[-1].stop_session()
        self.assertEqual(self.main("run")[0], 0)
        self.assertEqual(self.session()["env"]["AG2_HMAC_STRICT"], "1")
        rc, _, err = self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox), "--hmac-strict")
        self.assertEqual((rc, "no key file" in err), (2, True))

    def test_verify_fails_the_hmac_part_on_a_missing_or_malformed_key(self):
        self.configure(HMAC_STRICT="1")
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL hmac key: strict mode:", out)
        (self.home / "task-hmac.key").write_text("short")
        self.assertIn("FAIL hmac key: strict mode:", self.main("verify")[1])
        (self.home / "task-hmac.key").write_text("ab" * 32)
        self.assertIn("PASS hmac key", self.main("verify")[1])
        self.configure(HMAC_KEY_FILE="", HMAC_STRICT="")
        self.assertIn("PASS hmac key: none (every task reads unsigned)", self.main("verify")[1])


class ConnectOwnership(ConnectBase):
    def test_an_unowned_config_is_kept_and_named_on_install_run_and_uninstall(self):
        cfg = cli.config_path()
        cfg.write_text("CLAUDE=/theirs\n")
        rc, _, err = self.install_with(self.runner())
        self.assertEqual(rc, 1)
        self.assertIn(f"{cfg} was not written by install; remove it by hand (rm {cfg})", err)
        self.assertEqual(cfg.read_text(), "CLAUDE=/theirs\n")
        self.assertFalse(cli.service_path().exists(), "nothing written before the refusal")

    def test_a_dangling_a_directory_or_an_unparsable_config_is_one_line(self):
        cfg = cli.config_path()
        cfg.symlink_to(self.home / "gone")
        rc, _, err = self.main("run")
        self.assertEqual((rc, "dangling symlink" in err), (2, True))
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, f"remove it by hand (rm {cfg})" in err), (1, True))
        cfg.unlink()
        cfg.mkdir()
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, "is a directory, not a config; remove it yourself" in err), (1, True))
        self.assertEqual(self.main("verify")[0], 1)
        rc, _, err = self.main("uninstall")
        self.assertEqual((rc, "is a directory" in err), (1, True))
        self.assertTrue(cfg.is_dir())
        cfg.rmdir()
        good = self.configure().read_text()
        cfg.write_text(good + "PERMISSION_MODE='unclosed\n")
        rc, _, err = self.main("run")
        self.assertEqual((rc, "is not parsable" in err), (2, True))
        rc, out, err = self.main("verify")
        self.assertEqual((rc, "is not parsable" in out), (1, True))
        rc, _, err = self.install_with(self.runner())
        self.assertEqual(rc, 0, "a marked but unparsable config is replaced by install: " + err)

    def test_verify_stops_cleanly_on_a_config_missing_a_required_key(self):
        self.configure(PLUGIN_DIR="")
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config", out)
        self.assertIn("lacks PLUGIN_DIR", out)
        self.assertNotIn("claude", {line.split()[1].rstrip(":") for line in out.splitlines() if line[:4] in ("PASS", "FAIL")})


class ConnectBranches(ConnectBase):
    """The small branches the command tests do not reach, called in-process."""

    def test_console_script_run_and_platform_helpers(self):
        for p in self.patches:       # the real helpers, not the fixture's stand-ins
            p.stop()
        exe = self.bin / cli.COMMAND
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        with mock.patch.object(cm.sysconfig, "get_path", return_value=str(self.bin)):
            self.assertEqual(cm.console_script(L), str(exe))
        with mock.patch.object(cm.sysconfig, "get_path", return_value=str(self.home / "nowhere")), \
                mock.patch.object(cm.shutil, "which", return_value=None):
            self.assertEqual(cm.console_script(L), "")
            self.assertIn("was not found beside", cm.render_supervisor(L)[1])
        self.assertTrue(cm._ok(cm._run(["true"])))
        self.assertIsNone(cm._run([str(self.home / "no-such-program")]))
        self.assertEqual(cm._why(None), "could not run")
        self.assertEqual(cm._why(subprocess.CompletedProcess([], 4, "", "")), "exit 4")
        self.assertEqual(cm._why(subprocess.CompletedProcess([], 1, "", "a\nlast line\n")), "last line")
        self.assertIsInstance(cm.is_macos(), bool)
        self.assertTrue(cm._launchd_target().startswith("gui/"))
        self.assertTrue(str(cm.log_path(L)).endswith("claude-local.log"))
        self.assertEqual(cli.read_config(self.write_conf("# c\nA='x y'\nB=\nbare\n")), {"A": "x y", "B": ""})
        self.assertEqual(cli.SHAPE, "claude-local")
        with mock.patch.object(cm, "is_macos", return_value=True):
            self.assertEqual(cm.job_name(L), cli.AGENT_LABEL)
            self.assertIn("launchctl bootout --wait gui/", cm.stop_command(L))
        with mock.patch.object(cm, "is_macos", return_value=False):
            self.assertEqual(cm.stop_command(L), f"systemctl --user stop {cli.SERVICE_NAME}")

    def write_conf(self, text):
        p = self.home / "some.conf"
        p.write_text(text)
        return p

    def test_probes_read_explicit_outputs_only(self):
        for got, want in ((ACTIVE, "active"), ((0, "", ""), "active"), ((3, "activating\n", ""), "active"), (INACTIVE, "absent"),
                          ((3, "failed\n", ""), "absent"), ((4, "", "Failed to connect to bus\n"), "unknown"), (None, "unknown")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-active"): got})):
                self.assertEqual(cm.unit_active(L), want, got)
        for got, want in ((0, "active"), (NOT_LOADED, "absent"), ((113, "", "Bad request\n"), "unknown"), (None, "unknown")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): got})):
                self.assertEqual(cm.agent_loaded(L), want, got)
        for got, want in (((0, "enabled\n", ""), "enabled"), ((1, "disabled\n", ""), "disabled"), ((1, "not-found\n", ""), "disabled"),
                          ((0, "", ""), "unknown"), (None, "unknown")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "is-enabled"): got})):
                self.assertEqual(cm.unit_enabled(L), want, got)
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): None})):
            self.assertEqual(cm.unit_load_state(L), "unknown")
        with mock.patch.object(cm, "is_macos", return_value=True), \
                mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): 0})):
            self.assertEqual(cm.manager_state(L), "active")
        with mock.patch.object(cm, "_run", side_effect=Runner({("loginctl", "enable-linger"): 1})), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cm.linger(L)
        self.assertIn("enable-linger owner failed", out.getvalue())

    def test_a_non_plugin_dir_a_missing_console_script_and_a_failed_write(self):
        rc, _, err = self.main("install", "--plugin-dir", str(self.home))
        self.assertEqual((rc, "is not a Claude Code plugin" in err), (2, True))
        with mock.patch.object(cm, "console_script", return_value=""):
            rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, "was not found beside" in err), (1, True))
        self.assertFalse(cli.config_path().exists(), "nothing written without a unit to write")
        with mock.patch.object(cm.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cm._write(self.home / "x.conf", "text", 0o600)
        self.assertEqual([p.name for p in self.home.glob(".x.conf.*")], [], "the temp file is gone")

    def test_run_and_verify_edge_branches(self):
        rc, _, err = self.main("run")
        self.assertEqual((rc, "unreadable" in err), (2, True), "no config yet")
        rc, _, err = self.main("debug-run")
        self.assertEqual((rc, "unreadable" in err), (2, True))
        self.configure()
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
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["nonsense"])
        self.assertEqual(e.exception.code, 2)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--plugin-dir"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])


class LockHelpers(unittest.TestCase):
    """task_io: lock_held is held | free | unknown (the flock, occupancy); lock_holder is attribution."""

    def test_lock_held_lock_holder_lock_path_and_lock_holder_pid(self):
        from ag2_sparrow import task_io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "work"
            self.assertEqual(task_io.lock_held(str(root)), "free", "no root")
            self.assertIsNone(task_io.lock_holder_pid(str(root)))
            self.assertEqual(task_io.lock_holder(str(root)), {})
            root.mkdir()
            self.assertEqual(task_io.lock_held(str(root)), "free", "no lock file yet")
            holder = hold(str(root), "claude-local")
            try:
                self.assertEqual(task_io.lock_held(str(root)), "held")
                rec = task_io.lock_holder(str(root))
                self.assertEqual((rec["pid"], rec["live"], rec["adapter"]), (holder.pid, True, "claude-local"))
                self.assertTrue(rec["start"])
                self.assertEqual(task_io.lock_holder_pid(str(root)), holder.pid)
                self.assertTrue(task_io.lock_path(str(root)).endswith("/lock"))
            finally:
                unhold(holder)
            self.assertEqual(task_io.lock_held(str(root)), "free", "released: the file stays, the flock is gone")
            self.assertIsNone(task_io.lock_holder_pid(str(root)))
            with mock.patch.object(task_io.holder_lock, "lock_dir", side_effect=PermissionError("no")):
                self.assertEqual(task_io.lock_held(str(root)), "unknown", "an error is unknown, never free")
                self.assertIsNone(task_io.lock_path(str(root)))
            with mock.patch.object(task_io.os, "open", side_effect=PermissionError("no")):
                self.assertEqual(task_io.lock_held(str(root)), "unknown")
            with mock.patch.object(task_io.holder_lock, "status", side_effect=OSError("no")):
                self.assertEqual(task_io.lock_holder(str(root)), {})

    def test_a_stale_record_under_a_free_lock_reads_free(self):
        from ag2_sparrow import task_io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "work"
            root.mkdir()
            holder = hold(str(root))
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait(timeout=30)
            self.assertEqual(task_io.lock_held(str(root)), "free", "the record may linger; the flock does not")
            self.assertFalse(task_io.lock_holder(str(root)).get("live", False))
            self.assertEqual(cm.holder_line(str(root)), "free")

    def test_release_after_reap_refuses_a_live_child_and_leaves_the_lock_held(self):
        from ag2_sparrow import task_io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "work"
            root.mkdir()
            owner = task_io.acquire_lock(str(root), "claude-local")
            fd = task_io.lease_fd(owner)
            child = subprocess.Popen(["sleep", "30"], pass_fds=(fd,))
            try:
                self.assertFalse(task_io.release_after_reap(owner, child.pid))
                self.assertEqual(task_io.lock_held(str(root)), "held", "nothing unlocked")
            finally:
                child.kill()
                child.wait(timeout=30)
            self.assertTrue(task_io.release_after_reap(owner, child.pid))
            self.assertEqual(task_io.lock_held(str(root)), "free")


class ConnectLauncherProcess(ConnectBase):
    """Every launcher exit reaps the child, as real processes; SIGKILL alone leaves it the lease."""

    def launcher(self, linger=30, driver="sys.exit(cli.main(['run']))"):
        (self.home / "claude-linger").write_text(str(linger))
        env = dict(os.environ, PYTHONPATH=str(PKG))
        proc = subprocess.Popen([sys.executable, "-c", "import sys, subprocess; from ag2_sparrow.adapters.claude_code import cli\n"
                                 + driver], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            if self.session():
                break
            time.sleep(0.05)
        self.assertIsNotNone(self.session(), "the session started")
        return proc, self.session()["pid"]

    @staticmethod
    def alive(pid):
        return alive(pid)

    def wait_gone(self, pid, seconds=10):
        return wait_gone(pid, seconds)

    def assert_reaped_and_released(self, child, work):
        from ag2_sparrow import task_io
        self.assertTrue(self.wait_gone(child), "the child was terminated and reaped")
        self.assertEqual(task_io.lock_held(work), "free")
        self.assertIsInstance(holder_lock.read_holder(work), holder_lock.Missing, "no stale record")

    def test_sigterm_reaps_the_child_before_the_lock_is_released(self):
        from ag2_sparrow import task_io
        self.configure()
        launcher, child = self.launcher()
        work = str(self.work)
        self.assertEqual(task_io.lock_holder_pid(work), launcher.pid, "verify would show the live launcher")
        self.assertEqual(self.session()["cwd"], str(self.inbox), "the child's cwd is tasks/ under the held root")
        os.kill(launcher.pid, signal.SIGTERM)
        _, err = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 143, err)
        self.assert_reaped_and_released(child, work)

    def test_sigint_reaps_the_child_before_the_lock_is_released(self):
        self.configure()
        launcher, child = self.launcher()
        os.kill(launcher.pid, signal.SIGINT)
        _, err = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 130, err)
        self.assertIn("interrupted; child reaped", err)
        self.assert_reaped_and_released(child, str(self.work))

    def test_a_post_spawn_exception_reaps_the_child_before_the_lock_is_released(self):
        driver = ("class Faulty(subprocess.Popen):\n"
                  "    n = 0\n"
                  "    def wait(self, timeout=None):\n"
                  "        Faulty.n += 1\n"
                  "        if Faulty.n == 2: raise RuntimeError('boom after spawn')\n"
                  "        return super().wait(timeout=timeout)\n"
                  "sys.exit(cli.run(type('A', (), {})(), popen=Faulty))")
        self.configure()
        launcher, child = self.launcher(driver=driver)
        _, err = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 1, err)
        self.assertIn("boom after spawn", err, "the exception is re-raised, not swallowed")
        self.assert_reaped_and_released(child, str(self.work))

    def test_a_sigkilled_launcher_leaves_the_lease_with_its_child_and_verify_says_so(self):
        from ag2_sparrow import task_io
        self.configure()
        launcher, child = self.launcher()
        work = str(self.work)
        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=30)
        try:
            self.assertTrue(self.alive(child), "the session outlives its launcher")
            rc, _, err = self.main("run")
            self.assertEqual(rc, 75, "the child still holds the flock, so a second launcher is refused: " + err)
            self.assertIsNone(self.session_after(child), "and it started no second session")
            self.assertEqual(task_io.lock_held(work), "held")
            self.assertIn("INFO session: %s held, launcher gone (child alive)" % work, self.main("verify")[1])
        finally:
            os.kill(child, signal.SIGKILL)
            self.wait_gone(child)
        owner = task_io.acquire_lock(work, "cleanup")
        task_io.release_lock(owner)

    def test_an_unconfirmed_reap_exits_without_releasing(self):
        """A kill that does not take: the launcher exits, the lock is not unlocked, the child stays fenced."""
        driver = ("class Immortal(subprocess.Popen):\n"
                  "    def terminate(self): pass\n"
                  "    def kill(self): pass\n"
                  "    def wait(self, timeout=None):\n"
                  "        if timeout is not None: raise subprocess.TimeoutExpired(self.args, timeout)\n"
                  "        return None\n"
                  "import ag2_sparrow.connect_common as cm; cm.TERM_GRACE_S = 0.2\n"
                  "sys.exit(cli.run(type('A', (), {})(), popen=Immortal))")
        from ag2_sparrow import task_io
        self.configure()
        launcher, child = self.launcher(driver=driver)
        os.kill(launcher.pid, signal.SIGINT)
        launcher.wait(timeout=30)                  # the child keeps the pipes, so no communicate() yet
        try:
            self.assertEqual(launcher.returncode, 130)
            self.assertTrue(self.alive(child), "the child is still there")
            self.assertEqual(task_io.lock_held(str(self.work)), "held", "and keeps the root fenced")
        finally:
            os.kill(child, signal.SIGKILL)
            self.wait_gone(child)
        _, err = launcher.communicate(timeout=30)
        self.assertIn("not confirmed reaped; exiting without releasing", err)

    def session_after(self, first_pid):
        rec = self.session()
        return None if rec is None or rec["pid"] == first_pid else rec


class ConnectEnvAndPaths(ConnectBase):
    def test_auth_status_runs_under_the_same_allowlist(self):
        self.configure()
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
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, "is a directory, not a supervisor file" in err), (1, True))
        self.assertFalse(cli.config_path().exists())
        self.configure()
        out = self.main("verify")[1]
        self.assertIn("FAIL supervisor", out)
        self.assertIn("is a directory", out)
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, "is a directory" in err), (1, True))
        self.assertTrue(unit.is_dir(), "left alone")
        unit.rmdir()
        unit.symlink_to(self.home / "gone")
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, f"remove it by hand (rm {unit})" in err), (1, True))
        self.assertIn("dangling symlink", self.main("verify")[1])
        rc, out, err = self.main("uninstall")
        self.assertEqual((rc, "dangling symlink" in err), (1, True))
        self.assertTrue(unit.is_symlink(), "left alone")

    def test_a_key_less_config_is_one_line_in_run_and_verify(self):
        cfg = self.configure()
        good = cfg.read_text()
        cfg.write_text(good.replace("INBOX_DIR=", "INBOX_WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks INBOX_DIR" in err), (2, True))
        self.assertNotIn("Traceback", err)
        self.assertIsNone(self.session())
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, "lacks INBOX_DIR" in out), (1, True))


class ConnectVerify(ConnectBase):
    def checks(self, out):
        return {line.split()[1].rstrip(":"): line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "FAIL")}

    def test_verify_passes_every_part_and_fails_the_broken_one(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        (self.home / "task-hmac.key").write_text("ab" * 32)
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, out, _ = self.main("verify")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.checks(out).values()), {"PASS"}, out)
        self.assertEqual(set(self.checks(out)), {"config", "claude", "login", "plugin", "inbox", "results", "hmac", "relay",
                                                 "supervisor"})
        self.assertIn(f"INFO job: {cli.SERVICE_NAME} active", out)
        self.assertIn(f"INFO session: {self.work} held by launcher pid {runner.pid()}", out)
        runner.stop_session()
        self.assertIn(f"INFO session: {self.work} free", self.main("verify")[1])
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
        with mock.patch.object(cm, "lock_held", return_value="unknown"):
            self.assertIn("INFO session: %s unknown (the lock of" % self.work, self.main("verify")[1])

    def test_verify_without_a_config_says_so(self):
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config", out)

    def test_device_env_path_is_the_shared_owners(self):
        from ag2_sparrow.adapters.hatch import cli as hatch_cli
        self.assertEqual(hatch_cli.device_env_path(), _dirs.device_env_path())
        self.assertEqual(_dirs.device_env_path(), self.relay)
        with mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": "/elsewhere/relay.env"}):
            self.assertEqual(_dirs.device_env_path(), Path("/elsewhere/relay.env"))


if __name__ == "__main__":
    unittest.main()
