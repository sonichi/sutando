#!/usr/bin/env python3
"""The claude-code adapter ships a well-formed Claude Code mod whose embedded
Python mirrors Sparrow's rules, its helper scripts write what Sparrow's parser
reads, and nothing in it names a host.

The mod's own `*.test.ts` needs the `claude` binary; this file is the part CI
can run with stdlib Python alone."""
from __future__ import annotations

import contextlib
import errno
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
import threading
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
try:
    listing = sorted(os.listdir(os.environ.get("AG2_INBOX_DIR", ".")))
except OSError as e:
    listing = ["ERROR %%s" %% e]
json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid(), "ppid": os.getppid(),
           "listing": listing},
          open(os.path.join(home, "claude-session.json"), "w"))
import signal, time
if read("claude-ignore-term", ""):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if read("claude-signal-parent", ""):
    os.kill(os.getppid(), getattr(signal, "SIG" + read("claude-signal-parent", "")))
time.sleep(float(read("claude-linger", "0")))
sys.exit(int(read("claude-exit", "0")))
'''
TOKEN = "tok-FAKE-RELAY-TOKEN-never-copied"
L = cli.LOCAL
STAND_IN = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import holder_lock, task_io\n"
            "install = %r\n"
            "if install: holder_lock.install_id = lambda: install\n"
            "o = task_io.acquire_lock(%r, %r); print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
UNPUBLISHED = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import holder_lock\n"
               "r = holder_lock.acquire(%r, install_id=holder_lock.install_id())\n"
               "print('held', flush=True); sys.stdin.read(); holder_lock.release(r.owner)")
OP_HOLDER = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm\n"
             "from ag2_sparrow.adapters.claude_code.cli import LOCAL\n"
             "with cm.operation_lock(LOCAL): print('held', flush=True); sys.stdin.read()")
NOT_LOADED = (113, "", 'Could not find service "ai.ag2.sparrow.claude-local" in domain for uid: 501\n')
SERVICE_TARGET = f"gui/{os.getuid()}/ai.ag2.sparrow.claude-local"


def SHOW(load, active, enabled):
    """`systemctl show -p LoadState,ActiveState,UnitFileState --value`'s three lines."""
    return (0, f"{load}\n{active}\n{enabled}\n", "")
def SHOWING(**words):
    """A `show` answer by the properties asked for (`-p A,B --value`), as systemctl answers."""
    return lambda cmd: (0, "".join(words[k] + "\n" for k in cmd[cmd.index("-p") + 1].split(",")), "")


START_VERBS = ("restart", "bootstrap", "kickstart")
STOP_VERBS = ("stop", "bootout")


class StubChild:
    """A Popen stand-in for in-process pins: a live `sleep` child gives it a real pid (a Linux pidfd
    needs one), `wait` ends it at once, and the spawn-time kwargs are kept for inspection."""

    def __init__(self, argv, **kw):
        self.args, self.kw, self.stdin = argv, kw, io.StringIO()
        self.proc = subprocess.Popen(["sleep", "30"])
        self.pid, self.returncode = self.proc.pid, None
        self.on_spawn()

    def on_spawn(self):
        pass

    def on_wait(self):
        pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.on_wait()
        self.proc.kill()
        self.proc.wait(timeout=30)
        self.returncode = 0
        return 0

    def send_signal(self, sig):
        self.proc.send_signal(sig)


def hold(root, name="first-launcher", script=STAND_IN, install=""):
    """A lock holder in another process (holder_lock allows one acquisition per process)."""
    p = subprocess.Popen([sys.executable, "-c", script % ((str(PKG), install, str(root), name) if script is STAND_IN
                                                             else (str(PKG), str(root)) if script is UNPUBLISHED
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
    could not run. With a root, a start verb runs a stand-in launcher (adapter `adapter`, install identity
    `install`) holding the root's lock, a stop verb ends it (after `stop_delay`), and the probes answer
    from the manager's own state unless the table says otherwise: `show` LoadState from the unit file or
    a `registered` job, ActiveState from the stand-in, UnitFileState from enable/disable; `print` likewise."""

    def __init__(self, table=None, default=0, root=None, adapter="claude-local", install="", registered=False,
                 stop_delay=0.0):
        self.table, self.default, self.calls, self.root = dict(table or {}), default, [], root
        self.session, self.adapter, self.install = None, adapter, install
        self.enabled, self.registered, self.stop_delay, self.timers = False, registered, stop_delay, []

    def start_session(self):
        self.stop_session()
        self.session = hold(self.root, self.adapter, install=self.install)

    def stop_session(self):
        for t in self.timers:
            t.join()
        self.timers.clear()
        if self.session is not None:
            unhold(self.session)
            self.session = None

    def end_session_later(self):
        session, self.session = self.session, None
        if session is not None:
            t = threading.Timer(self.stop_delay, unhold, (session,))
            t.start()
            self.timers.append(t)

    def verb(self, cmd):
        return cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))

    def answer(self, verb, cmd):
        if verb == "print":
            if self.session:
                return (0, f"\tpid = {self.session.pid}\n\tstate = running\n", "")
            return (0, "\tstate = waiting\n", "") if self.registered else NOT_LOADED
        if verb == "show":
            loaded = cm.service_path(L).exists() or self.registered
            words = {"LoadState": "loaded" if loaded else "not-found", "ActiveState": "active" if self.session else "inactive",
                     "SubState": "running" if self.session else "dead", "UnitFileState": "enabled" if self.enabled else "disabled"}
            props = cmd[cmd.index("-p") + 1].split(",")
            return (0, "".join(words[k] + "\n" for k in props), "")
        return self.default

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        verb = self.verb(cmd)
        got = self.table[(cmd[0], verb)] if (cmd[0], verb) in self.table else self.answer(verb, cmd)
        if callable(got):
            got = got(cmd)
        if got is None:
            return None
        rc, out, err = got if isinstance(got, tuple) else (got, "", "exit %s" % got)
        if rc == 0 and verb in ("enable", "disable"):
            self.enabled = verb == "enable"
            self.registered = self.registered and verb == "enable"
        if rc == 0 and verb == "bootout":
            self.registered = False
        if self.root and rc == 0:
            if verb in START_VERBS:
                self.start_session()
            elif verb in STOP_VERBS:
                self.end_session_later() if self.stop_delay else self.stop_session()
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
        env = {"HOME": str(self.home), "USER": "owner", "PATH": f"{self.bin}:/usr/bin:/bin", cm.MANAGED_VAR: "1",
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

    def runner(self, table=None, default=0, root=True, adapter="claude-local", **kw):
        r = Runner(table, default, str(self.work) if root else None, adapter, **kw)
        self.runners.append(r)
        return r

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def run_cli(self, popen=subprocess.Popen):
        """cli.run in-process with a popen of the test's choosing, stdout and stderr captured."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run(type("A", (), {})(), popen=popen)
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox),
                         "--claude-config-dir", str(self.home / ".claude-sutando"),
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


def task_io_lock_holder(root):
    from ag2_sparrow import task_io
    return task_io.lock_holder(root)


class ConnectRender(ConnectBase):
    def test_unit_and_plist_carry_the_marker_and_paths_only(self):
        exec_start = str(self.bin / cli.COMMAND)
        unit = cm.render_service(L, exec_start)
        self.assertEqual(unit.split("\n")[0], cli.MARKER)
        self.assertIn(f"ExecStart={exec_start} run\n", unit)
        for line in ("Restart=always", "RestartSec=10", "RestartPreventExitStatus=75", "WantedBy=default.target",
                     "KillMode=control-group", "TimeoutStopSec=30", f"Environment={cm.MANAGED_VAR}=1"):
            self.assertIn(line + "\n", unit)
        plist = cm.render_plist(L, exec_start, "/opt/bin:/usr/bin", "/var/log/x.log")
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER), plist[:200])
        self.assertTrue(plist.startswith("<?xml"), "the declaration stays first")
        body = plistlib.loads(plist.encode())
        self.assertEqual(body["ProgramArguments"], [exec_start, "run"])
        self.assertEqual((body["Label"], body["KeepAlive"], body["RunAtLoad"]), (cli.AGENT_LABEL, True, True))
        self.assertEqual(body["EnvironmentVariables"], {"PATH": "/opt/bin:/usr/bin", cm.MANAGED_VAR: "1"})
        self.assertEqual((body["AbandonProcessGroup"], body["ExitTimeOut"], body["ThrottleInterval"]), (False, 30, 30),
                         "launchd SIGTERMs the group on the launcher's death, bounds a bootout, respawns after 30 s")
        for text in (unit, plist, cli.render_config({"CLAUDE": "/x/claude", "INBOX_DIR": "/i"})):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("REMOTE_TASK_TOKEN", text)
        self.assertEqual([ln for ln in unit.splitlines() if ln.startswith("Environment=")], [f"Environment={cm.MANAGED_VAR}=1"],
                         "the unit carries paths in ExecStart and the one marker, no values")
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
        self.assertEqual(runner.verbs("systemctl"), ["show", "show-environment", "daemon-reload", "enable", "restart"],
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
        self.assertEqual(verbs[:4], ["show", "stop", "disable", "show"], "the job is stopped and proven inactive+disabled "
                         "before anything is written")
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
        self.assertEqual(next(c for c in runner.calls if c[1] == "bootout"), ["launchctl", "bootout", "--wait", SERVICE_TARGET],
                         "the single service target, the only form --wait applies to")
        self.assertEqual(runner.calls[0], ["launchctl", "print", SERVICE_TARGET])
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

    def test_confirmation_requires_this_installs_identity_too(self):
        other = self.runner(install="/elsewhere/site-packages/ag2_sparrow")
        rc, _, err = self.install_with(other)
        self.assertEqual(rc, 1, "same adapter, another install: not this manager's launcher")
        self.assertIn(f"did not acquire {self.work}", err)
        self.assertIn(f"held by launcher pid {other.pid()}", err, "held all along, by the wrong install")

    def test_fresh_holder_refuses_the_snapshotted_holder_and_takes_a_different_one(self):
        self.configure()
        first = hold(str(self.work), "claude-local")
        try:
            mine = task_io_lock_holder(str(self.work))
            with mock.patch.object(cm, "CONFIRM_TIMEOUT_S", 0.5):
                self.assertIsNone(cm._fresh_holder(L, str(self.work), mine), "the pre-apply holder itself is not fresh")
                self.assertEqual(cm._fresh_holder(L, str(self.work), {})["pid"], first.pid, "with no snapshot it is")
        finally:
            unhold(first)
        second = hold(str(self.work), "claude-local")
        try:
            self.assertEqual(cm._fresh_holder(L, str(self.work), mine)["pid"], second.pid, "a different live holder is")
        finally:
            unhold(second)

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
        for table in ({("systemctl", "stop"): 0, ("systemctl", "show"): SHOW("loaded", "active", "enabled")},
                      {("systemctl", "show"): (1, "", "Failed to connect to bus\n")},
                      {("systemctl", "stop"): None}):
            rc, _, err = self.install_with(self.runner(table), False, "--permission-mode", "plan")
            self.assertEqual(rc, 1, table)
            self.assertIn("nothing was applied", err)
            self.assertEqual(cli.config_path().read_text(), before, "the old config stands")
        for table in ({("systemctl", "show"): (1, "", "Failed to connect to bus\n")}, {("systemctl", "show"): None}):
            rc, _, err = self.install_with(self.runner(table), False, "--permission-mode", "plan")
            self.assertEqual((rc, "is unknown; nothing was applied" in err), (1, True), table)

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

    def test_a_tasks_symlink_is_refused_at_install_and_at_run(self):
        """A `tasks` that is itself a link would put the lock on this root and the session on the link's
        target: two locks for one inbox. Refused before any write, and by run through O_NOFOLLOW."""
        real = self.home / "elsewhere" / "tasks"
        real.mkdir(parents=True)
        self.work.mkdir(parents=True, exist_ok=True)
        link = self.work / "tasks"
        link.symlink_to(real)
        for extra in (("--inbox", str(link)), ("--work", str(self.work))):
            runner = self.runner()
            rc, _, err = self.install_with(runner, False, *extra)
            self.assertEqual(rc, 2, err)
            self.assertIn("is a symlink; tasks must be a real directory", err)
            self.assertFalse(cli.config_path().exists(), "nothing written")
            self.assertEqual(runner.calls, [], "the manager is never touched")
        from ag2_sparrow import task_io
        self.configure(INBOX_DIR=str(link))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "tasks/ under the held root is a symlink" in err), (2, True), "ERR=" + err)
        self.assertIsNone(self.session(), "no session started against the link's target")
        self.assertEqual(task_io.lock_held(str(self.work)), "free", "and the lock was released")

    def test_a_work_root_change_is_refused_with_uninstall_then_install(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        other = self.home / "other"
        before = cli.config_path().read_text()
        runner.calls.clear()
        rc, _, err = self.install_with(runner, False, "--work", str(other))
        self.assertEqual(rc, 1)
        self.assertIn(f"the work root is {self.work}, not {other}; run {cli.COMMAND} uninstall, then {cli.COMMAND} install", err)
        self.assertEqual(cli.config_path().read_text(), before)
        self.assertEqual(runner.calls, [], "refused before the manager is touched")
        self.assertFalse(other.exists(), "the proposed root is untouched: no tasks/ or results/ made")

    def test_the_child_runs_against_tasks_under_the_held_root_inode_not_the_path(self):
        self.configure()
        from ag2_sparrow import task_io
        held_tasks = os.stat(self.inbox)
        seen, moved, real_acquire = {}, self.home / "moved-away", cm.acquire_lock

        def acquire_then_replace_the_path(root, adapter, **kw):
            owner = real_acquire(root, adapter, **kw)
            os.rename(self.work, moved)                       # the path is replaced right after the lock check
            self.work.mkdir()
            (self.work / "tasks").mkdir()
            return owner

        class Child(StubChild):
            def on_spawn(self):
                st = os.stat(".")
                seen["cwd_is_held"] = (st.st_dev, st.st_ino) == (held_tasks.st_dev, held_tasks.st_ino)
                seen["sibling"] = task_io.lock_held(str(moved))
        with mock.patch.object(cm, "acquire_lock", side_effect=acquire_then_replace_the_path):
            rc = cli.run(type("A", (), {})(), popen=Child)
        self.assertEqual(rc, 0)
        self.assertTrue(seen["cwd_is_held"], "the spawn's cwd is tasks/ under the held root inode, not the new path")
        self.assertEqual(seen["sibling"], "held", "the moved-away root (the held inode) is what a sibling contends on")
        self.assertEqual(task_io.lock_held(str(moved)), "free", "released when the launcher is done")

    def test_replacing_the_root_at_the_popen_seam_does_not_change_what_the_session_lists(self):
        """The session's cwd is the held tasks inode and AG2_INBOX_DIR is '.', so a root replaced between
        the env's construction and the spawn changes nothing it lists."""
        self.configure()
        (self.inbox / "task-held.txt").write_text("task: in the held inbox\n")
        moved = self.home / "moved-away"

        def replace_then_spawn(argv, **kw):
            os.rename(self.work, moved)
            self.work.mkdir()
            (self.work / "tasks").mkdir()
            (self.work / "tasks" / "task-replacement.txt").write_text("task: in the replacement\n")
            return subprocess.Popen(argv, **kw)
        rc = cli.run(type("A", (), {})(), popen=replace_then_spawn)
        self.assertEqual(rc, 0)
        rec = self.session()
        self.assertEqual(rec["listing"], ["task-held.txt"], "the session lists the held inode, never the replacement")
        self.assertEqual((rec["env"]["AG2_INBOX_DIR"], rec["env"]["AG2_RESULTS_DIR"]), (".", "../results"))
        self.assertEqual(Path(rec["cwd"]).resolve(), (moved / "tasks").resolve())

    def test_a_config_without_a_root_is_one_line(self):
        self.configure()
        cfg = cli.config_path()
        cfg.write_text(cfg.read_text().replace("ROOT=", "WAS="))
        rc, _, err = self.main("run")
        self.assertEqual((rc, "lacks ROOT" in err), (2, True))


class ConnectOrphan(ConnectBase):
    """A held root with no registered job is an orphan: install refuses before any file is written."""

    def test_a_live_launcher_no_job_manages_refuses_a_reinstall_before_any_write_and_names_its_pid(self):
        """A real launcher holding the root with no registered job (a hand start, or a job the manager
        forgot) is an orphan: install probes, refuses, writes nothing, and names the launcher pid."""
        self.configure()
        from ag2_sparrow import task_io
        before = cli.config_path().read_text()
        launcher, child = ConnectLauncherProcess.launcher(self)
        try:
            self.assertEqual(task_io.lock_holder_pid(str(self.work)), launcher.pid)
            shutil.rmtree(self.results)
            runner = self.runner({})
            rc, out, err = self.install_with(runner, False, "--permission-mode", "default")
            self.assertEqual(rc, 1)
            self.assertFalse(self.results.exists(), "the refusal made no directory under the root")
            self.assertIn(f"{self.work} is held by a session no job of {cli.SERVICE_NAME} manages (launcher pid {launcher.pid})", err)
            self.assertIn("nothing was applied", err)
            self.assertNotIn(ConnectInstall.ACQUIRED, out)
            self.assertEqual(runner.verbs("systemctl"), ["show"], "probed, not applied")
            self.assertEqual(cli.config_path().read_text(), before, "config untouched")
            self.assertFalse(cli.service_path().exists(), "no unit written")
        finally:
            os.kill(launcher.pid, signal.SIGTERM)
            launcher.communicate(timeout=30)
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
            rc, _, err = self.install_with(self.runner({("systemctl", "show"): SHOW("loaded", "inactive", "enabled")}))
            self.assertEqual(rc, 1, "a registered but inactive job runs no holder either: an orphan")
            self.assertIn(f"no job of {cli.SERVICE_NAME} manages (launcher pid {holder.pid})", err)
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
            with mock.patch.object(cm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("uninstall")
            self.assertEqual((rc, "operation is in progress" in err), (1, True))
            self.assertTrue(cli.config_path().exists())
            with mock.patch.object(cm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("debug-run")
            self.assertEqual((rc, "operation is in progress" in err), (2, True))
            self.assertIsNone(self.session())
        finally:
            unhold(other)
        seen = {}

        class Child(StubChild):
            def on_spawn(self):
                seen["oplock_free_at_spawn"] = oplock_free()
        with mock.patch.object(cm, "_run", side_effect=self.runner()):
            self.assertEqual(self.main("uninstall")[0], 0, "the job is gone, so debug-run may run")
        self.configure()
        with mock.patch.object(cm, "_run", side_effect=Runner({})):
            rc = cli.debug_run(type("A", (), {})(), popen=Child)
        self.assertEqual(rc, 0)
        self.assertTrue(seen["oplock_free_at_spawn"], "debug-run releases the operation lock once the root lock is held")
        self.assertTrue(cm.oplock_path(L).exists(), "the mutex file is never unlinked")
        with mock.patch.object(cm, "_run", side_effect=self.runner({})):
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
        self.assertEqual(rec["argv"][0], str(self.claude))
        self.assertEqual(rec["ppid"], os.getpid(), "the session is the launcher's direct child: no trampoline")
        self.assertNotIn(cm.MANAGED_VAR, rec["env"], "the manager's marker is not passed on to the session")
        # macOS stamps __CF_USER_TEXT_ENCODING on every child; the fake's own Python coerces LC_CTYPE.
        env = {k: v for k, v in rec["env"].items() if k not in ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")}
        self.assertEqual(env["AG2_AUTOARM"], "1", "the plugin arms itself from this marker at session.start")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home / ".claude-sutando"))
        self.assertEqual((env["AG2_INBOX_DIR"], env["AG2_RESULTS_DIR"], env["AG2_HMAC_KEY_FILE"]),
                         (".", "../results", str(self.home / "task-hmac.key")), "task I/O bound to the held inode, by cwd")
        self.assertEqual(env["USER"], "owner", "macOS needs USER for the Keychain login")
        self.assertTrue(set(env) <= {"PATH", "HOME", "USER", "CLAUDE_CONFIG_DIR", "AG2_INBOX_DIR", "AG2_RESULTS_DIR",
                                     "AG2_HMAC_KEY_FILE", "AG2_AUTOARM"}, sorted(env))
        for secret in ("REMOTE_TASK_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_SECRET_ACCESS_KEY",
                       "SUTANDO_WORKSPACE_DIR", "AG2_DEVICE_ENV"):
            self.assertNotIn(secret, env)
        self.assertEqual(rec["cwd"], str(self.inbox))
        self.assertEqual(rec["listing"], [], "the inbox it lists is its cwd")
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

    def test_the_lock_is_held_and_recorded_before_the_child_which_gets_no_descriptor_and_no_group(self):
        """R1: the session is spawned directly: stdin a pipe, the env, close_fds, and nothing else (no
        pass_fds, no new session or process group); the record is published before it exists."""
        self.configure()
        from ag2_sparrow import task_io
        seen, work = {}, str(self.work)

        class Child(StubChild):
            def on_spawn(self):
                seen["at_spawn"] = task_io.lock_holder(work)        # publish-before-spawn

            def on_wait(self):
                seen["during_wait"] = task_io.lock_holder_pid(work)
                seen["stdin"] = self.stdin.getvalue()
                seen["kw"], seen["env"] = self.kw, self.kw["env"]
        rc = cli.run(type("A", (), {})(), popen=Child)
        self.assertEqual(rc, 0)
        self.assertEqual((seen["at_spawn"]["pid"], seen["at_spawn"]["live"], seen["at_spawn"]["adapter"],
                          seen["at_spawn"]["install_id"]), (os.getpid(), True, "claude-local", holder_lock.install_id()))
        self.assertEqual(seen["during_wait"], os.getpid(), "held by this launcher while the child runs")
        self.assertEqual(sorted(seen["kw"]), ["close_fds", "env", "stdin"], "the spawn's whole keyword set")
        self.assertEqual((seen["kw"]["close_fds"], seen["kw"]["stdin"]), (True, subprocess.PIPE))
        self.assertEqual(seen["stdin"], "", "nothing is written to stdin: the plugin arms itself")
        self.assertEqual(seen["env"]["AG2_AUTOARM"], "1")
        self.assertIsNone(task_io.lock_holder_pid(work), "released when the launcher is done")

    def test_a_probe_loop_beside_repeated_launcher_starts_never_forges_a_75(self):
        from ag2_sparrow import task_io
        self.configure()
        prober = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
                                   "print('probing', flush=True)\nwhile True: task_io.lock_held(%r)" % (str(PKG), str(self.work))],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(prober.stdout.readline().strip(), "probing")
            codes = [self.main("run")[0] for _ in range(25)]
        finally:
            prober.kill()
            prober.wait(timeout=30)
        self.assertEqual(codes, [0] * 25, "a probe's momentary flock is retried through, never reported as another holder")

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
        for show in (SHOW("loaded", "active", "enabled"), SHOW("loaded", "inactive", "enabled"), SHOW("loaded", "inactive", "disabled"),
                     SHOW("not-found", "inactive", "enabled")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): show})):
                rc, _, err = self.main("debug-run")
            self.assertEqual((rc, f"{cli.SERVICE_NAME} is registered" in err), (2, True), show)
            self.assertIsNone(self.session())
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): None})):
            rc, _, err = self.main("debug-run")
        self.assertEqual((rc, "unknown state" in err), (2, True))
        holder = hold(str(self.work))
        try:
            with mock.patch.object(cm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("debug-run")
        finally:
            unhold(holder)
        self.assertEqual(rc, 2)
        self.assertIn(f"held by launcher pid {holder.pid}; stop that session first", err)
        self.assertIsNone(self.session())
        with mock.patch.object(cm, "_run", side_effect=Runner({})):
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
        self.assertEqual(runner.verbs("systemctl"), ["show", "stop", "disable", "show", "daemon-reload", "show"])
        self.assertFalse(cli.service_path().exists() or cli.config_path().exists())
        self.assertIn(f"stopped: {cli.SERVICE_NAME}", out)
        self.assertLess(out.index(f"removed: {cli.service_path()}"), out.index(f"removed: {cli.config_path()}"), "config last")
        self.assertEqual(self.relay.read_text(), f"REMOTE_TASK_TOKEN={TOKEN}\n", "Sparrow's token file is untouched")
        self.assertTrue((self.plugin / ".claude-plugin" / "plugin.json").exists())
        with mock.patch.object(cm, "_run", side_effect=Runner()):
            self.assertIn("nothing to remove", self.main("uninstall")[1])

    def test_a_partial_systemd_teardown_keeps_the_config_and_completes_on_retry(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        for table, why in (({("systemctl", "daemon-reload"): 1}, "daemon-reload failed"),
                           ({("systemctl", "show"): SHOW("loaded", "inactive", "disabled")}, "still known to systemd (LoadState loaded)")):
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
            self.assertEqual(runner.verbs("systemctl"), ["show", "daemon-reload", "show"], "the retry finishes from where it stopped")
            self.assertFalse(cli.config_path().exists())
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        with mock.patch.object(cm, "_run", side_effect=self.runner({("systemctl", "show"): SHOW("loaded", "inactive", "enabled")})):
            rc, _, err = self.main("uninstall")
        self.assertEqual((rc, "is still inactive and enabled after the stop" in err), (1, True))
        self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        with mock.patch.object(cm, "_run", side_effect=self.runner()):
            self.assertEqual(self.main("uninstall")[0], 0)

    def test_a_registered_job_without_files_is_still_torn_down(self):
        self.configure()
        runner = self.runner(registered=True)
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("systemctl"), ["show", "stop", "disable", "show", "daemon-reload", "show"],
                         "the manager is probed and the job stopped, disabled, reloaded away")
        self.assertFalse(cli.config_path().exists())
        with mock.patch.object(cm, "_run", side_effect=Runner()):
            self.assertIn("nothing to remove", self.main("uninstall")[1])
        runner = self.runner(registered=True)
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual((rc, runner.verbs("systemctl")), (0, ["show", "stop", "disable", "show", "daemon-reload", "show"]),
                         "no files at all, a registered job: still torn down")
        self.assertNotIn("nothing to remove", out)
        mac = self.runner(registered=True)
        with mock.patch.object(cm, "is_macos", return_value=True), mock.patch.object(cm, "_run", side_effect=mac):
            rc, out, _ = self.main("uninstall")
        self.assertEqual((rc, mac.calls), (0, [["launchctl", "print", SERVICE_TARGET], ["launchctl", "bootout", "--wait", SERVICE_TARGET],
                                               ["launchctl", "print", SERVICE_TARGET]]))

    def test_uninstall_waits_a_bounded_time_for_the_root_to_free_after_the_stop(self):
        runner = self.runner(stop_delay=0.6)
        self.assertEqual(self.install_with(runner)[0], 0)
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 0, "the launcher reaps within the bound: " + err)
        self.assertFalse(cli.config_path().exists())

    def test_launchd_boots_out_with_wait_and_requires_the_documented_not_found(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner, True)[0], 0)
        runner.calls.clear()
        with mock.patch.object(cm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.verbs("launchctl"), ["print", "bootout", "print"])
        self.assertEqual(runner.calls[1], ["launchctl", "bootout", "--wait", SERVICE_TARGET])
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
            self.assertIn(f"Manual recovery: 1. stop the job by hand: launchctl bootout --wait {SERVICE_TARGET}", err)
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
            with mock.patch.object(cm, "_run", side_effect=Runner({})):
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
        with mock.patch.object(cm, "_run", side_effect=Runner({})):
            self.assertEqual(self.main("uninstall")[0], 0, "re-run once the root is free")
        self.assertFalse(cli.config_path().exists())

    def test_a_held_root_with_no_live_launcher_in_the_record_names_lsof_on_the_lock_file(self):
        """A holder between its flock and its record (or one whose record is gone) is the only way a
        held lock names no live launcher now that no child ever holds it; the recovery names lsof."""
        from ag2_sparrow import task_io
        self.configure()
        cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
        holder = hold(str(self.work), script=UNPUBLISHED)
        try:
            with mock.patch.object(cm, "_run", side_effect=Runner({})):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn("held by an unrecorded holder", err)
            self.assertNotIn("child", err)
            self.assertIn(f"lsof {task_io.lock_path(str(self.work))}", err)
            self.assertTrue(cli.config_path().exists())
        finally:
            unhold(holder)

    def test_an_unknown_stop_probe_or_a_lock_error_refuses(self):
        self.assertEqual(self.install_with(self.runner())[0], 0)
        self.runners[-1].stop_session()
        for table in ({("systemctl", "stop"): 0, ("systemctl", "show"): SHOW("loaded", "active", "enabled")},
                      {("systemctl", "stop"): None}):
            with mock.patch.object(cm, "_run", side_effect=Runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn("Manual recovery", err)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        for table in ({("systemctl", "stop"): 1, ("systemctl", "show"): (1, "", "Failed to connect to bus\n")},
                      {("systemctl", "show"): None}):
            with mock.patch.object(cm, "_run", side_effect=Runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual((rc, "state is unknown; nothing was removed" in err), (1, True), table)
            self.assertTrue(cli.service_path().exists() and cli.config_path().exists())
        with mock.patch.object(cm, "_run", side_effect=self.runner()), mock.patch.object(cm, "lock_held", return_value="unknown"):
            rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertIn("unknown (the lock of", err)
        self.assertTrue(cli.config_path().exists(), "a lock error keeps everything")
        with mock.patch.object(cm, "_run", side_effect=self.runner({("systemctl", "stop"): 1})):
            self.assertEqual(self.main("uninstall")[0], 0, "stop failed but the unit is confirmed inactive and disabled")

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

    def test_a_dangling_a_directory_or_an_unparsable_config_is_one_line_and_kept(self):
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
        for bad in (good + "PERMISSION_MODE='unclosed\n", good.replace("INBOX_DIR=", "INBOX_WAS="),
                    good.replace(f"ROOT={self.work}", "ROOT=relative/work")):
            cfg.write_text(bad)
            scratch = self.home / "cwd"
            scratch.mkdir(exist_ok=True)
            was = os.getcwd()
            os.chdir(scratch)                                   # a relative ROOT would be made under the cwd
            try:
                rc, _, err = self.main("run")
            finally:
                os.chdir(was)
            self.assertEqual(rc, 2, bad)
            self.assertEqual(os.listdir(scratch), [], "refused by the loader, before anything under the cwd is made")
            self.assertIsNone(self.session())
            rc, _, err = self.install_with(self.runner())
            self.assertEqual(rc, 1, "a marked but malformed config is never replaced: " + err)
            self.assertIn("remove it by hand", err)
            self.assertEqual(cfg.read_text(), bad, "preserved")
            self.assertFalse(cli.service_path().exists())
            rc, _, err = self.main("uninstall")
            self.assertEqual((rc, cfg.read_text()), (1, bad), "uninstall keeps it too: " + err)

    def test_a_malformed_owned_config_fails_closed_with_an_old_root_orphan(self):
        """The persisted root is the only anchor; a config that cannot name it stops install and uninstall
        before any mutation, while a holder on the old root stays untouched."""
        from ag2_sparrow import task_io
        cfg = self.configure()
        cm._write(cli.service_path(), cm.render_service(L, "/x"), 0o644)
        good = cfg.read_text()
        bad = good.replace(f"ROOT={self.work}", "ROOT='unmatched")
        cfg.write_text(bad)
        holder = hold(str(self.work), "claude-local")
        try:
            other = self.home / "other"
            rc, _, err = self.install_with(self.runner(), False, "--work", str(other))
            self.assertEqual(rc, 1)
            self.assertIn("is not parsable", err)
            self.assertFalse(other.exists())
            self.assertEqual(cfg.read_text(), bad, "preserved")
            with mock.patch.object(cm, "_run", side_effect=self.runner()) as r:
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1)
            self.assertIn("is not parsable", err)
            self.assertTrue(cli.service_path().exists() and cfg.exists(), "nothing removed")
            self.assertEqual(r.side_effect.calls, [], "the manager is not touched")
            self.assertEqual(task_io.lock_held(str(self.work)), "held", "the old root's holder is untouched")
        finally:
            unhold(holder)
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
            self.assertEqual(cm.stop_command(L), f"launchctl bootout --wait {SERVICE_TARGET}")
        with mock.patch.object(cm, "is_macos", return_value=False):
            self.assertEqual(cm.stop_command(L), f"systemctl --user stop {cli.SERVICE_NAME}")

    def write_conf(self, text):
        p = self.home / "some.conf"
        p.write_text(text)
        return p

    def test_probes_read_explicit_outputs_only(self):
        for got, want in ((SHOW("loaded", "active", "enabled"), "active"), (SHOW("loaded", "activating", "enabled"), "active"),
                          (SHOW("not-found", "inactive", "disabled"), "absent"), (SHOW("not-found", "failed", ""), "absent"),
                          (SHOW("loaded", "inactive", "disabled"), "registered"), (SHOW("not-found", "inactive", "enabled"), "registered"),
                          (SHOW("loaded", "inactive", "enabled"), "registered"), ((1, "", "Failed to connect to bus\n"), "unknown"),
                          (SHOW("odd", "inactive", "disabled"), "unknown"), (None, "unknown")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): got})) as r:
                self.assertEqual(cm.unit_active(L), want, got)
            self.assertEqual(r.side_effect.calls[0], ["systemctl", "--user", "--no-ask-password", "show", "-p",
                                                      "LoadState,ActiveState,UnitFileState", "--value", cli.SERVICE_NAME])
        for got, want in ((0, "active"), (NOT_LOADED, "absent"), ((113, "", "Bad request\n"), "unknown"), (None, "unknown")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): got})) as r:
                self.assertEqual(cm.agent_loaded(L), want, got)
            self.assertEqual(r.side_effect.calls[0], ["launchctl", "print", SERVICE_TARGET])
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
                self.assertEqual((rec["pid"], rec["live"], rec["adapter"], rec["install_id"]),
                                 (holder.pid, True, "claude-local", holder_lock.install_id()))
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


class ConnectLauncherProcess(ConnectBase):
    """Every launcher exit stops and reaps its direct child, as real processes; a SIGKILLed launcher
    frees the lock at once and leaves the session to the manager (option A)."""

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
        self.assertEqual(self.session()["ppid"], launcher.pid, "the session is the launcher's direct child")
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

    def test_a_sigkilled_launcher_frees_the_lock_at_once_and_the_managers_ordering_is_the_guard(self):
        """R9, option A: no fence survives the launcher. After SIGKILL the lock reads free with the
        session still alive and `verify` says so beside the manager's state; a second `run` is NOT
        refused by the lock (the manager's ordering and the run gate are the guard). The test stops
        the session itself, as launchd's group SIGTERM or systemd's cgroup teardown would."""
        from ag2_sparrow import task_io
        self.configure()
        launcher, child = self.launcher()
        work = str(self.work)
        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=30)
        try:
            self.assertTrue(self.alive(child), "the session outlives its launcher")
            self.assertEqual(task_io.lock_held(work), "free", "the flock died with the launcher: no fence")
            self.assertFalse(task_io.lock_holder(work).get("live", False), "the record names a dead launcher")
            respawning = SHOWING(LoadState="loaded", ActiveState="activating", SubState="auto-restart", UnitFileState="enabled")
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): respawning})):
                out = self.main("verify")[1]
            self.assertIn(f"INFO session: {work} restarting (manager activating/auto-restart, lock free)", out, out)
            self.assertNotIn(f"INFO session: {work} free", out, "a free lock under a live job never reads bare free")
            (self.home / "claude-linger").write_text("0")
            rc, _, err = self.main("run")
            self.assertEqual(rc, 0, "a second launcher is not refused by the lock: " + err)
            self.assertIsNotNone(self.session_after(child), "and it ran a second session beside the stray")
            self.assertTrue(self.alive(child), "which the lock did nothing about: that is the manager's ordering")
        finally:
            os.kill(child, signal.SIGKILL)
            self.wait_gone(child)
            launcher.stdout.close()
            launcher.stderr.close()
        self.assertEqual(task_io.lock_held(work), "free")

    def test_an_unconfirmed_kill_exits_within_the_bound_and_the_lock_goes_with_the_launcher(self):
        """A kill that does not take: every timed wait expires; the launcher signals the handle once
        each (terminate, then kill), waits the bound, exits with the signal's code, and the lock is
        free, since only the launcher ever held it: the survivor is the manager's to clean."""
        driver = ("import time\n"
                  "class Immortal(subprocess.Popen):\n"
                  "    def send_signal(self, sig): print('signal %d called' % sig, file=sys.stderr, flush=True)\n"
                  "    def wait(self, timeout=None):\n"
                  "        if timeout is not None: raise subprocess.TimeoutExpired(self.args, timeout)\n"
                  "        time.sleep(600)\n"
                  "import ag2_sparrow.connect_common as cm; cm.TERM_GRACE_S = 0.2; cm.REAP_BOUND_S = 0.5\n"
                  "cm._pidfd = lambda child: None\n"
                  "sys.exit(cli.run(type('A', (), {})(), popen=Immortal))")
        from ag2_sparrow import task_io
        self.configure()
        for sig, want in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            launcher, child = self.launcher(driver=driver)
            started = time.monotonic()
            os.kill(launcher.pid, sig)
            launcher.wait(timeout=30)                  # the child keeps the pipes, so no communicate() yet
            elapsed = time.monotonic() - started
            try:
                self.assertEqual(launcher.returncode, want, sig)
                self.assertLess(elapsed, 5.0, "grace + bound, not forever")
                self.assertTrue(self.alive(child), "the child is still there")
                self.assertEqual(task_io.lock_held(str(self.work)), "free", "the lock went with the launcher")
            finally:
                os.kill(child, signal.SIGKILL)
                self.wait_gone(child)
            _, err = launcher.communicate(timeout=30)
            self.assertEqual((err.count("signal 15 called"), err.count("signal 9 called")), (1, 1), err)
            self.assertIn("did not die within 0 s of the kill; what is left is the manager's to clean", err)
            self.assertNotIn("child reaped", err, "never claimed without a confirmed wait")
            (self.home / "claude-session.json").unlink()

    def session_after(self, first_pid):
        rec = self.session()
        return None if rec is None or rec["pid"] == first_pid else rec

    def test_a_forced_stop_reaches_a_direct_child_that_ignores_sigterm(self):
        """The kill after the grace targets the unreaped direct child itself: a session that ignores
        SIGTERM is killed, reaped, and the launcher exits 143 with the lock free."""
        from ag2_sparrow import task_io
        driver = ("import ag2_sparrow.connect_common as cm; cm.TERM_GRACE_S = 0.5\n"
                  "sys.exit(cli.main(['run']))")
        self.configure()
        (self.home / "claude-ignore-term").write_text("1")
        launcher, child = self.launcher(driver=driver)
        self.assertEqual(self.session()["ppid"], launcher.pid, "the session is the direct child")
        os.kill(launcher.pid, signal.SIGTERM)
        _, err = launcher.communicate(timeout=30)
        try:
            self.assertEqual(launcher.returncode, 143, err)
            self.assertTrue(self.wait_gone(child, 2), "the session that ignored SIGTERM was killed")
            self.assertIn("child exited -9", err)
            self.assertIn("stopped; child reaped", err)
            self.assertEqual(task_io.lock_held(str(self.work)), "free")
        finally:
            if self.alive(child):
                os.kill(child, signal.SIGKILL)

    def test_an_inherited_ignored_sigchld_is_reset_so_the_child_is_reapable(self):
        """SIGCHLD=SIG_IGN inherited from the manager's environment would let the kernel reap the child
        before wait() could, and its exit code would read 0; the launcher resets it before the spawn."""
        driver = ("import signal; signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
                  "sys.exit(cli.main(['run']))")
        self.configure()
        (self.home / "claude-exit").write_text("3")
        launcher, child = self.launcher(linger=0.3, driver=driver)
        _, err = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 3, "the child's own exit code, so it was reaped by us: " + err)
        self.assertIn("child exited 3", err)

    def test_sigint_is_an_explicit_stop_path_every_time(self):
        """Repeated SIGINT: 130 and a reaped, released root each run, never a KeyboardInterrupt escaping
        as -2. AG2_SPARROW_STRESS_N runs (default 5)."""
        self.configure()
        for i in range(int(os.environ.get("AG2_SPARROW_STRESS_N", "5"))):
            launcher, child = self.launcher()
            os.kill(launcher.pid, signal.SIGINT)
            _, err = launcher.communicate(timeout=30)
            self.assertEqual(launcher.returncode, 130, f"run {i}: {err}")
            self.assert_reaped_and_released(child, str(self.work))
            (self.home / "claude-session.json").unlink()


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


class ConnectStopPath(ConnectBase):
    """R2 and R3, in-process: SIGINT and SIGTERM are one deterministic stop path whose handler only
    records; the adapter signals only its unreaped direct child, by pidfd or handle, never after wait()."""

    def session_signals(self, name, linger="30", **knobs):
        self.configure()
        (self.home / "claude-signal-parent").write_text(name)
        (self.home / "claude-linger").write_text(linger)
        for key, value in knobs.items():
            (self.home / key).write_text(value)

    def assert_session_reaped_and_free(self):
        from ag2_sparrow import task_io
        self.assertTrue(wait_gone(self.session()["pid"]), "the session is gone")
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_handlers_go_directly_over_the_inherited_dispositions_and_an_inherited_sig_ign_still_stops(self):
        """No SIG_DFL step on the way in, so a signal in that window has no default action; an inherited
        SIG_IGN is replaced, so bootout, systemctl stop and Ctrl-C always reach the handler: 130."""
        installed, real = [], signal.signal

        def spy(sig, handler):
            installed.append((sig, handler))
            return real(sig, handler)
        was = signal.signal(signal.SIGINT, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGINT, was)
        self.session_signals("INT", linger="3")
        with mock.patch.object(cm.signal, "signal", side_effect=spy):
            rc, _, err = self.main("run")
        self.assertEqual(rc, 130, err)
        self.assertIn("interrupted; child reaped", err)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN, "the inherited disposition is restored after")
        stop_sigs = [(sig, h) for sig, h in installed if sig in (signal.SIGINT, signal.SIGTERM)]
        self.assertEqual([sig for sig, _ in stop_sigs[:2]], [signal.SIGTERM, signal.SIGINT], "installed, then restored")
        self.assertTrue(all(callable(h) for _, h in stop_sigs[:2]), "no SIG_DFL step: the handler goes straight over "
                        "the inherited disposition; what follows is the restore")
        self.assertEqual(stop_sigs[2:], [(signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.SIG_IGN)], "restored as found")
        self.assert_session_reaped_and_free()

    def test_the_handler_only_records_the_first_signal_and_an_ignored_sigchld_is_reset(self):
        state = cm.stop_state()
        was_chld = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGCHLD, was_chld)
        before = cm.stop_handlers(state)
        child, sent = StubChild([]), []
        try:
            self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_DFL, "reset before any spawn, so wait() reaps")
            state["child"] = child
            child.send_signal = lambda sig: sent.append(sig)
            handler = signal.getsignal(signal.SIGTERM)
            self.assertIs(signal.getsignal(signal.SIGINT), handler, "one handler for both")
            handler(signal.SIGTERM, None)
            handler(signal.SIGINT, None)
            self.assertEqual(state["sig"], signal.SIGTERM, "the first signal is the one recorded")
            self.assertEqual(sent, [], "the handler signals nothing: terminate, kill, wait and release are the loop's")
            self.assertIsNone(child.poll())
        finally:
            child.wait()
            cm.restore_handlers(state, before)
        self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_IGN, "restored with the rest")
        self.assertIsNone(state["installed"])

    def test_a_stop_landing_inside_popen_is_neither_raised_nor_lost(self):
        """The handle is published after Popen returned and the pidfd decision was made, and the loop
        re-checks a stop recorded meanwhile: 130, the child reaped, 'reaped' only after the wait."""
        from ag2_sparrow import task_io
        seen = {}

        class Inside(subprocess.Popen):
            def __init__(self, argv, **kw):
                os.kill(os.getpid(), signal.SIGINT)
                super().__init__(argv, **kw)
                seen["child"] = self

            def wait(self, timeout=None):
                rc = super().wait(timeout=timeout)
                print("WAIT-RETURNED", file=sys.stderr)
                return rc
        self.configure()
        (self.home / "claude-linger").write_text("30")
        rc, _, err = self.run_cli(Inside)
        self.assertEqual(rc, 130, err)
        self.assertIn("child exited -15", err, "terminated by the loop once the handle existed")
        self.assertEqual(err.count("WAIT-RETURNED"), 1)
        self.assertLess(err.index("WAIT-RETURNED"), err.index("interrupted; child reaped"), "reaped is said after the wait")
        self.assertEqual(seen["child"].returncode, -15)
        self.assertTrue(wait_gone(seen["child"].pid))
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_a_stop_before_the_spawn_starts_no_session(self):
        from ag2_sparrow import task_io
        real = cm.acquire_lock

        def stop_then_acquire(*a, **kw):
            os.kill(os.getpid(), signal.SIGTERM)
            return real(*a, **kw)
        self.configure()
        with mock.patch.object(cm, "acquire_lock", side_effect=stop_then_acquire):
            rc, _, err = self.main("run")
        self.assertEqual(rc, 143, err)
        self.assertIn("stopped before the session started", err)
        self.assertIsNone(self.session())
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_every_signal_targets_the_unreaped_direct_child_by_handle_or_pidfd_and_none_after_the_wait(self):
        """The shim: every signal's target kind (handle | pidfd | pid | group) and whether the child was
        already reaped. Never a group, never a bare pid, nothing after the reap; terminate then kill
        for a child that ignores SIGTERM. A mutant that signals after wait() lands here."""
        seen, log = {}, []
        real_kill, real_killpg = os.kill, os.killpg

        def reaped():
            return seen["child"].returncode is not None if "child" in seen else None

        def kill(pid, sig):
            log.append(("pid", sig, reaped()))
            return real_kill(pid, sig)

        def killpg(pgid, sig):
            log.append(("group", sig, reaped()))
            return real_killpg(pgid, sig)

        def send(self_, sig):
            log.append(("handle", sig, self_.returncode is not None))
            self_.poll()
            if self_.returncode is None:
                real_kill(self_.pid, sig)

        def popen(argv, **kw):
            seen["child"] = subprocess.Popen(argv, **kw)
            return seen["child"]
        patches = [mock.patch.object(os, "kill", kill), mock.patch.object(os, "killpg", killpg),
                   mock.patch.object(subprocess.Popen, "send_signal", send), mock.patch.object(cm, "TERM_GRACE_S", 0.3)]
        if hasattr(signal, "pidfd_send_signal"):
            real_pidfd = signal.pidfd_send_signal

            def pidfd_send(fd, sig, *a):
                log.append(("pidfd", sig, reaped()))
                return real_pidfd(fd, sig, *a)
            patches.append(mock.patch.object(signal, "pidfd_send_signal", pidfd_send))
        self.session_signals("TERM", **{"claude-ignore-term": "1"})
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            rc, _, err = self.run_cli(popen)
        self.assertEqual(rc, 143, err)
        signalled = [entry for entry in log if entry[1] != 0]
        self.assertEqual([sig for _, sig, _ in signalled], [signal.SIGTERM, signal.SIGKILL], signalled)
        self.assertTrue({kind for kind, _, _ in signalled} <= {"handle", "pidfd"}, signalled)
        self.assertEqual([r for _, _, r in signalled], [False, False], "never after the reap")
        self.assertIsNotNone(seen["child"].returncode, "reaped by the loop")
        self.assertIn("child exited -9", err)
        self.assert_session_reaped_and_free()

    def test_a_pidfd_open_failure_stops_the_session_bounded_and_refuses(self):
        """The chosen failure path: ENOSYS, EMFILE or anything from pidfd_open is a bounded stop of the
        child just spawned, then exit 2 with one line, the lock released. Patched to raise, so it runs
        here on macOS too (where the real decision is the handle fallback)."""
        from ag2_sparrow import task_io
        self.configure()
        (self.home / "claude-linger").write_text("30")
        seen = {}

        def popen(argv, **kw):
            seen["child"] = subprocess.Popen(argv, **kw)
            return seen["child"]
        with mock.patch.object(cm.os, "pidfd_open", side_effect=OSError(errno.ENOSYS, "nope"), create=True):
            rc, _, err = self.run_cli(popen)
        self.assertEqual(rc, 2, err)
        self.assertIn("refusing to start: pidfd_open failed (nope); the session was stopped and reaped", err)
        self.assertEqual(seen["child"].returncode, -15, "the child just spawned was terminated and reaped")
        self.assertTrue(wait_gone(seen["child"].pid))
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_with_a_pidfd_every_signal_goes_through_it_and_it_is_closed_on_every_path(self):
        """Linux's path (simulated where there is no pidfd): signals go through pidfd_send_signal, the
        child's number is never used, and the descriptor is closed on the normal path and on a
        post-spawn exception alike."""
        seen, sent, closed, real_close = {}, [], [], os.close

        def popen(argv, **kw):
            seen["child"] = subprocess.Popen(argv, **kw)
            return seen["child"]

        def pidfd_open(pid):
            self.assertEqual(pid, seen["child"].pid, "opened on the direct child, right after the spawn")
            seen["pidfd"] = os.open("/dev/null", os.O_RDONLY)
            return seen["pidfd"]

        def pidfd_send(fd, sig, *a):
            sent.append((fd, sig))
            os.kill(seen["child"].pid, sig)

        def close(fd):
            closed.append(fd)
            real_close(fd)
        self.session_signals("TERM")
        with mock.patch.object(os, "pidfd_open", pidfd_open, create=True), \
                mock.patch.object(signal, "pidfd_send_signal", pidfd_send, create=True), \
                mock.patch.object(os, "close", close):
            rc, _, err = self.run_cli(popen)
        self.assertEqual(rc, 143, err)
        self.assertEqual(sent, [(seen["pidfd"], signal.SIGTERM)], "through the pidfd, once; nothing after the wait")
        self.assertIn(seen["pidfd"], closed, "closed on the way out")
        self.assert_session_reaped_and_free()

        class Faulty(subprocess.Popen):
            def wait(self, timeout=None):
                raise RuntimeError("boom after spawn")

        def faulty(argv, **kw):
            seen["child"] = Faulty(argv, **kw)
            return seen["child"]
        closed.clear()
        (self.home / "claude-session.json").unlink()
        with mock.patch.object(os, "pidfd_open", pidfd_open, create=True), \
                mock.patch.object(signal, "pidfd_send_signal", pidfd_send, create=True), \
                mock.patch.object(os, "close", close), self.assertRaises(RuntimeError):
            self.run_cli(faulty)
        self.assertIn(seen["pidfd"], closed, "closed on the exception path too")
        self.assertTrue(wait_gone(seen["child"].pid), "and the child was stopped before the re-raise")

    def test_the_handle_is_published_only_after_popen_returned_and_the_pidfd_decision(self):
        self.configure()
        state, seen = cm.stop_state(), {}

        class Child(StubChild):
            def on_spawn(self):
                seen["at_spawn"] = state["child"]

        def pidfd(child):
            seen["at_pidfd"] = state["child"]
            return None
        inbox_fd = os.open(self.inbox, os.O_RDONLY | os.O_DIRECTORY)
        with mock.patch.object(cm, "_pidfd", pidfd):
            rc = cm.supervise(L, [str(self.claude)], {}, Child, state, inbox_fd)
        self.assertEqual(rc, 0)
        self.assertIsNone(seen["at_spawn"], "no handle while Popen runs")
        self.assertIsNone(seen["at_pidfd"], "none while the pidfd decision is made")
        self.assertIsInstance(state["child"], Child, "published after both")
        with self.assertRaises(OSError):
            os.fstat(inbox_fd)       # the tasks descriptor is closed once the spawn is over
        self.assertIsNone(state["installed"], "the handlers it installed are restored")


class ConnectCwdAndThreads(ConnectBase):
    """R4: the launcher fchdirs to the held tasks descriptor around the spawn and restores its cwd; the
    cwd is process-wide, so it refuses to spawn with any other thread alive."""

    def test_the_spawn_runs_under_the_held_tasks_fd_and_the_launchers_cwd_is_restored(self):
        self.configure()
        scratch = self.home / "launcher-cwd"
        scratch.mkdir()
        was = os.getcwd()
        os.chdir(scratch)
        self.addCleanup(os.chdir, was)
        seen = {}

        class Child(StubChild):
            def on_spawn(self):
                seen["cwd"] = os.getcwd()
        self.assertEqual(cli.run(type("A", (), {})(), popen=Child), 0)
        self.assertTrue(os.path.samefile(seen["cwd"], self.inbox), "spawned with cwd = the held tasks directory")
        self.assertTrue(os.path.samefile(os.getcwd(), scratch), "and the launcher's cwd restored")
        self.assertEqual(self.main("run")[0], 0)
        self.assertEqual(self.session()["cwd"], str(self.inbox))
        self.assertTrue(os.path.samefile(os.getcwd(), scratch))
        (self.home / "claude-linger").write_text("30")
        with mock.patch.object(cm.os, "pidfd_open", side_effect=OSError(errno.EMFILE, "full"), create=True):
            self.assertEqual(self.main("run")[0], 2)
        self.assertTrue(os.path.samefile(os.getcwd(), scratch), "restored on the refusal path too")
        wait_gone(self.session()["pid"])

    def test_a_launcher_with_another_thread_alive_refuses_to_spawn(self):
        from ag2_sparrow import task_io
        self.configure()
        gate = threading.Event()
        t = threading.Thread(target=gate.wait)
        t.start()
        try:
            rc, _, err = self.main("run")
        finally:
            gate.set()
            t.join()
        self.assertEqual(rc, 2, err)
        self.assertIn("refusing to start: the launcher runs 2 threads and the cwd switch around the spawn needs exactly one", err)
        self.assertIsNone(self.session())
        self.assertEqual(task_io.lock_held(str(self.work)), "free", "released on the refusal")


class ConnectEntryPoints(ConnectBase):
    """R5: `run` is the manager's entrypoint, gated on the marker the unit sets; `debug-run` is the
    human path; the marker never reaches the session."""

    def test_a_bare_run_refuses_naming_debug_run_and_a_managed_run_starts(self):
        self.configure()
        with mock.patch.dict(os.environ):
            os.environ.pop(cm.MANAGED_VAR)
            rc, _, err = self.main("run")
        self.assertEqual(rc, 2)
        self.assertIn(f"error: {cli.COMMAND} run is the manager's entrypoint ({cm.MANAGED_VAR}=1 in the unit's environment); "
                      f"for a session by hand run {cli.COMMAND} debug-run", err)
        self.assertIsNone(self.session(), "no session, no lock taken")
        with mock.patch.dict(os.environ, {cm.MANAGED_VAR: "0"}):
            self.assertEqual(self.main("run")[0], 2, "the value is 1, nothing else")
        self.assertIsNone(self.session())
        rc, _, err = self.main("run")
        self.assertEqual(rc, 0, "managed: " + err)
        self.assertIsNotNone(self.session())
        (self.home / "claude-session.json").unlink()
        with mock.patch.dict(os.environ), mock.patch.object(cm, "_run", side_effect=Runner({})):
            os.environ.pop(cm.MANAGED_VAR)
            rc, _, err = self.main("debug-run")
        self.assertEqual(rc, 0, "a bare debug-run is the human path: " + err)
        self.assertIsNotNone(self.session())

    def test_the_marker_never_reaches_the_session_env(self):
        self.configure()
        self.assertEqual(self.main("run")[0], 0)
        self.assertNotIn(cm.MANAGED_VAR, self.session()["env"])
        cfg = cli.read_config(cli.config_path())
        self.assertNotIn(cm.MANAGED_VAR, cli.child_env(cfg, {"PATH": "/bin", cm.MANAGED_VAR: "1"}), "an allowlist")
        self.assertNotIn(cm.MANAGED_VAR, cli.ENV_KEEP)

    def test_help_says_the_gate_is_a_guard_not_a_boundary_and_what_a_killed_debug_run_leaves(self):
        for cmd, phrases in (("run", ["refused unless AG2_SPARROW_MANAGED=1 is in the environment, as the unit sets it",
                                      "a guard against an accidental hand start, not a security boundary", "use debug-run by hand"]),
                             ("debug-run", ["No manager owns its tree", "a SIGKILLed debug-run leaves its session alive with the "
                                            "root free, yours to stop"])):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
                cli.main([cmd, "--help"])
            self.assertEqual(e.exception.code, 0)
            text = " ".join(out.getvalue().split())
            for phrase in phrases:
                self.assertIn(phrase, text, cmd)


class ConnectOccupancy(ConnectBase):
    """R6: `verify` reports the manager's own state beside the lock, so a free lock under a live or
    respawning job never reads bare 'free'."""

    def test_verify_reads_the_lock_beside_the_managers_state(self):
        self.configure()
        work = str(self.work)
        restarting = SHOWING(LoadState="loaded", ActiveState="activating", SubState="auto-restart", UnitFileState="enabled")
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): restarting})):
            out = self.main("verify")[1]
        self.assertIn(f"INFO session: {work} restarting (manager activating/auto-restart, lock free)", out)
        self.assertNotIn(f"INFO session: {work} free", out)
        self.assertIn("INFO systemd ordering is asserted from KillMode=control-group and not witnessed", out)
        holder = hold(work, "claude-local")
        try:
            running = SHOWING(LoadState="loaded", ActiveState="active", SubState="running", UnitFileState="enabled")
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): running})):
                out = self.main("verify")[1]
            self.assertIn(f"INFO session: {work} held by launcher pid {holder.pid} (manager active/running)", out)
            with mock.patch.object(cm, "_run", side_effect=Runner({})):
                self.assertIn(f"INFO session: {work} held by launcher pid {holder.pid} (manager absent)", self.main("verify")[1])
        finally:
            unhold(holder)
        with mock.patch.object(cm, "_run", side_effect=Runner({})):
            self.assertIn(f"INFO session: {work} free (no job)", self.main("verify")[1])
        stopped = SHOWING(LoadState="loaded", ActiveState="inactive", SubState="dead", UnitFileState="disabled")
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): stopped})):
            self.assertIn(f"INFO session: {work} free (manager inactive/dead)", self.main("verify")[1])
        with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): None})):
            self.assertIn(f"INFO session: {work} free (manager unknown)", self.main("verify")[1])
        live = (0, "ai.ag2.sparrow.claude-local = {\n\tactive count = 1\n\tpid = 4242\n\tstate = running\n}\n", "")
        with mock.patch.object(cm, "is_macos", return_value=True):
            with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): live})):
                out = self.main("verify")[1]
            self.assertIn(f"INFO session: {work} restarting (manager pid 4242, state running, lock free)", out)
            self.assertNotIn("KillMode", out)
            self.assertIn(f"INFO launchd SIGTERMs the group once after a launcher death and respawns after {cm.THROTTLE_S} s", out)
            with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): (0, "\tstate = waiting\n", "")})):
                self.assertIn(f"INFO session: {work} restarting (manager state waiting, lock free)", self.main("verify")[1])
            with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): NOT_LOADED})):
                self.assertIn(f"INFO session: {work} free (no job)", self.main("verify")[1])
            holder = hold(work, "claude-local")
            try:
                with mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): live})):
                    self.assertIn(f"INFO session: {work} held by launcher pid {holder.pid} (manager pid 4242, state running)",
                                  self.main("verify")[1])
            finally:
                unhold(holder)

    def test_manager_detail_reads_explicit_outputs_only(self):
        for got, want in (((0, "", ""), "loaded"), ((1, "", "odd\n"), "unknown"), (NOT_LOADED, "absent"), (None, "unknown")):
            with mock.patch.object(cm, "is_macos", return_value=True), \
                    mock.patch.object(cm, "_run", side_effect=Runner({("launchctl", "print"): got})):
                self.assertEqual(cm.manager_detail(L), want, got)
        for got, want in ((None, "unknown"), ((1, "", "bus\n"), "unknown"), ((0, "\n\n", ""), "unknown"),
                          (SHOWING(LoadState="not-found", ActiveState="inactive", SubState="dead", UnitFileState="disabled"), "absent"),
                          (SHOWING(LoadState="loaded", ActiveState="failed", SubState="failed", UnitFileState="enabled"), "failed/failed")):
            with mock.patch.object(cm, "_run", side_effect=Runner({("systemctl", "show"): got})):
                self.assertEqual(cm.manager_detail(L), want, got)


class ConnectReadme(unittest.TestCase):
    def test_the_readme_states_the_limits_and_the_fallbacks(self):
        text = " ".join((ADAPTER / "README.md").read_text().split())
        for phrase in ("`AbandonProcessGroup` false", "`ThrottleInterval` 30", "`KillMode=control-group`", "it never SIGKILLs",
                       "A session that ignores SIGTERM overlaps the new one", "calls `setsid()`",
                       "asserted from `KillMode=control-group` and not witnessed here", "macOS has no `pidfd_open`",
                       "`Popen` handle is the fallback", "`AG2_SPARROW_MANAGED=1`", "not a security boundary",
                       "yours to stop", "no trampoline", "as its **direct child**", "never signals a bare pid or a group"):
            self.assertIn(phrase, text, phrase)
        for gone in ("held, launcher gone", "trampoline **parent**", "group kill", "sweeps the group"):
            self.assertNotIn(gone, text, gone)


class ConnectSourceRules(unittest.TestCase):
    """R1 and R7 as source pins: no group signal or descriptor passing in the package; no lease symbol
    anywhere the design once named it."""

    def test_no_killpg_anywhere_in_the_package_and_no_group_or_descriptor_passing_in_the_lifecycle(self):
        for path in sorted((PKG / "ag2_sparrow").rglob("*.py")):
            self.assertNotIn("killpg", path.read_text(), path)
        src = Path(cm.__file__).read_text()
        for token in ("start_new_session", "process_group", "pass_fds", "setsid", "setpgid", "os.kill(", "TRAMPOLINE"):
            self.assertNotIn(token, src, token)

    def test_no_lease_symbol_survives_in_the_module_the_adapter_the_docs_or_the_tests(self):
        words = ["child_" + "lease", "end_" + "lease", "lease" + "_used", "Owner" + "." + "leased"]
        pattern = re.compile("|".join(map(re.escape, words)) + r"|\." + "leased" + r"\b")
        repo = PKG.parents[1]
        files = [Path(cm.__file__), Path(task_io.__file__), Path(holder_lock.__file__), Path(cli.__file__),
                 ADAPTER / "README.md", Path(__file__)]
        files += [p for p in (repo / "src" / "holder_lock.py", repo / "tests" / "holder-lock.test.py",
                              repo / "docs" / "design-holder-lock.md") if p.exists()]
        for path in files:
            self.assertIsNone(pattern.search(path.read_text()), path)
        self.assertFalse(hasattr(holder_lock, words[0]))
        self.assertFalse(hasattr(holder_lock, words[1]))
        self.assertFalse(hasattr(task_io, "lease_fd") or hasattr(task_io, "release_after_reap"))


if __name__ == "__main__":
    unittest.main()
