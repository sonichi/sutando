"""Fixtures shared by the claude-code adapter suites (test_claude_code_adapter, _connect and _install):
the fake manager, the stand-in launchers, the connect backend and the base test cases."""
from __future__ import annotations

import contextlib
import errno
import functools
import hashlib
import hmac
import io
import json
import os
import plistlib
import re
import secrets
import signal
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ, isolated_child_env  # noqa: E402
from _helpers.subprocess_coverage import covered_code  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

PKG = Path(__file__).resolve().parents[1]
# Every file of the claude-code adapter suites, for the source pins that scan the tests too.
SUITE_FILES = sorted(Path(__file__).resolve().parent.glob("test_claude_code_*.py")) + [Path(__file__).resolve()]
sys.path.insert(0, str(PKG))
from ag2_sparrow.local_task_protocol import parse_task_headers  # noqa: E402
from ag2_sparrow.team_result_guard import resolve_access_tier  # noqa: E402

from ag2_sparrow import connect_bridge as cb, task_io  # noqa: E402
from ag2_sparrow import gateway_credential_sources as _sources  # noqa: E402

ADAPTER = PKG / "adapters" / "claude-code"
MOD = PKG / "ag2_sparrow" / "adapters" / "claude_code" / "ag2space-inbox"
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


# ── `ag2-sparrow-claude-local`: the installed local shape ─────────────

from ag2_sparrow import _dirs, connect_common as cm, holder_lock, service_manager as sm, tmux_pane_keys, tmux_probe  # noqa: E402
from ag2_sparrow import claude_local_accepter as accepter, claude_local_card as card, claude_local_records as records  # noqa: E402

from ag2_sparrow.adapters.claude_code import cli, detector, hmac_policy, hold as hold_mod, inbox, launcher, trust  # noqa: E402
from ag2_sparrow.adapters.claude_code import shape as shape_mod  # noqa: E402

from ag2_sparrow.hitl.schema import ActionReply, STATUS_IN_PROGRESS, STATUS_PENDING, STATUS_RESOLVED  # noqa: E402

# The fake is a #!/bin/sh wrapper that execs the quoted interpreter on the body beside
# it, so an interpreter path containing a space works where a shebang could not carry it.
FAKE_CLAUDE_SH = '''#!/bin/sh
exec "%(py)s" "$0.py" "$@"
'''
FAKE_CLAUDE = r'''import json, os, sys
home = os.environ["HOME"]
# The wrapper is what the launcher ran; record it as argv[0], as a real claude would see itself.
if sys.argv[0].endswith(".py"):
    sys.argv[0] = sys.argv[0][:-3]
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
    listing = ["ERROR %s" % e]
# PWD, SHLVL, OLDPWD and _ are the sh wrapper's own, never the launcher's.
env = {k: v for k, v in os.environ.items() if k not in ("PWD", "SHLVL", "OLDPWD", "_")}
import stat as _stat
fifos = []
for name in os.listdir("/dev/fd"):
    try:
        if _stat.S_ISFIFO(os.fstat(int(name)).st_mode):
            fifos.append(int(name))
    except OSError:
        pass
json.dump({"argv": sys.argv, "env": env, "cwd": os.getcwd(), "pid": os.getpid(), "ppid": os.getppid(),
           "listing": listing, "fifos": fifos},
          open(os.path.join(home, "claude-session.json"), "w"))
import signal, time
if read("claude-ignore-term", ""):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if read("claude-ignore-hup", ""):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
if read("claude-signal-parent", ""):
    os.kill(os.getppid(), getattr(signal, "SIG" + read("claude-signal-parent", "")))
if read("claude-exit-on-exit", ""):
    for line in sys.stdin:
        if line.strip() == "/exit":
            break
    sys.exit(0)
time.sleep(float(read("claude-linger", "0")))
sys.exit(int(read("claude-exit", "0")))
'''


def write_fake_claude(path, py=sys.executable):
    """The wrapper at `path` (what --claude names) and its Python body at `path`.py."""
    path = Path(path)
    path.write_text(FAKE_CLAUDE_SH % {"py": py})
    path.chmod(0o755)
    path.with_name(path.name + ".py").write_text(FAKE_CLAUDE)
    return path


TOKEN = "tok-FAKE-RELAY-TOKEN-never-copied"
NONCE = "0123456789abcdef0123456789abcdef"
HOLD_DRIVER = ("import sys, os, time; sys.path.insert(0, %r)\n"
               "from ag2_sparrow.adapters.claude_code import cli, hold\nfrom ag2_sparrow import task_io\n%s"
               "sys.exit(cli.main(['hold', '--generation', %r, '--']))")
# A consumer stand-in for the inbox CLI: the root held, a sleep child attached as the consumer.
CONSUMER_HOLDER = ("import sys, subprocess; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
                   "o = task_io.acquire_lock(%r, 'claude-local'); c = subprocess.Popen(['sleep', '300'])\n"
                   "task_io.attach_consumer(o, task_io.holder_record_bytes(o), c.pid, task_io.start_time(c.pid))\n"
                   "print('held', c.pid, flush=True); sys.stdin.read(); c.kill(); c.wait(); task_io.release_lock(o)")
SESSION_ID = "0f3c5a9e-5b1d-4c2e-9a7b-1234567890ab"
L = cli.LOCAL
REAL_SOCKET_PROBLEM, REAL_TMUX_VERSION = launcher.socket_problem, launcher.tmux_version   # ConnectBase patches both
STAND_IN = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import holder_lock, task_io\n"
            "install = %r\n"
            "if install: holder_lock.install_id = lambda: install\n"
            "o = task_io.acquire_lock(%r, %r); print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
UNPUBLISHED = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import holder_lock\n"
               "r = holder_lock.acquire(%r, install_id=holder_lock.install_id())\n"
               "print('held', flush=True); sys.stdin.read(); holder_lock.release(r.owner)")
REPLACER = ("import os, sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm, task_io\n"
            "r, relay, agent, adapter = %r, %r, %r, %r\n"
            "os.rename(r, r + '.parked'); os.rename(r + '.bridge', r + '.bridge.parked'); os.mkdir(r)\n"
            "from ag2_sparrow import gateway_credential_sources as gs\n"
            "gs.write_seat_credentials(r + '.bridge', agent_id=agent, instance='d' * 32, tier='owner', relay_url=relay, "
            "bearer='replacer-bearer-RRRR7777')\n"
            "o = task_io.acquire_lock(r, adapter); print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
SUCCESSOR_WRITER = ("import os, sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm, task_io\n"
                    "r, relay, agent, adapter = %r, %r, %r, %r\n"
                    "os.rename(r, r + '.parked'); os.mkdir(r); o = task_io.acquire_lock(r, adapter)\n"
                    "print('held', flush=True)\n"
                    "got = cm.write_seat_held(o, r, r + '.bridge', {'relay_url': relay, "
                    "'bearer': 'successor-bearer-UUUU9999', 'agent_id': agent})\n"
                    "print('written', got[1] or 'ok', flush=True); sys.stdin.read(); task_io.release_lock(o)")
SEAT_HOLDER = ("import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
               "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()")
AGENT_LOCK_HOLDER = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm\n"
                     "lock = cm.agent_lock(%r); lock.__enter__()\n"
                     "print('held', flush=True); sys.stdin.read()")
CONTENDER = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm\n"
             "from ag2_sparrow.adapters.%s import %s as other\n"
             "cm.AGENT_LOCK_WAIT_S = 0.3; cm.LAUNCH_WAIT_S = 0.3\n"
             "print('install', cm.install(other.LOCAL, {}, %r), flush=True)\n"
             "print('launch', cm.launch_admission(%r)[1] or 'admitted', flush=True)")
# The manager's start, as every Runner start does it: the production admission, then the root. Its stop
# (stdin closed) is recorded as a signal, which ends a wait in admission at once, as in production.
LAUNCHED = ("import sys, threading; sys.path.insert(0, %r)\n"
            "from ag2_sparrow import connect_common as cm, holder_lock, task_io\n"
            "install = %r\n"
            "if install: holder_lock.install_id = lambda: install\n"
            "state, stopped = {'sig': None}, threading.Event()\n"
            "def eof():\n"
            "    sys.stdin.read(); state['sig'] = 15; stopped.set()\n"
            "threading.Thread(target=eof, daemon=True).start()\n"
            "fd, why = cm.launch_admission(sys.argv[1], state, wait_s=5)\n"
            "if why: print('refused', why, flush=True); sys.exit(75)\n"
            "o = task_io.acquire_lock(sys.argv[1], %r); cm.end_admission(fd)\n"
            "print('held', flush=True); stopped.wait(); task_io.release_lock(o)")
# The real launcher entry point (hold_and_supervise), a sleep for its consumer; argv: root, a file whose
# absence holds it between admission and the root ('' for none), a file it appends to once it acts.
REAL_LAUNCHER = ("import os, subprocess, sys, time; sys.path.insert(0, %r)\n"
                 "from ag2_sparrow import connect_common as cm, task_io\n"
                 "from ag2_sparrow.adapters.claude_code.cli import LOCAL\n"
                 "root, pause, acted = sys.argv[1:4]\n"
                 "cm.LAUNCH_WAIT_S = 20\n"
                 "state = cm.stop_state(); cm.stop_handlers(state)\n"
                 "real = task_io.acquire_lock\n"
                 "def paused(*a, **kw):\n"
                 "    while pause and not os.path.exists(pause):\n"
                 "        time.sleep(0.05)\n"
                 "    return real(*a, **kw)\n"
                 "task_io.acquire_lock = paused\n"
                 "if acted != os.devnull: open(acted + '.ready', 'w').close()   # its stop handlers are in place\n"
                 "def prepare(owner, r):\n"
                 "    with open(acted, 'a') as f: f.write('acted\\n')\n"
                 "    print('held', flush=True); return None, ''\n"
                 "def supervise(child):\n"
                 "    import select\n"
                 "    while state['sig'] is None and not (select.select([sys.stdin], [], [], 0.1)[0] and not sys.stdin.read(1)):\n"
                 "        pass\n"
                 "    child.kill(); child.wait(); return 0\n"
                 "rc = cm.hold_and_supervise(LOCAL, root, state, prepare, lambda ctx: subprocess.Popen(['sleep', '300']),\n"
                 "                           supervise, lambda c: (c.kill(), c.wait()), 'run', 'test')\n"
                 "print('refused', rc, flush=True)")
# launchd's shape: the job's pid is a shim whose child is the launcher; the stop signal is forwarded.
SHIM = ("import signal, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[1]] + sys.argv[2:])\n"
        "signal.signal(signal.SIGTERM, lambda *a: child.terminate())\n"
        "sys.exit(child.wait())")
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


START_VERBS = ("restart", "start", "bootstrap", "kickstart")
STOP_VERBS = ("stop", "bootout")


def hold(root, name="first-launcher", script=STAND_IN, install=""):
    """A lock holder in another process (holder_lock allows one acquisition per process)."""
    p = subprocess.Popen([sys.executable, "-c", covered_code(script % ((str(PKG), install, str(root), name) if script is STAND_IN
                                                                          else (str(PKG), str(root)) if script is UNPUBLISHED
                                                                          else (str(PKG),)))],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def launched(root, adapter, install="", real=False, pause="", acted="", shim=False):
    """The manager's start, not waited for: its launcher waits in admission until the operation that
    started it releases the agent lock."""
    if real:
        code = REAL_LAUNCHER % (str(PKG),)
        argv = [sys.executable, "-c", SHIM, code] if shim else [sys.executable, "-c", code]
        argv += [str(root), pause, acted or os.devnull]
    else:
        argv = [sys.executable, "-c", LAUNCHED % (str(PKG), install, adapter), str(root), "claude-local"]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=real)


def unhold(p):
    p.stdin.close()
    p.wait(timeout=30)
    p.stdout.close()


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
    """A manager command table for service_manager._run: (program, verb) -> rc or (rc, out, err); None =
    could not run. With a root, a start verb runs a stand-in launcher (adapter `adapter`, install identity
    `install`) holding the root's lock, a stop verb ends it (after `stop_delay`), and the probes answer
    from the manager's own state unless the table says otherwise: `show` LoadState from the unit file or
    a `registered` job, ActiveState from the stand-in, UnitFileState from enable/disable; `print` likewise."""

    def __init__(self, table=None, default=0, root=None, adapter="claude-local", install="", registered=False,
                 stop_delay=0.0):
        self.table, self.default, self.calls, self.root = dict(table or {}), default, [], root
        self.session, self.adapter, self.install = None, adapter, install
        self.enabled, self.registered, self.stop_delay, self.timers = False, registered, stop_delay, []
        self.real, self.pause, self.acted, self.shim, self.spawned = False, "", "", False, []
        self.stop_timeout = 10

    def start_session(self, by_manager=False):
        self.stop_session()
        self.session = launched(self.root, self.adapter, self.install, self.real, self.pause, self.acted,
                                self.shim) if by_manager else \
            hold(self.root, self.adapter, install=self.install)
        self.spawned.append(self.session)

    def stop_session(self):
        for t in self.timers:
            t.join()
        self.timers.clear()
        if self.session is not None:
            if self.real:                           # the manager's stop: SIGTERM to its process group, then a bound
                try:
                    os.killpg(self.session.pid, signal.SIGTERM)
                    self.session.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    return                              # the stop timed out: the job is left running
                except ProcessLookupError:
                    pass
            unhold(self.session)
            self.session = None

    def alive(self):
        return self.session is not None and self.session.poll() is None

    def reap(self):
        """The test's own cleanup: every launcher it spawned, and its process group, gone."""
        for spawned in self.spawned:
            if self.real and spawned.poll() is None:
                try:
                    os.killpg(spawned.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.stop_session()

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
            if self.alive():
                return (0, f"\tpid = {self.session.pid}\n\tstate = running\n", "")
            return (0, "\tstate = waiting\n", "") if self.registered else NOT_LOADED
        if verb == "show":
            loaded = cm.service_path(L).exists() or self.registered
            words = {"LoadState": "loaded" if loaded else "not-found", "ActiveState": "active" if self.alive() else "inactive",
                     "SubState": "running" if self.alive() else "dead", "UnitFileState": "enabled" if self.enabled else "disabled",
                     "MainPID": str(self.session.pid) if self.alive() else "0"}
            props = cmd[cmd.index("-p") + 1].split(",")
            return (0, "".join(words[k] + "\n" for k in props), "")
        return self.default

    def __call__(self, cmd, timeout=30):
        self.calls.append(list(cmd))
        verb = self.verb(cmd)
        loaded = (37, "", "Bootstrap failed: 37: Operation already in progress\n")   # launchd, on a loaded job
        got = self.table[(cmd[0], verb)] if (cmd[0], verb) in self.table else \
            loaded if verb == "bootstrap" and self.alive() else self.answer(verb, cmd)
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
            if verb in START_VERBS and not (verb == "start" and self.alive()):     # start is a no-op when active
                self.start_session(by_manager=True)
            elif verb in STOP_VERBS:
                self.end_session_later() if self.stop_delay else self.stop_session()
                if self.alive():
                    return subprocess.CompletedProcess(cmd, 1, "", "Job for the unit timed out stopping")
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def verbs(self, program):
        return [self.verb(c) for c in self.calls if c[0] == program]

    def pid(self):
        return self.session.pid if self.alive() else None


class ConnectBase(unittest.TestCase):
    home_dir = None     # a suite that starts a scratch tmux server puts HOME under /tmp: a short socket path

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="cl", dir=self.home_dir)).resolve()   # macOS: /var is /private/var
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.claude = write_fake_claude(self.bin / "claude")
        self.work = self.home / "work"
        self.inbox, self.results = self.work / "tasks", self.work / "results"
        self.plugin = self.home / "plugin"
        (self.home / "task-hmac.key").write_text("ab" * 32)     # what install passes: strict by default needs it
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
        self.patches = [mock.patch.object(sm, "_run", side_effect=lambda cmd, timeout=30: self.calls.append(cmd)),
                        mock.patch.object(cm, "is_macos", return_value=False),
                        mock.patch.object(cm, "console_script", return_value=str(self.bin / cli.COMMAND)),
                        mock.patch.object(cm, "CONFIRM_TIMEOUT_S", 2.0), mock.patch.object(cm, "OP_WAIT_S", 1.0),
                        mock.patch.object(launcher, "tmux_version", return_value=(3, 6)),
                        mock.patch.object(launcher, "socket_problem", return_value="")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.runners = []
        self.addCleanup(lambda: [r.reap() for r in self.runners])

    def runner(self, table=None, default=0, root=True, adapter="claude-local", **kw):
        r = Runner(table, default, str(self.work) if root else None, adapter, **kw)
        self.runners.append(r)
        return r

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.main("install", "--plugin-dir", str(self.plugin), "--inbox", str(self.inbox),
                         "--claude-config-dir", str(self.home / ".claude-sutando"),
                         "--hmac-key-file", str(self.home / "task-hmac.key"), *extra)

    def install_with(self, runner, mac=False, *extra):
        with mock.patch.object(sm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=mac):
            return self.install(*extra)

    def values(self, **over):
        v = {"CLAUDE": str(self.claude), "PLUGIN_DIR": str(self.plugin), "INBOX_DIR": str(self.inbox),
             "RESULTS_DIR": str(self.results), "CLAUDE_CONFIG_DIR": str(self.home / ".claude-sutando"),
             "HMAC_KEY_FILE": str(self.home / "task-hmac.key"), "HMAC_STRICT": "", "PERMISSION_MODE": "bypassPermissions",
             "SESSION_ID": SESSION_ID, "HITL_STORE": str(cli.hitl_store_root()), "ROOT": str(self.work),
             "BRIDGE_STATE_DIR": str(_dirs.state_dir())}
        v.update(over)
        return v

    def configure(self, **over):
        """The config as install writes it, with no manager involved: what run, verify and uninstall read."""
        for d in (self.inbox, self.results):
            d.mkdir(parents=True, exist_ok=True)
        cm._write(cli.config_path(), cli.render_config(self.values(**over)), 0o600)
        return cli.config_path()

    def status(self, state=None, name="gateway-status.json", **over):
        """A bridge's gateway status, as the bridge writes it each poll, feeding this test's root by default."""
        state = Path(state) if state else cli.bridge_state(cli.read_config(cli.config_path()))
        state.mkdir(parents=True, exist_ok=True)
        rec = {"connected": True, "ts": int(time.time()), "agent_id": None, "error": None,
               "tasks_dir": os.path.realpath(self.inbox), "results_dir": os.path.realpath(self.results)}
        rec.update(over)
        (state / name).write_text(json.dumps(rec))
        self.addCleanup((state / name).unlink, missing_ok=True)     # the default state dir outlives a test
        return state / name

    def session(self):
        p = self.home / "claude-session.json"
        return json.loads(p.read_text()) if p.exists() else None

    def hold_env(self, root=None, **extra):
        env = isolated_child_env(PYTHONPATH=str(PKG))
        env.update({hold_mod.ENV_ROOT: root or str(self.work), hold_mod.ENV_CONFIG_DIR: str(cli.config_dir()),
                    hold_mod.ENV_SESSION_ID: SESSION_ID, hold_mod.ENV_HITL_STORE: str(cli.hitl_store_root())})
        env.update(extra)
        return env

    def hold_proc(self, root=None, patch="", linger=None, **extra):
        """`hold` in a child process (a real root lock, a real fchdir), stdin /dev/null."""
        if linger is not None:
            (self.home / "claude-linger").write_text(str(linger))
        return subprocess.Popen([sys.executable, "-c", covered_code(HOLD_DRIVER % (str(PKG), patch, NONCE))],
                                env=self.hold_env(root, **extra),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def hold_cmd(self, root=None, patch="", linger=0, **extra):
        p = self.hold_proc(root, patch, linger, **extra)
        out, err = p.communicate(timeout=60)
        return p.returncode, out, err


def task_io_lock_holder(root):
    return task_io.lock_holder(root)


class BridgeFake:
    """The agent bridge's own manager state, so its unit never reaches the launcher's Runner: commands
    naming the bridge's service or label answer here, every other command goes to `runner`."""

    def __init__(self, runner, table=None):
        self.runner, self.table, self.calls = runner, dict(table or {}), []
        self.active = self.enabled = self.registered = False

    def ours(self, cmd):
        return any(cli.BRIDGE.service_name in a or cli.BRIDGE.agent_label in a for a in cmd)

    def __call__(self, cmd, timeout=30):
        if not self.ours(cmd):
            return self.runner(cmd, timeout)
        self.calls.append(list(cmd))
        verb = cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))
        if verb in self.table:
            got = self.table[verb]
            return None if got is None else subprocess.CompletedProcess(cmd, got, "", "exit %s" % got)
        out = ""
        if verb == "show":
            loaded = cm.service_path(cli.BRIDGE).exists() or self.registered
            words = {"LoadState": "loaded" if loaded else "not-found", "ActiveState": "active" if self.active else "inactive",
                     "UnitFileState": "enabled" if self.enabled else "disabled"}
            out = "".join(words[k] + "\n" for k in cmd[cmd.index("-p") + 1].split(","))
        elif verb == "print":
            if not self.registered:
                return subprocess.CompletedProcess(cmd, 113, "", 'Could not find service "%s" in domain\n' % cli.BRIDGE.agent_label)
            return subprocess.CompletedProcess(cmd, 0, "\tstate = running\n" if self.active else "\tstate = waiting\n", "")
        elif verb in ("enable", "disable"):
            self.enabled = verb == "enable"
        elif verb in ("restart", "bootstrap"):
            self.active = self.registered = True
        elif verb in ("stop", "bootout"):
            self.active = False
            self.registered = self.registered and verb == "stop"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def verbs(self):
        return [c[1] if c[0] == "launchctl" else next(a for a in c[1:] if not a.startswith("--")) for c in self.calls]


AGENT = "@probe.agent:ag2.space"


class AgentRootBase(ConnectBase):
    def setUp(self):
        super().setUp()
        self.relay.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\nAGENT_MXID={AGENT}\n")
        self.agents = self.home / ".ag2-sparrow" / "agents"
        self.root = self.agents / "probe.agent_ag2.space"
        self.scripts = mock.patch.object(cm, "console_script", side_effect=lambda shape: str(self.bin / shape.command))
        self.scripts.start()
        self.addCleanup(self.scripts.stop)

    def bridged(self, table=None, root=None, **kw):
        runner = Runner(table, 0, str(root or self.root), **kw)
        self.runners.append(runner)
        return BridgeFake(runner)

    def install_default(self, fake, *extra, mac=False):
        with mock.patch.object(sm, "_run", side_effect=fake), mock.patch.object(cm, "is_macos", return_value=mac):
            return self.main("install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                             "--hmac-key-file", str(self.home / "task-hmac.key"), *extra)

    def cfg(self):
        return cli.read_config(cli.config_path())


BASE = "https://backend.example/api"
CODE = "acp.code-MMMM3333"
BEARER = "redeemed-bearer-NNNN4444"
RELAY = "https://relay.backend.example/relay"  # in BASE's domain


class _Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.body


class Backend:
    """The E2 redemption endpoint: a list of statuses answered in order, then 200 with the credential."""

    def __init__(self, *statuses, answer=None):
        self.statuses, self.calls = list(statuses), []
        self.answer = {"relay_url": RELAY, "bearer": BEARER, "agent_id": AGENT} if answer is None else answer

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data.decode()), req.get_method()))
        status = self.statuses.pop(0) if self.statuses else 200
        status, body = status if isinstance(status, tuple) else (status, {})
        if status == 202:
            return _Resp(202, json.dumps(body).encode())
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode()))
        return _Resp(200, json.dumps(self.answer).encode())


def seed_seat(seat, bearer):
    """A seat as another writer left it: the redeemed shape, written by the shared credential writer."""
    assert _sources.write_seat_credentials(seat, agent_id=AGENT, instance="c" * 32, tier="owner",
                                           relay_url=RELAY, bearer=bearer) == ""
    return seat


def hold_consumer(root):
    """A root held with a live sleep consumer in another process: (proc, consumer pid)."""
    p = subprocess.Popen([sys.executable, "-c", covered_code(CONSUMER_HOLDER % (str(PKG), str(root)))], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline().split()
    assert line[:1] == ["held"], line
    return p, int(line[1])


def wait_for(predicate, seconds=10.0, step=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(step)
    return predicate()


CONTRACT_TS = MOD / "tests" / "cli-contract.ts"


def contract_scenarios() -> list:
    """The scenarios the harness's fake CLI is bound to (one JSON literal in a .ts module the harness imports)."""
    text = CONTRACT_TS.read_text()
    return json.loads(text[text.index("export const CONTRACT = ") + len("export const CONTRACT = "):])


def contract_names() -> list:
    """The pinned scenario set: the names each runner must execute, exactly and in order."""
    text = CONTRACT_TS.read_text()
    start = text.index("export const CONTRACT_NAMES = ") + len("export const CONTRACT_NAMES = ")
    return json.loads(text[start:text.index("export const CONTRACT = ")].rsplit("\n//", 1)[0])


def contract_set_problem(ran: list) -> str:
    """'' when `ran` is exactly the pinned scenario set, else the missing and extra names."""
    want = contract_names()
    if ran == want:
        return ""
    return "missing %s; extra %s" % ([n for n in want if n not in ran], [n for n in ran if n not in want]) \
        if set(ran) != set(want) else "order differs"


def contract_shape(expect: dict, rc: int, out: dict) -> str:
    """'' when the reply has the expected shape (exit, refused, retry, fields, keys), else what differs."""
    got = {"exit": rc, "refused": "refused" in out, "retry": out.get("retry") is True}
    bad = ["%s %r != %r" % (k, got[k], expect[k]) for k in ("exit", "refused", "retry") if got[k] != expect[k]]
    bad += ["%s %r != %r" % (k, out.get(k), v) for k, v in (expect.get("fields") or {}).items() if out.get(k) != v]
    bad += ["no %s" % k for k in expect.get("has") or [] if k not in out]
    reason = str(out.get("reason", ""))
    bad += ["reason %r" % reason for k in ("reason_prefix",) if k in expect and not reason.startswith(expect[k])]
    bad += ["reason %r" % reason for k in ("reason_suffix",) if k in expect and not reason.endswith(expect[k])]
    bad += ["%s not marked" % k for k in expect.get("marked") or [] if k not in (out.get("stuck") or {})]
    bad += ["%s still marked" % k for k in expect.get("unmarked") or [] if k in (out.get("stuck") or {})]
    return "; ".join(bad)


def fd_path(fd):
    """The path an open fd names (macOS F_GETPATH, Linux /proc), or '' when it cannot be read."""
    try:
        if sys.platform == "darwin":
            import fcntl
            return os.path.realpath(fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode())
        return os.path.realpath(os.readlink("/proc/self/fd/%d" % fd))
    except OSError:
        return ""


def complete_safe(path):
    """Whether the ledger still opens (a corrupted one is reset so later property cases stay independent)."""
    try:
        inbox.lifecycle.SettledIds(path)
        return True
    except inbox.lifecycle.LedgerError:
        return False


def real_ids(path):
    return [line for line in Path(path).read_text(errors="replace").splitlines()
            if inbox.lifecycle.local_task_protocol.valid_task_id(line)]


CANARY = ("AG2_TEST_CANARY", "canary-must-not-reach-the-server")
CONSOLE_SH = """#!/bin/sh
exec "%(py)s" -c '%(cov)simport sys; sys.path.insert(0, "%(pkg)s"); from ag2_sparrow.adapters.claude_code.cli import main; sys.exit(main(sys.argv[1:]))' "$@"
"""
STUB_SH = "#!/bin/sh\nexec cat\n"
TMUX_DIR = os.path.dirname(shutil.which("tmux") or "/usr/bin/tmux")


ROOM, OWNER_MXID = "!owner-dm:ag2.space", "@owner:ag2.space"
STALL = {"reason": "static for 600 s with task-a in flight", "since": 100.0, "frame_hash": "f" * 16, "task": "task-a",
         "checks": "pane static while an offer is in flight", "lines": ["working..."]}


def tree_bytes(root):
    """Every file under `root` (lock files aside) with its bytes: the store compared before and after."""
    root = Path(root)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and p.name != ".lock"}


class CardBase(ConnectBase):
    """A configured root, the bridge's claude-local store emptied, and a launcher whose stop is counted."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.root = str(self.work)
        self.assertEqual(inbox.prepare_root(self.root), "")
        self.state_dir = _dirs.state_dir()
        self.store_root = card.store_root(self.state_dir)
        for d in (self.store_root.parent,):
            shutil.rmtree(d, ignore_errors=True)
            self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(str(self.store_root), self.values()["HITL_STORE"])
        self.lc = launcher.Launcher(cli.load_config()[0], cli.config_dir(), sleep=lambda s: None, clock=lambda: 5000.0)
        self.stops = []
        self.lc.stop_session = lambda rec, why: self.stops.append(why) or "stopped"
        card.register_install(self.store_root, self.lc.opaque, self.root, self.lc.supervisor_lock)

    def raise_card(self, stall=STALL, room=ROOM):
        records.save_supervisor(self.root, dict(records.load_supervisor(self.root), stall=stall,
                                                install_id=self.lc.opaque, generation={"pane_id": "%0"}))
        rec = self.lc.raise_card(self.lc.record(), card.KIND_STALL, stall["checks"])
        store, _ = card.manager(self.store_root)
        req = store.load(rec["hitl"]["id"])
        if room:
            req.room = room
            req.subject = {card.SUBJECT: dict(card.local(req), room=room)}
            store.save(req, store.projection(req.id))
        return store.load(req.id)

    def reply(self, req, action=card.ACTION_STOP, room=ROOM, sender=OWNER_MXID, revision=None):
        return ActionReply(hitl_id=req.id, expected_revision=req.revision if revision is None else revision,
                           action_id=action, guard=req.guard, room=room, sender=sender)

    def accept(self, reply, owner_ok=True, local=False):
        return accepter.accept(reply, state_dir=self.state_dir, owner_ok=owner_ok, local=local, log=lambda *a: None)

    def load(self, req_id):
        return card.manager(self.store_root)[0].load(req_id)


class FakeTmux:
    """A Tmux stand-in for the classifier: what has-session answers and the pane's fields."""

    def __init__(self, present=True, fields=None):
        self.sock, self.present, self.fields = "/tmp/fake.sock", present, fields

    def has_session(self, name=None):
        return self.present

    def pane_fields(self, pane_id):
        return self.fields


GEN = {"pane_id": "%0", "pane_pid": 1, "session_id": "$0", "session_created": 1, "server_pid": 1, "server_start": "s",
       "start_command": "c", "nonce": NONCE}


def ran(rc=0, out="", err=""):
    return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()


def raises(*_, **__):
    raise OSError(errno.ENOENT, "gone")


class StubTm:
    """The Tmux surface create_session and the stop path touch: the commands it ran, fixed fields."""

    def __init__(self, fields=None, present=True):
        self.tmux, self.sock, self.fields, self.present, self.calls = "tmux", "/tmp/stub.sock", fields, present, []

    def run(self, *args, **_):
        self.calls.append(args)

    def pane_fields(self, pane_id):
        return self.fields

    def has_session(self, name=None):
        return self.present

    def capture(self, pane_id):
        return None


# The pane of a fresh install, as the live witness captured it from Claude Code 2.1.303.
TRUST_PANE = """────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:
 /w/root/tasks
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source project, or work from your team). If not, take a moment to review what's in this
 folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""
BYPASS_PANE = """  WARNING: Claude Code running in Bypass Permissions mode
  ❯ No, exit
    Yes, I accept
  Enter to confirm · Esc to cancel
"""
READY_PANE = """ ▐▛███▛█   Claude Code
❯ Try "how does <filepath> work?"
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


def cm_dict(req):
    from ag2_sparrow.hitl.manager import _req_to_dict
    return _req_to_dict(req)
