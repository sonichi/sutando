#!/usr/bin/env python3
"""`ag2-sparrow-codex-local`: the installer, the managed launcher over the connector, verify and
uninstall, driven in-process against a fake `codex` (version, login status, and the stub app-server for
the connector's own child) and a fake manager whose start verbs run a stand-in holder. No real codex,
no systemctl or launchctl, no relay."""
from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import os
import plistlib
import shlex
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402
from _helpers.subprocess_coverage import covered_code  # noqa: E402

# Before the gateway-reaching imports below: no outer credential selector survives.
isolate_os_environ()

import fcntl  # noqa: E402  (outside the scanner's Python 3.9 stdlib list)
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
from ag2_sparrow import _dirs, connect_bridge as cb, connect_common as cm, holder_lock, service_manager as sm, task_io  # noqa: E402
from ag2_sparrow import gateway_credential_sources as _sources  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc, connect_cli as cli  # noqa: E402

STUB = Path(cc.__file__).with_name("stub_app_server.py")
# The wrapper survives an interpreter path with a space: exec of the quoted path on the body beside it.
FAKE_CODEX_SH = '#!/bin/sh\nexec "%(py)s" "$0.py" "$@"\n'
FAKE_CODEX = r'''import json, os, sys
home = os.environ["HOME"]
def read(name, default):
    try:
        return open(os.path.join(home, name)).read().strip()
    except OSError:
        return default
def record(name, obj):
    path = os.path.join(home, name)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".tmp", path)    # a poller never reads a torn file
if sys.argv[1:] == ["--version"]:
    print(read("codex-version-banner", "") or "codex-cli " + read("codex-version", "0.160.1"))
    sys.exit(int(read("codex-version-rc", "0")))
if sys.argv[1:3] == ["login", "status"]:
    record("codex-login.json", {"env": dict(os.environ)})
    print("Logged in using ChatGPT" if read("codex-login-rc", "0") == "0" else "Not logged in")
    sys.exit(int(read("codex-login-rc", "0")))
if sys.argv[1:3] == ["app-server", "--listen"]:
    record("codex-server.json", {"argv": sys.argv, "env": dict(os.environ), "pid": os.getpid()})
    os.execv(%(py)r, [%(py)r, %(stub)r])
sys.exit(3)
'''


def write_fake_codex(path, py=sys.executable):
    """The wrapper at `path` (what --codex names) and its Python body at `path`.py."""
    path = Path(path)
    path.write_text(FAKE_CODEX_SH % {"py": py})
    path.chmod(0o755)
    path.with_name(path.name + ".py").write_text(FAKE_CODEX % {"py": py, "stub": str(STUB)})
    return path


def bounded(fn, seconds=10.0):
    """`fn()` on a thread, its value, or an AssertionError once `seconds` pass: a wait that never ends fails."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "still running after %.0f s" % seconds
    return box[0]


def wait_for(pred, seconds=10.0):
    """True once `pred()` holds within `seconds`, polled; else False."""
    deadline = time.monotonic() + seconds
    while not pred():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True
TOKEN = "tok-FAKE-RELAY-TOKEN-never-copied"
FORBIDDEN_ARGS = ("--once", "--idle-exit", "--push", "--thread-id", "--ephemeral", "--developer-instructions",
                  "stub", "proxy", "--list-models", "--list-loaded")
STAND_IN = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
            "o = task_io.acquire_lock(%r, %r); print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
SUCCESSOR = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import holder_lock as h\n"
             "g = h.acquire(%r, install_id=h.install_id()); print('held' if isinstance(g, h.Acquired) else g, flush=True)\n"
             "sys.stdin.read(); h.release(g.owner)")
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
# The manager's start, as every Runner start does it: the production admission, then the root.
LAUNCHED = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm, holder_lock, task_io\n"
            "install = %r\n"
            "if install: holder_lock.install_id = lambda: install\n"
            "fd, why = cm.launch_admission(sys.argv[1], wait_s=5)\n"
            "if why: print('refused', why, flush=True); sys.exit(75)\n"
            "o = task_io.acquire_lock(sys.argv[1], %r); cm.end_admission(fd)\n"
            "print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
# The real launcher entry point (hold_and_supervise), a sleep for its consumer; argv: root, a file whose
# absence holds it between admission and the root ('' for none), a file it appends to once it acts.
REAL_LAUNCHER = ("import os, subprocess, sys, time; sys.path.insert(0, %r)\n"
                 "from ag2_sparrow import connect_common as cm, task_io\n"
                 "from ag2_sparrow.adapters.codex.connect_cli import LOCAL\n"
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
             "from ag2_sparrow.adapters.codex.connect_cli import LOCAL\n"
             "with cm.operation_lock(LOCAL): print('held', flush=True); sys.stdin.read()")
TASK = ("id: %s\ntimestamp: 2026-10-16T00:00:00Z\nsource: chat\nchannel_id: %s\naccess_tier: owner\n"
        "priority: normal\ntask: say hi\n")
NOT_LOADED = (113, "", 'Could not find service "ai.ag2.sparrow.codex-local" in domain for uid: 501\n')
START_VERBS = ("restart", "start", "bootstrap", "kickstart")
STOP_VERBS = ("stop", "bootout")
L = cli.LOCAL


def hold(root, adapter="codex-local"):
    """A lock holder in another process (holder_lock allows one acquisition per process)."""
    p = subprocess.Popen([sys.executable, "-c", STAND_IN % (str(PKG), str(root), adapter)],
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
        argv = [sys.executable, "-c", LAUNCHED % (str(PKG), install, adapter), str(root), "codex-local"]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=real)


def unhold(p):
    p.stdin.close()
    p.wait(timeout=30)
    p.stdout.close()


class Runner:
    """A manager command table for service_manager._run: (program, verb) -> rc or (rc, out, err); None =
    could not run. With a root, a start verb runs a stand-in launcher holding the root's lock, a stop
    verb ends it, and the probes answer from the manager's own state unless the table says otherwise."""

    def __init__(self, table=None, default=0, root=None, adapter="codex-local"):
        self.table, self.default, self.calls, self.root, self.adapter = dict(table or {}), default, [], root, adapter
        self.session, self.enabled, self.real = None, False, False
        self.pause, self.acted, self.shim, self.spawned, self.stop_timeout = "", "", False, [], 10

    def start_session(self, by_manager=False):
        self.stop_session()
        self.session = launched(self.root, self.adapter, real=self.real, pause=self.pause, acted=self.acted,
                                shim=self.shim) if by_manager else hold(self.root, self.adapter)
        self.spawned.append(self.session)

    def stop_session(self):
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

    def verb(self, cmd):
        return cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))

    def answer(self, verb, cmd):
        if verb == "print":
            return (0, f"\tpid = {self.session.pid}\n\tstate = running\n", "") if self.alive() else NOT_LOADED
        if verb == "show":
            loaded = cm.service_path(L).exists()
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
        if got is None:
            return None
        rc, out, err = got if isinstance(got, tuple) else (got, "", "exit %s" % got)
        if rc == 0 and verb in ("enable", "disable"):
            self.enabled = verb == "enable"
        if self.root and rc == 0:
            if verb in START_VERBS and not (verb == "start" and self.alive()):     # start is a no-op when active
                self.start_session(by_manager=True)
            elif verb in STOP_VERBS:
                self.stop_session()
                if self.alive():
                    return subprocess.CompletedProcess(cmd, 1, "", "Job for the unit timed out stopping")
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def verbs(self, program):
        return [self.verb(c) for c in self.calls if c[0] == program]

    def pid(self):
        return self.session.pid if self.alive() else None


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="cx")).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.codex = write_fake_codex(self.bin / "codex")
        self.work = self.home / "work"
        self.codex_home = self.home / ".codex-agent"
        self.codex_home.mkdir()
        relay = self.home / ".config" / "ag2-sparrow" / "relay.env"
        relay.parent.mkdir(parents=True)
        relay.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\n")
        relay.chmod(0o600)
        self.relay = relay
        env = {"HOME": str(self.home), "USER": "owner", "PATH": f"{self.bin}:/usr/bin:/bin", cm.MANAGED_VAR: "1",
               "SUTANDO_WORKSPACE_DIR": "/secret/ws", "REMOTE_TASK_TOKEN": TOKEN, "OPENAI_API_KEY": "sk-should-not-pass",
               "ANTHROPIC_API_KEY": "sk-ant-should-not-pass", "AWS_SECRET_ACCESS_KEY": "aws-should-not-pass"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in ("AG2_DEVICE_ENV", "CODEX_HOME", "AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR"):
            os.environ.pop(key, None)
        # No test reaches the host's systemctl, loginctl or launchctl; the bounds are 2 s and 1 s, not 30.
        self.calls = []
        self.patches = [mock.patch.object(sm, "_run", side_effect=lambda cmd, timeout=30: self.calls.append(cmd)),
                        mock.patch.object(cm, "is_macos", return_value=False),
                        mock.patch.object(cm, "console_script", return_value=str(self.bin / cli.COMMAND)),
                        mock.patch.object(cm, "CONFIRM_TIMEOUT_S", 2.0), mock.patch.object(cm, "OP_WAIT_S", 1.0)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.runners = []
        self.addCleanup(lambda: [r.reap() for r in self.runners])

    def runner(self, table=None, default=0, root=True, adapter="codex-local"):
        r = Runner(table, default, str(self.work) if root else None, adapter)
        self.runners.append(r)
        return r

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra):
        return self.main("install", "--work", str(self.work), "--codex-home", str(self.codex_home), *extra)

    def install_with(self, runner, mac=False, *extra):
        with mock.patch.object(sm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=mac):
            return self.install(*extra)

    def configure(self, **over):
        """The config as install writes it, with no manager involved: what run, verify and uninstall read."""
        values = {"CODEX": str(self.codex), "INBOX_DIR": str(self.work / "tasks"), "RESULTS_DIR": str(self.work / "results"),
                  "CODEX_HOME": str(self.codex_home), "SANDBOX": "read-only", "ALLOW_NON_OWNER": "", "MODEL": "",
                  "ROOT": str(self.work)}
        values.update(over)
        for d in ("tasks", "results"):
            (self.work / d).mkdir(parents=True, exist_ok=True)
        cm._write(cli.config_path(), cm.render_config(L, values), 0o600)
        return cli.config_path()

    def server(self):
        p = self.home / "codex-server.json"
        return json.loads(p.read_text()) if p.exists() else None

    def checks(self, out):
        return {line.split(":")[0][5:]: line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "FAIL")}


class Render(Base):
    def test_unit_and_plist_carry_the_marker_the_managed_var_and_paths_only(self):
        exec_start = str(self.bin / cli.COMMAND)
        unit = cm.render_service(L, exec_start)
        self.assertEqual(unit.split("\n")[0], "# Written by ag2-sparrow-codex-local install; re-run install to update it.")
        for line in (f"ExecStart={exec_start} run\n", "RestartPreventExitStatus=75\n", f"Environment={cm.MANAGED_VAR}=1\n",
                     "KillMode=control-group\n", "Description=ag2-sparrow codex-local (the Codex connector"):
            self.assertIn(line, unit)
        plist = cm.render_plist(L, exec_start, "/opt/bin", "/var/log/x.log")
        self.assertTrue(cm.marked(plist, cli.PLIST_MARKER))
        body = plistlib.loads(plist.encode())
        self.assertEqual((body["Label"], body["ProgramArguments"]),
                         ("ai.ag2.sparrow.codex-local", [exec_start, "run", "--launchd"]), "the shared entry shim")
        self.assertEqual(body["EnvironmentVariables"], {"PATH": "/opt/bin", cm.MANAGED_VAR: "1"})
        self.assertEqual(body["KeepAlive"], {"SuccessfulExit": False},
                         "a logical 75 exits 0 through the shim, so launchd does not relaunch over another holder")
        for text in (unit, plist, cm.render_config(L, {"CODEX": "/x", "ROOT": "/w"})):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("REMOTE_TASK_TOKEN", text)


class Install(Base):
    ACQUIRED = "the managed launcher acquired the expected root"

    def test_a_fresh_install_records_the_settings_starts_and_requires_a_fresh_holder(self):
        runner = self.runner()
        rc, out, err = self.install_with(runner)
        self.assertEqual(rc, 0, err)
        cfg_path = cli.config_path()
        self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
        self.assertTrue(cfg_path.read_text().startswith(cli.MARKER))
        cfg = cm.read_config(cfg_path)
        self.assertEqual((cfg["CODEX"], cfg["ROOT"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"]),
                         (str(self.codex), str(self.work), str(self.work / "tasks"), str(self.work / "results")))
        self.assertEqual((cfg["CODEX_HOME"], cfg["SANDBOX"], cfg["ALLOW_NON_OWNER"], cfg["MODEL"]),
                         (str(self.codex_home), "read-only", "", ""))
        self.assertTrue((self.work / "tasks").is_dir() and (self.work / "results").is_dir())
        self.assertTrue(cm.service_path(L).read_text().startswith(cli.MARKER))
        self.assertNotIn(TOKEN, cfg_path.read_text())
        self.assertEqual(runner.verbs("systemctl"), ["show", "show", "show-environment", "daemon-reload", "enable", "restart",
                                                    "show", "show"],
                         "absent and free: no stop, then register and start")
        self.assertIn(f"{self.ACQUIRED}: {self.work} (launcher pid {runner.pid()}, no live connector attached)", out)
        self.assertIsNone(self.server(), "install starts no connector itself; the manager does")

    def test_sandbox_and_non_owner_choices(self):
        rc, out, _ = self.install_with(self.runner(), False, "--sandbox", "workspace-write", "--allow-non-owner", "--model", "m1")
        self.assertEqual(rc, 0)
        self.assertIn("NOTE sandbox: workspace-write", out)
        cfg = cm.read_config(cli.config_path())
        self.assertEqual((cfg["SANDBOX"], cfg["ALLOW_NON_OWNER"], cfg["MODEL"]), ("workspace-write", "1", "m1"))
        self.assertEqual(cli.connector_argv(cfg, str(self.work), 7)[-5:],
                         ["--sandbox", "workspace-write", "--allow-non-owner", "--model", "m1"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["install", "--work", str(self.work), "--sandbox", "danger-full-access"])

    def test_install_refuses_an_old_codex_before_writing_anything(self):
        (self.home / "codex-version").write_text("0.157.0")
        rc, _, err = self.install()
        self.assertEqual(rc, 2)
        self.assertIn("codex 0.157.0 is older than the 0.160.1 this adapter needs", err)
        self.assertFalse(cli.config_path().exists())
        (self.home / "codex-version").unlink()
        (self.home / "codex-version-banner").write_text("A new version 0.170.0 is available\ncodex-cli 0.159.9")
        self.assertIn("codex 0.159.9 is older", cli.version_problem(str(self.codex)), "the codex line, not the notice")
        (self.home / "codex-version-banner").unlink()
        (self.home / "codex-version-rc").write_text("1")
        self.assertIn("did not answer", cli.version_problem(str(self.codex)))

    def test_a_root_with_a_line_break_is_refused_before_anything_is_written(self):
        rc, _, err = self.main("install", "--work", str(self.home / "a\nb"))
        self.assertEqual(rc, 2)
        self.assertIn("contains a line break", err)
        self.assertFalse(cli.config_path().exists())

    def test_an_unowned_config_is_a_final_refusal(self):
        cfg = cli.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("CODEX=/theirs\n")
        rc, _, err = self.install_with(self.runner())
        self.assertEqual((rc, cfg.read_text()), (1, "CODEX=/theirs\n"))
        self.assertIn("was not written by install; remove it by hand", err)

    def test_a_reinstall_stops_proves_free_then_replaces_and_starts(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        old = runner.pid()
        runner.calls.clear()
        rc, out, _ = self.install_with(runner, False, "--sandbox", "workspace-write")
        self.assertEqual(rc, 0)
        verbs = runner.verbs("systemctl")
        self.assertEqual(verbs[:4], ["show", "stop", "disable", "show"], "stopped and proven inactive+disabled first")
        self.assertEqual(verbs[-5:], ["daemon-reload", "enable", "restart", "show", "show"], "the shows read MainPID")
        self.assertIn("changed: ", out)
        self.assertEqual(cm.read_config(cli.config_path())["SANDBOX"], "workspace-write")
        self.assertNotEqual(runner.pid(), old, "a fresh holder, not the old connector")
        runner.table[("systemctl", "restart")] = 1
        rc, _, err = self.install_with(runner, False, "--sandbox", "read-only")
        self.assertEqual(rc, 1)
        self.assertIn("did not acquire", err)
        self.assertEqual(cm.read_config(cli.config_path())["SANDBOX"], "read-only", "the files as written; the retry is the same install")

    def test_fresh_holder_refuses_the_snapshotted_holder_and_takes_a_different_one(self):
        self.configure()
        self.work.mkdir(exist_ok=True)
        first = hold(str(self.work), cli.SHAPE)
        try:
            mine = task_io.lock_holder(str(self.work))
            with mock.patch.object(cm, "CONFIRM_TIMEOUT_S", 0.5):
                self.assertIsNone(cm._fresh_holder(L, str(self.work), mine), "the pre-apply holder itself is not fresh")
                self.assertEqual(cm._fresh_holder(L, str(self.work), {})["pid"], first.pid, "with no snapshot it is")
        finally:
            unhold(first)
        second = hold(str(self.work), cli.SHAPE)
        try:
            self.assertEqual(cm._fresh_holder(L, str(self.work), mine)["pid"], second.pid, "a different live holder is")
        finally:
            unhold(second)

    def test_a_debug_holder_that_won_the_root_is_never_taken_for_the_managed_launcher(self):
        """The manager's start lost to a hand launcher (same adapter family, same install): install fails."""
        runner = self.runner(adapter="codex-local.debug")
        rc, _, err = self.install_with(runner)
        self.assertEqual(rc, 1)
        self.assertIn("did not acquire", err)
        self.assertRegex(err, r"held by launcher pid \d+\)")
        self.assertIn("the job this operation started, was stopped", err, "so it never takes the root later")

    def test_a_root_held_by_a_session_no_job_manages_is_refused(self):
        self.work.mkdir()
        holder = hold(self.work, "other-adapter")
        try:
            rc, _, err = self.install_with(self.runner())
        finally:
            unhold(holder)
        self.assertEqual(rc, 1)
        self.assertIn("held by a session no job of", err)
        self.assertFalse(cli.config_path().exists(), "nothing was applied")


class Run(Base):
    """`run` holds the root for its own life and attaches the connector as the consumer."""

    def run_with(self, rc=0, debug=False):
        seen = {}

        def popen(argv, **kw):
            seen.update(argv=argv, env=kw["env"], cwd=kw["cwd"], stdin=kw["stdin"], pass_fds=kw["pass_fds"])
            # A real child: the attach checks its ppid and start time.
            return subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(0.4); sys.exit(%d)" % rc],
                                    stdin=subprocess.DEVNULL)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run(argparse.Namespace(debug=debug, launchd=False), popen=popen)
        return code, seen, err.getvalue()

    def test_the_connector_argv_env_and_cwd_under_the_launchers_lock(self):
        self.configure()
        code, seen, err = self.run_with()
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["argv"], [sys.executable, "-m", "ag2_sparrow.adapters.codex.codex_connector", "--work",
                                        str(self.work), "--held", "--root-fd", str(seen["pass_fds"][0]), "--codex",
                                        str(self.codex), "--server", "stdio", "--sandbox", "read-only"])
        self.assertEqual(len(seen["pass_fds"]), 1, "the held root's descriptor, and only it, reaches the connector")
        for arg in FORBIDDEN_ARGS:
            self.assertNotIn(arg, seen["argv"], arg)
        env = seen["env"]
        self.assertEqual(set(env), {"PATH", "HOME", "USER", "CODEX_HOME", "PYTHONPATH"})
        self.assertEqual(env["PYTHONPATH"], str(PKG), "the connector runs from this package")
        self.assertEqual(env["CODEX_HOME"], str(self.codex_home))
        self.assertTrue(env["PATH"].startswith(str(self.bin) + os.pathsep), "the configured codex comes first")
        for secret in ("REMOTE_TASK_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
                       "SUTANDO_WORKSPACE_DIR", cm.MANAGED_VAR):
            self.assertNotIn(secret, env)
        self.assertEqual((seen["cwd"], seen["stdin"]), (str(self.work), subprocess.DEVNULL))
        self.assertTrue((self.work / "state" / "threads").is_dir(), "the launcher makes the connector's dirs under the held root")
        self.assertEqual(task_io.lock_held(str(self.work)), "free", "released with the launcher")
        self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing, "no stale record")

    def test_no_codex_home_means_none_is_passed(self):
        self.configure(CODEX_HOME="")
        _, seen, _ = self.run_with()
        self.assertNotIn("CODEX_HOME", seen["env"])

    def test_the_connectors_own_75_is_1_and_other_exits_pass_through(self):
        self.configure()
        code, _, err = self.run_with(rc=75)
        self.assertEqual(code, 1, err)
        self.assertIn("reported as 1", err)
        self.assertEqual(self.run_with(rc=2)[0], 2)

    def test_run_is_the_managers_entrypoint_and_debug_run_the_hand_start(self):
        self.configure()
        with mock.patch.dict(os.environ, {cm.MANAGED_VAR: ""}):
            rc, _, err = self.main("run")
            self.assertEqual(rc, 2)
            self.assertIn(f"{cm.MANAGED_VAR}=1", err)
            self.assertIn("debug-run", err)
            self.assertEqual(self.run_with(debug=True)[0], 0, "debug-run skips the gate only")

    def test_debug_run_waits_for_the_operation_lock_then_refuses_without_touching_the_root(self):
        self.configure()
        holder = subprocess.Popen([sys.executable, "-c", OP_HOLDER % str(PKG)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            code, seen, err = self.run_with(debug=True)
        finally:
            unhold(holder)
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("another ag2-sparrow-codex-local operation is in progress", err)
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_debug_run_holds_under_the_debug_label_and_frees_the_operation_lock_once_acquired(self):
        self.configure()
        seen = {}

        def during(child, pidfd, state):
            seen["adapter"] = task_io.lock_holder(str(self.work)).get("adapter")
            fd = os.open(cm.oplock_path(cli.LOCAL), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["oplock_free"] = True
            except BlockingIOError:
                seen["oplock_free"] = False
            finally:
                os.close(fd)
            return child.wait()
        with mock.patch.object(cm, "_run_until_end", side_effect=during):
            self.assertEqual(self.run_with(debug=True)[0], 0)
        self.assertEqual(seen, {"adapter": "codex-local.debug", "oplock_free": True})

    def test_run_refuses_an_old_codex_and_a_broken_config(self):
        rc, _, err = self.main("run")
        self.assertEqual((rc, "unreadable" in err), (2, True))
        self.configure()
        (self.home / "codex-version").write_text("0.1.0")
        code, seen, err = self.run_with()
        self.assertEqual((code, "is older than" in err, seen), (2, True, {}))

    def test_a_held_work_root_is_75_and_starts_no_connector(self):
        self.configure()
        holder = hold(self.work, "other-adapter")
        try:
            code, seen, err = self.run_with()
        finally:
            unhold(holder)
        self.assertEqual(code, 75, err)
        self.assertIn("other-adapter", err)
        self.assertEqual(seen, {}, "no connector was spawned")

    def test_a_stop_before_the_spawn_a_spawn_error_and_a_connector_that_survives_the_kill(self):
        self.configure()
        stopped = dict(cm.stop_state(), sig=signal.SIGTERM)
        with mock.patch.object(cm, "stop_state", return_value=stopped):
            code, seen, err = self.run_with()
        self.assertEqual((code, seen, err), (128 + signal.SIGTERM, {}, "run: stopped before the connector started\n"))
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

        def raises(argv, **kw):
            raise OSError(2, "No such file")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.run(argparse.Namespace(debug=False, launchd=False), popen=raises)
        self.assertEqual((code, err.getvalue()), (2, "run: refusing to start: the connector could not be spawned (No such file)\n"))
        # The child ends first (the release would otherwise keep the flock for the live consumer), then the
        # wait reports a kill never confirmed.
        with mock.patch.object(cm, "_run_until_end", side_effect=lambda child, pidfd, state: (child.wait(), state.update(sig=signal.SIGTERM))[1]):
            code, _, err = self.run_with()
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertIn("run: the connector did not die within the bound; the manager's to clean", err)

    def test_run_launchd_is_the_shared_shim_over_this_launcher(self):
        self.configure()
        seen = []

        class Child:
            def __init__(self, rc):
                self.rc = rc

            def wait(self):
                return self.rc
        for rc, expect in ((75, 0), (0, 1), (-15, 1), (2, 2), (1, 1)):
            code = cli.run(argparse.Namespace(debug=False, launchd=True), popen=lambda argv, rc=rc: seen.append(argv) or Child(rc))
            self.assertEqual(code, expect, rc)
        self.assertEqual(seen[0], [str(self.bin / cli.COMMAND), "run"], "the console script with plain run")
        with mock.patch.object(cm, "console_script", return_value=""):
            cli.run(argparse.Namespace(debug=False, launchd=True), popen=lambda argv: seen.append(argv) or Child(75))
        self.assertEqual(seen[-1][:2], [sys.executable, "-c"])
        self.assertIn("from ag2_sparrow.adapters.codex.connect_cli import main", seen[-1][2])

    def test_a_held_root_the_connector_cannot_be_pointed_at_is_refused(self):
        """The prepare step may raise the lock's own refusal (a replaced root): its line and code, nothing spawned."""
        self.configure()
        for code, expect in ((2, 2), (3, 75)):
            with mock.patch.object(task_io, "root_path", side_effect=task_io.LockRefused(code, "refusing to start: gone")):
                rc, seen, err = self.run_with()
            self.assertEqual((rc, seen, err), (expect, {}, "refusing to start: gone\n"))
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_the_shared_reap_tolerates_a_child_that_cannot_be_killed_or_waited(self):
        calls = []

        class Stuck:
            def kill(self):
                calls.append("kill")
                raise OSError("gone")

            def wait(self, timeout=None):
                calls.append("wait")
                raise subprocess.TimeoutExpired("x", timeout)
        self.assertIsNone(cm.kill_and_reap(Stuck()))
        self.assertEqual(calls, ["kill", "wait"])

    def test_an_unobserved_root_never_reads_free(self):
        self.configure()
        with mock.patch.object(task_io, "occupancy", return_value="unobserved"):
            line = cli.occupancy_line(str(self.work))
        self.assertTrue(line.startswith("unobserved (the root's holder or consumer cannot be verified; manager "), line)

    def test_a_connector_the_attach_cannot_name_is_reaped(self):
        self.configure()
        with mock.patch.object(task_io, "attach_consumer", side_effect=holder_lock.LockError("record changed")):
            code, seen, err = self.run_with()
        self.assertEqual(code, 2, err)
        self.assertIn("no consumer: record changed", err)
        self.assertEqual(task_io.lock_held(str(self.work)), "free")


class HeldConnector(Base):
    """The connector's --held: no lock of its own, a start only once a holder attached it."""

    HOLDER = ("import sys, subprocess; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
              "o = task_io.acquire_lock(%r, 'codex-local')\n"
              "c = subprocess.Popen([sys.executable, '-c', %r, str(o.root_fd)], pass_fds=(o.root_fd,))\n"
              "task_io.attach_consumer(o, task_io.holder_record_bytes(o), c.pid, task_io.start_time(c.pid))\n"
              "print('rc', c.wait(), flush=True); task_io.release_lock(o)")
    CONNECTOR = ("import sys; sys.path.insert(0, %r); from ag2_sparrow.adapters.codex import codex_connector as cc\n"
                 "sys.exit(cc.main(['--work', %r, '--held', '--root-fd', sys.argv[1], '--server', 'stub', '--idle-exit', '0.3']))")

    def root_fd(self):
        """The work root's descriptor for an in-process --held, the test's cwd restored afterwards."""
        self.work.mkdir(exist_ok=True)
        for d in cli.WORK_DIRS:
            (self.work / d).mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.work), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.addCleanup(os.chdir, os.getcwd())
        return str(fd)

    def test_held_needs_the_root_descriptor(self):
        with self.assertRaises(SystemExit) as cm_, contextlib.redirect_stderr(io.StringIO()):
            cc.main(["--work", str(self.work), "--held", "--server", "stub"])
        self.assertEqual(cm_.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cc.main(["--work", str(self.work), "--held", "--root-fd", "999", "--server", "stub"])
        self.assertEqual((rc, "--root-fd 999 is not the held root's directory" in err.getvalue()), (2, True), err.getvalue())

    def test_held_under_the_holder_that_attached_it_serves_on_the_stub_server(self):
        """The happy path as real processes: the holder attaches its child, the child serves without a lock of its own."""
        self.work.mkdir()
        for d in cli.WORK_DIRS:
            (self.work / d).mkdir(parents=True, exist_ok=True)
        connector = covered_code(self.CONNECTOR % (str(PKG), str(self.work)))
        holder = subprocess.run([sys.executable, "-c", covered_code(self.HOLDER % (str(PKG), str(self.work), connector))],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual((holder.returncode, holder.stdout), (0, "rc 0\n"), holder.stderr)
        self.assertTrue((self.work / "state" / "app-server-stub.stderr").exists(), "the stub app-server was started")
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_held_without_a_holder_is_refused_and_takes_no_lock(self):
        fd = self.root_fd()
        err = io.StringIO()
        with mock.patch.object(cc, "HELD_WAIT_S", 0.3), contextlib.redirect_stderr(err):
            rc = bounded(lambda: cc.main(["--work", str(self.work), "--held", "--root-fd", fd, "--server", "stub"]))
        self.assertEqual(rc, 2)
        self.assertIn("no holder of %s attached this process" % self.work, err.getvalue())
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_held_whose_record_names_this_pid_without_verified_liveness_is_refused(self):
        """A stale record (a reused pid, live False) or an unreadable lineage (live None) serves nothing."""
        fd = self.root_fd()
        mine = task_io.start_time(os.getpid())
        for record, word in (({"start": mine, "live": False}, "liveness false"), ({"start": mine, "live": None}, "liveness unknown"),
                             ({"start": "bogus", "live": True}, "a start time that is not this process's")):
            err = io.StringIO()
            with mock.patch.object(cc, "holder_consumer", return_value=dict(record, pid=os.getpid())), \
                    mock.patch.object(cc, "HELD_WAIT_S", 0.3), contextlib.redirect_stderr(err):
                rc = bounded(lambda: cc.main(["--work", str(self.work), "--held", "--root-fd", fd, "--server", "stub",
                                              "--idle-exit", "0.2"]))
            self.assertEqual(rc, 2, (record, err.getvalue()))
            self.assertIn("names this pid with %s, not verified" % word, err.getvalue())
            self.assertFalse((self.work / "state" / "app-server-stub.stderr").exists(), "no app-server was started")
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_held_under_a_holder_that_attached_another_process_is_refused(self):
        fd = self.root_fd()
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sys, subprocess; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
            "o = task_io.acquire_lock(%r, 'codex-local'); c = subprocess.Popen(['sleep', '300'])\n"
            "task_io.attach_consumer(o, task_io.holder_record_bytes(o), c.pid, task_io.start_time(c.pid))\n"
            "print('held', flush=True); sys.stdin.read(); c.kill(); c.wait(); task_io.release_lock(o)")
            % (str(PKG), str(self.work))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with mock.patch.object(cc, "HELD_WAIT_S", 0.3), contextlib.redirect_stderr(io.StringIO()):
                rc = bounded(lambda: cc.main(["--work", str(self.work), "--held", "--root-fd", fd, "--server", "stub"]))
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        self.assertEqual(rc, 2)


class LauncherProcess(Base):
    """The launcher as a real process over the real connector and the stub app-server."""

    def launcher(self):
        env = isolated_child_env(PYTHONPATH=str(PKG))
        proc = subprocess.Popen([sys.executable, "-c", covered_code("import sys; from ag2_sparrow.adapters.codex import connect_cli\n"
                                                                    "sys.exit(connect_cli.main(['run']))")], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.end, proc)
        self.assertTrue(wait_for(self.server, 20), "the connector started its app-server through the fake codex")
        return proc

    @staticmethod
    def end(proc):
        """Whatever the test decided: the launcher ended and its pipes closed, so nothing leaks into the next test."""
        if proc.poll() is None:
            proc.terminate()  # the launcher reaps its connector on SIGTERM; a SIGKILL would orphan it
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.communicate(timeout=30)

    def test_the_app_server_comes_from_the_configured_executable_not_a_codex_earlier_on_path(self):
        """The executable the version check passed is the one that runs: a `codex` earlier on PATH is not."""
        selected = write_fake_codex(self.bin / "codex-selected")
        shadow = self.home / "shadow" / "bin"
        shadow.mkdir(parents=True)
        (shadow / "codex").write_text("#!%s\nimport json, os, sys\n"
                                     "json.dump({'argv': sys.argv}, open(os.path.join(os.environ['HOME'], 'codex-shadow.json'), 'w'))\n"
                                     "sys.exit(7)\n" % sys.executable)
        (shadow / "codex").chmod(0o755)
        self.configure(CODEX=str(selected))
        with mock.patch.dict(os.environ, {"PATH": f"{shadow}:{self.bin}:/usr/bin:/bin"}):
            launcher = self.launcher()
        self.assertEqual(self.server()["argv"][0], str(selected) + ".py", "the wrapper's body: the selected executable ran")
        self.assertFalse((self.home / "codex-shadow.json").exists(), "the shadowing codex never ran")
        os.kill(launcher.pid, signal.SIGTERM)
        launcher.communicate(timeout=60)
        self.assertEqual(launcher.returncode, 143)

    def answered(self, root, task_id, channel):
        """A task on `channel` written into `root`, its result text once the connector answered it."""
        (Path(root) / "tasks" / (task_id + ".txt")).write_text(TASK % (task_id, channel))
        path = Path(root) / "results" / (task_id + ".txt")
        self.assertTrue(wait_for(path.exists, 30), task_id)
        return path.read_text()

    def test_a_root_renamed_and_replaced_after_startup_is_served_by_inode_and_the_replacement_untouched(self):
        """The connector lives in the held inode: after an owner channel has completed a turn, the pathname is
        renamed and recreated and the replacement held by another process; the next turn on that channel,
        and a fresh channel's, work in the MOVED root (the turn's cwd, not the thread's remembered one),
        results land in the held inode, and the replacement never loses a task to this connector."""
        self.configure()
        launcher = self.launcher()
        self.assertTrue(wait_for(lambda: (task_io.holder_consumer(str(self.work)) or {}).get("live")), "the connector attached")
        self.assertIn("cwd=%s]" % self.work, self.answered(self.work, "task-a", "chan-a"), "the first turn, at the pathname")
        moved = self.home / "work.moved"
        os.rename(self.work, moved)
        for d in cli.WORK_DIRS:
            (self.work / d).mkdir(parents=True)
        replacement = hold(self.work, "other-adapter")
        try:
            self.assertIn("cwd=%s]" % moved, self.answered(moved, "task-c", "chan-a"),
                          "the same channel's next turn follows the moved root, not the thread's old cwd")
            self.assertIn("cwd=%s]" % moved, self.answered(moved, "task-d", "chan-b"), "a fresh channel too")
            (self.work / "tasks" / "task-b.txt").write_text(TASK % ("task-b", "chan-a"))
            self.assertFalse(wait_for(lambda: (self.work / "results" / "task-b.txt").exists(), 2.0),
                             "the replacement root is another holder's")
            self.assertTrue((self.work / "tasks" / "task-b.txt").exists())
            self.assertEqual(sorted(os.listdir(self.work / "state")), ["threads"], "nothing of this connector's state landed there")
            self.assertEqual(sorted(os.listdir(self.work / "results")), [], "the replacement has no connector results")
            self.assertTrue((moved / "state" / "app-server-stdio.stderr").exists())
        finally:
            unhold(replacement)
        os.kill(launcher.pid, signal.SIGTERM)
        launcher.communicate(timeout=60)
        self.assertEqual(launcher.returncode, 143)
        self.assertEqual(task_io.lock_held(str(moved)), "free")

    def test_sigterm_reaps_the_connector_and_frees_the_work_root(self):
        self.configure()
        launcher = self.launcher()
        self.assertTrue(wait_for(lambda: (task_io.holder_consumer(str(self.work)) or {}).get("live")), "the connector attached")
        consumer, rec = task_io.holder_consumer(str(self.work)), task_io.lock_holder(str(self.work))
        self.assertEqual((rec.get("adapter"), rec.get("owner_pid")), ("codex-local", launcher.pid),
                         "the launcher, not the connector, holds the lock")
        self.assertTrue(consumer and consumer.get("live") and consumer["pid"] != launcher.pid,
                        "the connector is the attached consumer: %r" % (consumer,))
        connector_pid = consumer["pid"]
        self.assertEqual(self.server()["env"].get("CODEX_HOME"), str(self.codex_home), "CODEX_HOME reached the app-server")
        self.assertNotIn("REMOTE_TASK_TOKEN", self.server()["env"])
        self.assertIn("held by launcher pid %d, its connector pid %d" % (launcher.pid, connector_pid),
                      cli.occupancy_line(str(self.work)))
        os.kill(launcher.pid, signal.SIGTERM)
        _, err = launcher.communicate(timeout=60)
        self.assertEqual(launcher.returncode, 143, err)
        self.assertTrue(wait_for(lambda: task_io.lock_held(str(self.work)) == "free"), "released with the launcher")
        self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing, "no stale record")
        self.assertTrue(wait_for(lambda: not task_io.pid_alive(connector_pid)), "the connector is still alive")


class VerifyUninstall(Base):
    def test_verify_every_part_and_the_broken_ones(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, _ = self.main("verify")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.checks(out)), {"config", "codex", "login", "inbox", "results", "relay token", "supervisor"})
        self.assertEqual(set(self.checks(out).values()), {"PASS"})
        self.assertIn("INFO sandbox: read-only", out)
        self.assertIn("INFO non-owner: refused unless", out)
        self.assertIn(f"INFO job: {L.service_name} active", out)
        self.assertIn(f"INFO session: {self.work} held by launcher pid {runner.pid()}, no live connector attached "
                      "(manager active/running)", out)
        def raises(*a, **kw):
            raise OSError("no exec")
        self.assertEqual(cli.login_problem(cm.read_config(cli.config_path()), run_=raises), "codex login status did not run (OSError)")
        login_env = json.loads((self.home / "codex-login.json").read_text())["env"]
        self.assertEqual(login_env["CODEX_HOME"], str(self.codex_home))
        self.assertNotIn("REMOTE_TASK_TOKEN", login_env)
        (self.home / "codex-login-rc").write_text("1")
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["login"], "FAIL")
        self.assertIn("not logged in under CODEX_HOME=", out)
        (self.home / "codex-login-rc").unlink()
        (self.home / "codex-version").write_text("0.150.0")
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["codex"], "FAIL")
        self.assertNotIn("login", self.checks(out))
        (self.home / "codex-version").unlink()
        self.relay.chmod(0o644)
        self.assertEqual(self.checks(self.main("verify")[1])["relay token"], "FAIL")
        self.relay.chmod(0o600)
        runner.stop_session()
        out = self.main("verify")[1]
        self.assertIn(f"INFO session: {self.work} free (manager unknown)", out, "a free lock under an unprobeable job is never plain free")
        cm.service_path(L).unlink()
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, self.checks(out)["supervisor"]), (1, "FAIL"))
        cli.config_path().write_text(cli.config_path().read_text().replace("CODEX=", "CODEX_WAS="))
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, "lacks CODEX" in out), (1, True))

    def test_uninstall_fails_closed_then_removes_only_marked_files(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        unit = cm.service_path(L)
        for table in ({("systemctl", "stop"): 1}, {("systemctl", "stop"): None}):
            with mock.patch.object(sm, "_run", side_effect=self.runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn("were kept so a rerun knows the work root", err)
            self.assertTrue(unit.exists() and cli.config_path().exists())
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertFalse(unit.exists() or cli.config_path().exists())
        self.assertIn(f"kept: {self.relay} (Sparrow's token) and the Codex login (CODEX_HOME) and the work root with state/threads", out)
        self.assertEqual(self.relay.read_text(), f"REMOTE_TASK_TOKEN={TOKEN}\n")
        self.assertTrue(self.codex_home.is_dir() and (self.work / "tasks").is_dir())
        self.assertEqual(task_io.lock_held(str(self.work)), "free")
        mac = self.runner()
        self.assertEqual(self.install_with(mac, True)[0], 0)
        plist = cm.plist_path(L)
        with mock.patch.object(cm, "is_macos", return_value=True):
            with mock.patch.object(sm, "_run", side_effect=self.runner({("launchctl", "bootout"): 3, ("launchctl", "print"): 0})):
                self.assertEqual(self.main("uninstall")[0], 1)
            self.assertTrue(plist.exists())
            with mock.patch.object(sm, "_run", side_effect=mac):
                self.assertEqual(self.main("uninstall")[0], 0)
            self.assertFalse(plist.exists())
        cfg = cli.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("CODEX=/theirs\n")
        rc, _, err = self.main("uninstall")
        self.assertEqual((rc, cfg.read_text()), (1, "CODEX=/theirs\n"))
        self.assertIn("remove it by hand", err)


class ConnectorEnv(unittest.TestCase):
    def test_the_connectors_child_env_passes_codex_home_only_when_set(self):
        with mock.patch.dict(os.environ, {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/h/.codex-agent", "OPENAI_API_KEY": "no"},
                             clear=True):
            self.assertEqual(cc.child_env(), {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/h/.codex-agent"})
        with mock.patch.dict(os.environ, {"PATH": "/bin", "HOME": "/h"}, clear=True):
            self.assertEqual(cc.child_env(), {"PATH": "/bin", "HOME": "/h"})


class FakeCodexWrapper(unittest.TestCase):
    """The harness's fake codex works through an interpreter path containing a space."""

    def test_the_wrapper_execs_the_quoted_interpreter_from_a_path_with_a_space(self):
        with tempfile.TemporaryDirectory() as d:
            spaced = Path(d) / "py dir" / "python3"
            spaced.parent.mkdir()
            spaced.symlink_to(sys.executable)
            home = Path(d) / "home"
            home.mkdir()
            codex = write_fake_codex(Path(d) / "codex", py=str(spaced))
            self.assertEqual(codex.read_text().splitlines()[0], "#!/bin/sh")
            r = subprocess.run([str(codex), "--version"], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(home)}, cwd=d)
            self.assertEqual((r.returncode, r.stdout), (0, "codex-cli 0.160.1\n"), r.stderr)


AGENT = "@probe.agent:ag2.space"
BASE = "https://backend.example/api"
CODE = "acp.code-MMMM3333"
BEARER = "redeemed-bearer-NNNN4444"
RELAY = "https://relay.backend.example/relay"  # on a host under BASE's
B = cli.BRIDGE


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
    """The redemption endpoint: the statuses answered in order, then 200 with the credential."""

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


class BridgeFake:
    """The agent bridge's own manager state: commands naming its unit or label answer here, the rest go to
    the launcher's `runner`."""

    def __init__(self, runner):
        self.runner, self.calls, self.active, self.enabled, self.registered = runner, [], False, False, False

    def __call__(self, cmd, timeout=30):
        if not any(B.service_name in a or B.agent_label in a for a in cmd):
            return self.runner(cmd, timeout)
        self.calls.append(list(cmd))
        verb = cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))
        out = ""
        if verb == "show":
            loaded = cm.service_path(B).exists() or self.registered
            words = {"LoadState": "loaded" if loaded else "not-found", "ActiveState": "active" if self.active else "inactive",
                     "UnitFileState": "enabled" if self.enabled else "disabled"}
            out = "".join(words[k] + "\n" for k in cmd[cmd.index("-p") + 1].split(","))
        elif verb in ("enable", "disable"):
            self.enabled = verb == "enable"
        elif verb in ("restart", "bootstrap"):
            self.active = self.registered = True
        elif verb in ("stop", "bootout"):
            self.active = False
        return subprocess.CompletedProcess(cmd, 0, out, "")



def seed_seat(seat, bearer):
    """A seat as another writer left it: the redeemed shape, written by the shared credential writer."""
    assert _sources.write_seat_credentials(seat, agent_id=AGENT, instance="c" * 32, tier="owner",
                                           relay_url=RELAY, bearer=bearer) == ""
    return seat


class ConnectCode(Base):
    """`connect --base --code`: connect_bridge's preflight, redemption and seat, then install with the seat,
    which wires the agent's bridge from the same launch env as claude-local's."""

    def setUp(self):
        super().setUp()
        self.root = self.home / ".ag2-sparrow" / "agents" / "probe.agent_ag2.space"
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(self.home / "host-state")   # served_elsewhere's, not the host's
        scripts = mock.patch.object(cm, "console_script", side_effect=lambda shape: str(self.bin / shape.command))
        scripts.start()
        self.addCleanup(scripts.stop)

    def fake(self):
        r = Runner(None, 0, str(self.root))
        self.runners.append(r)
        return BridgeFake(r)

    def connect(self, backend, fake=None, *extra, sleeps=None, stdin=None, base=BASE, code_args=("--code", "-")):
        fake = fake or self.fake()
        argv = ["connect", "--experimental-connect", "--base", base, *code_args, "--codex-home", str(self.codex_home),
                *extra]
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(CODE + "\n") if stdin is None else stdin
        with mock.patch.object(sm, "_run", side_effect=fake), mock.patch.object(cm, "is_macos", return_value=False), \
                mock.patch.object(urllib.request, "urlopen", side_effect=backend), \
                mock.patch.object(cm, "redeem_code", side_effect=functools.partial(
                    cm.redeem_code, sleep=lambda s: (sleeps if sleeps is not None else []).append(s))), \
                mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def seat(self):
        return cb.bridge_home(self.root) / "channels" / "ag2space"

    def test_connect_is_off_without_the_experimental_flag_and_touches_nothing(self):
        backend, fake, stdin = Backend(), self.fake(), io.StringIO(CODE + "\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sm, "_run", side_effect=fake), mock.patch.object(cm, "is_macos", return_value=False), \
                mock.patch.object(urllib.request, "urlopen", side_effect=backend), \
                mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["connect", "--base", BASE, "--code", "-", "--codex-home", str(self.codex_home)])
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual(err.getvalue(), "error: Codex connect is not enabled in this release; use install --work / "
                                         "verify\n")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(backend.calls, [], "no code is redeemed")
        self.assertEqual(stdin.read(), CODE + "\n", "the code is not even read")
        self.assertEqual(fake.runner.calls, [], "no manager is asked anything")
        self.assertFalse(self.root.exists(), "no root")
        self.assertFalse(cb.bridge_home(self.root).exists(), "no seat")
        self.assertFalse(cm.agent_lock_path(self.root).parent.exists(), "no agent lock")
        self.assertFalse(cli.config_path().exists(), "no config")
        self.assertFalse(cm.supervisor_path(cli.LOCAL).exists(), "no unit")
        self.assertFalse(cm.supervisor_path(cb.bridge_shape(cli.LOCAL)).exists(), "no bridge unit")
        with contextlib.redirect_stderr(io.StringIO()) as bare:
            self.assertEqual(cli.main(["connect"]), cli.EXIT_UNUSABLE, "the refusal comes before any argument check")
        self.assertIn("not enabled in this release", bare.getvalue())

    def assert_code_unspent(self, backend, rc, err):
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual(backend.calls, [], "the backend is never asked")
        self.assertIn("the code was not used", err)
        self.assertFalse(self.seat().exists())
        self.assertFalse(cli.config_path().exists())

    def bearer_files(self):
        found = []
        for path in self.home.rglob("*"):
            if path.is_file() and not path.is_symlink() and BEARER.encode() in path.read_bytes():
                found.append(path)
        return found

    def test_a_code_is_redeemed_once_and_the_seat_feeds_the_bridge_install_wires(self):
        self.relay.unlink()     # a fresh host: nothing but the code
        backend = Backend()
        rc, out, err = self.connect(backend)
        self.assertEqual(rc, 0, err)
        self.assertEqual(backend.calls, [(BASE + "/connect/poll", {"device_code": CODE}, "POST")])
        self.assertNotIn("warning", err)
        seat = self.seat()
        self.assertEqual(sorted(p.name for p in seat.iterdir()), [".env", "token"])
        for name in (".env", "token"):
            self.assertEqual(stat.S_IMODE((seat / name).stat().st_mode), 0o600, name)
        self.assertEqual((seat / "token").read_text(), f"{RELAY}|{BEARER}\n")
        values = dict(ln.split("=", 1) for ln in (seat / ".env").read_text().splitlines())
        self.assertEqual((values["AGENT_MXID"], values["REMOTE_TASK_TIER"], values["REMOTE_TASK_TOKEN_FILE"]),
                         (AGENT, "owner", str(seat / "token")))
        self.assertRegex(values["GATEWAY_INSTANCE"], r"^[0-9a-f]{32}$")
        cfg = cm.read_config(cli.config_path())
        self.assertEqual((cfg["ROOT"], cfg["TOKEN_ENV_FILE"], cfg["AGENT_ID"], cfg["BRIDGE"], cfg["INBOX_DIR"]),
                         (str(self.root), str(seat / ".env"), AGENT, cb.UNIT, str(self.root / "tasks")))
        unit = cm.service_path(B).read_text()
        self.assertTrue(unit.startswith("# Written by ag2-sparrow-codex-local install"))
        self.assertIn(f"ExecStart={self.bin / 'ag2-sparrow'}\n", unit)
        for line in ("REMOTE_TASK_SEAT=1", f"REMOTE_TASK_TOKEN_FILE={seat / 'token'}", f"AGENT_MXID={AGENT}",
                     f"AGENT_CONNECT_TASK_DIR={self.root}/tasks", f"AGENT_CONNECT_RESULT_DIR={self.root}/results",
                     f"AGENT_CONNECT_STATE_DIR={self.root}.bridge/state"):
            self.assertIn(f'Environment="{line}"\n', unit)
        self.assertEqual(unit, cm.render_supervisor(B, cb.launch_env(dict(cfg))[0])[0], "connect_bridge's launch env")
        self.assertIn("the managed launcher acquired the expected root", out)
        self.assertIn(f"started: {B.service_name}", out)
        self.assertIn("setup: built-in", out)
        self.assertNotIn(BEARER, out + err)
        self.assertEqual(self.bearer_files(), [seat / "token"], "the bearer is in the seat's token file and nowhere else")

    def test_the_instance_is_minted_once_and_kept_across_a_reconnect(self):
        fake = self.fake()
        self.assertEqual(self.connect(Backend(), fake)[0], 0)
        first = (self.seat() / ".env").read_text()
        fake.runner.stop_session()          # a rotation is stop, then connect
        rotated = Backend(answer={"relay_url": RELAY, "bearer": "rotated-bearer-OOOO5555", "agent_id": AGENT})
        rc, _out, err = self.connect(rotated, fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.seat() / ".env").read_text(), first)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|rotated-bearer-OOOO5555\n")

    def test_a_404_says_why_the_code_is_gone_and_writes_nothing(self):
        for body, why in (({"error": "gone"}, "already used or has expired; make a new one in s.space"),
                          ({"error": "agent_revoked"}, "the agent was removed in s.space; add it again"),
                          ({}, "already used or has expired")):
            with self.subTest(body=body):
                rc, _out, err = self.connect(Backend((404, body)))
                self.assertEqual(rc, cli.EXIT_UNUSABLE)
                self.assertIn(why, err)
                self.assertFalse(self.seat().exists())
                self.assertFalse(cli.config_path().exists())

    def test_503_and_202_are_retried_then_redeemed(self):
        sleeps = []
        backend = Backend(503, (202, {"state": "redeeming", "retry_after": 3}))
        rc, _out, err = self.connect(backend, sleeps=sleeps)
        self.assertEqual(rc, 0, err)
        self.assertEqual((len(backend.calls), sleeps), (3, [1, 3.0]))

    def test_503_or_202_throughout_gives_up_with_the_code_still_valid(self):
        sleeps = []
        rc, _out, err = self.connect(Backend(*[503] * 10), sleeps=sleeps)
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual(sleeps, list(cm.REDEEM_BACKOFF_S))
        self.assertIn("the code is still valid", err)
        self.assertFalse(self.seat().exists())
        sleeps = []
        rc, _out, err = self.connect(Backend(*[(202, {"retry_after": 999})] * 10), sleeps=sleeps)
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual(sleeps, [cm.RETRY_AFTER_MAX_S] * len(cm.REDEEM_BACKOFF_S), "capped")
        self.assertIn("still redeeming the code; run connect again with the same code", err)
        self.assertFalse(self.seat().exists())

    def test_no_code_is_refused_off_a_terminal_and_asked_for_on_one(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        backend = Backend()
        rc, _out, err = self.connect(backend, stdin=io.StringIO(""), code_args=())
        self.assert_code_unspent(backend, rc, err)
        self.assertIn("pass --code - and pipe the code in", err)
        with mock.patch.object(cm.getpass, "getpass", return_value=CODE) as asked:
            rc, _out, err = self.connect(Backend(), stdin=Tty(""), code_args=())
        self.assertEqual((rc, asked.call_count), (0, 1), err)

    def test_a_code_on_the_command_line_still_works_but_warns(self):
        rc, _out, err = self.connect(Backend(), None, code_args=("--code", CODE))
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: a code on the command line is kept by ps and shell history", err)

    def test_an_http_base_or_a_malformed_code_is_refused_before_anything_runs(self):
        for kw in ({"base": "http://backend.example/api"}, {"base": "ftp://backend.example"},
                   {"code_args": ("--code", "web.code-1")}):
            with self.subTest(kw=kw):
                backend = Backend()
                rc, _out, err = self.connect(backend, **kw)
                self.assert_code_unspent(backend, rc, err)
        loop = Backend(answer={"relay_url": "http://127.0.0.1:9/relay", "bearer": BEARER, "agent_id": AGENT})
        rc, _out, err = self.connect(loop, base="http://127.0.0.1:9/api")
        self.assertEqual(rc, 0, err)

    def test_a_relay_url_that_is_http_or_off_host_is_never_written(self):
        for relay, why in (("http://relay.backend.example/relay", "is not https"),
                           ("https://backend.example.co.uk/relay", "--trust-relay-host backend.example.co.uk"),
                           ("https://elsewhere.test/relay", "--trust-relay-host elsewhere.test")):
            with self.subTest(relay=relay):
                rc, _out, err = self.connect(Backend(answer={"relay_url": relay, "bearer": BEARER, "agent_id": AGENT}))
                self.assertEqual(rc, cli.EXIT_UNUSABLE)
                self.assertIn(why, err)
                self.assertIn("the code is spent but the seat was not written", err)
                self.assertFalse(self.seat().exists())
                self.assertNotIn(BEARER, err)
        rc, _out, err = self.connect(Backend(answer={"relay_url": "https://elsewhere.test/relay", "bearer": BEARER,
                                                     "agent_id": AGENT}), None, "--trust-relay-host", "elsewhere.test")
        self.assertEqual(rc, 0, err)

    def test_every_preflight_refusal_leaves_the_code_unspent(self):
        cases = (("logged out", lambda: (self.home / "codex-login-rc").write_text("1"), "not logged in under CODEX_HOME"),
                 ("too old", lambda: (self.home / "codex-version").write_text("0.157.0"), "older than the 0.160.1"),
                 ("no codex", lambda: self.codex.unlink(), "codex"),
                 ("task dir set", lambda: os.environ.__setitem__("AGENT_CONNECT_TASK_DIR", str(self.home / "q")),
                  "AGENT_CONNECT_TASK_DIR is set"))
        for name, arrange, why in cases:
            with self.subTest(name), mock.patch.dict(os.environ):
                arrange()
                backend = Backend()
                rc, _out, err = self.connect(backend)
                self.assert_code_unspent(backend, rc, err)
                self.assertIn(why, err)
            for f in ("codex-login-rc", "codex-version"):
                (self.home / f).unlink(missing_ok=True)
            write_fake_codex(self.bin / "codex")

    def test_under_a_host_supervisor_connect_declares_the_seat_with_no_vault_key(self):
        backend, fake = Backend(), self.fake()
        with mock.patch.object(_dirs, "bridge_host", return_value="sparrowd"):
            rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(cm.service_path(B).exists(), "the supervisor runs it: no unit of its own")
        cfg = cm.read_config(cli.config_path())
        seat_env = cb.seat_file(self.root)
        self.assertEqual((cfg["BRIDGE"], cfg["TOKEN_ENV_FILE"], cfg["TOKEN_VAULT_KEY"]), ("sparrowd", seat_env, ""))
        decl = cb.bridge_home(self.root) / cb.DECLARATION
        body = json.loads(decl.read_text())
        self.assertEqual((body["written_by"], body["host"], body["token_vault_key"]),
                         (f"{cli.COMMAND} install", "sparrowd", ""))
        self.assertEqual(body["env"], cb.launch_env(dict(cfg))[0], "connect_bridge's launch env")
        self.assertEqual((body["env"]["AG2_DEVICE_ENV"], body["env"]["REMOTE_TASK_TOKEN_FILE"], body["env"]["REMOTE_TASK_SEAT"]),
                         (seat_env, str(self.seat() / "token"), "1"))
        self.assertIn(f"with the seat {seat_env} and the token file it names", out)
        self.assertNotIn("vault set", out)
        self.assertNotIn(BEARER, decl.read_text() + out + err)

    def test_under_a_host_supervisor_install_takes_only_the_seat_beside_the_root(self):
        self.root.mkdir(parents=True)
        for seat in (self.home / "seat", cb.bridge_home(self.root)):
            seed_seat(seat, BEARER)
        elsewhere = cm.seat_env_path(self.home / "seat")
        with mock.patch.object(_dirs, "bridge_host", return_value="sparrowd"), \
                mock.patch.object(sm, "_run", side_effect=self.fake()):
            rc, _out, err = self.main("install", "--token-env-file", elsewhere, "--codex-home", str(self.codex_home))
            self.assertEqual(rc, cli.EXIT_UNUSABLE)
            self.assertIn(f"is the agent's seat, {cb.seat_file(self.root)}", err)
            self.assertFalse(cli.config_path().exists())
            rc, _out, err = self.main("install", "--token-env-file", cb.seat_file(self.root),
                                      "--codex-home", str(self.codex_home))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads((cb.bridge_home(self.root) / cb.DECLARATION).read_text())["env"]["AG2_DEVICE_ENV"],
                         cb.seat_file(self.root))

    def seated_by_another_adapter(self):
        """The agent's root and seat as another adapter left them: its credential in the seat."""
        self.root.mkdir(parents=True, exist_ok=True)
        seed_seat(cb.bridge_home(self.root), "other-bearer-PPPP6666")
        return {p: p.read_bytes() for p in sorted(self.seat().iterdir())}

    def test_a_root_another_adapter_holds_is_refused_before_the_seat_is_written(self):
        before = self.seated_by_another_adapter()
        holder = hold(self.root, "claude-local")
        try:
            backend = Backend()
            rc, _out, err = self.connect(backend)
        finally:
            unhold(holder)
        self.assertEqual({p: p.read_bytes() for p in sorted(self.seat().iterdir())}, before, "byte-identical")
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn(f"{self.root} is held by launcher pid {holder.pid}; stop what holds it", err)
        self.assertIn("the code is spent and nothing was written", err)
        self.assertEqual(len(backend.calls), 1, "the root is known only from the redemption's agent id")
        self.assertFalse(cli.config_path().exists())
        self.assertFalse(cm.service_path(B).exists())

    def test_a_free_root_passes_installs_own_check_and_connect_proceeds(self):
        self.seated_by_another_adapter()
        checked = []
        real = cm.under_root
        with mock.patch.object(cm, "under_root",
                               side_effect=lambda shape, root, act, **kw: checked.append((shape.name, root)) or real(shape, root, act, **kw)):
            rc, _out, err = self.connect(Backend())
        self.assertEqual(rc, 0, err)
        self.assertEqual(checked, [(cli.SHAPE, str(self.root))], "asked once, before the seat")
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|{BEARER}\n")

    def files(self):
        """Every file under the test home, by bytes, but the fake codex's call log and the empty operation
        lock files: what a refused connect may not change."""
        return {str(p): p.read_bytes() for p in sorted(self.home.rglob("*")) if p.is_file() and p.suffix != ".oplock" and p.name != cm.SEAT_LOCK and p.parent.name != ".locks"
                and not (p.parent == self.home and p.name.startswith("codex-"))}

    def assert_refused_untouched(self, fake, why, spent="the code is spent and nothing was written", unspent=False):
        """connect refuses before the seat, the agent's seat byte-identical and no file anywhere changed;
        `unspent`: refused before the code is redeemed at all."""
        seat = self.seated_by_another_adapter()
        before, backend = self.files(), Backend()
        rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertEqual({p: p.read_bytes() for p in sorted(self.seat().iterdir())}, seat, "byte-identical")
        self.assertEqual(self.files(), before, "nothing else was written")
        self.assertNotIn("connected:", out)
        self.assertIn(why, err)
        self.assertIn("the code was not used" if unspent else spent, err)
        self.assertEqual(len(backend.calls), 0 if unspent else 1)

    def test_a_job_active_for_another_root_does_not_vouch_for_a_held_root(self):
        a = self.home / "agent-a"
        fake = BridgeFake(Runner(None, 0, str(a)))
        self.runners.append(fake.runner)
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, _out, err = self.main("install", "--work", str(a), "--codex-home", str(self.codex_home))
        self.assertEqual(rc, 0, err)
        self.root.mkdir(parents=True)
        holder = hold(self.root, "claude-local")
        try:
            self.assert_refused_untouched(fake, f"{cm.job_name(L)} is running for", unspent=True)
        finally:
            unhold(holder)

    def test_an_active_job_that_cannot_be_proven_to_manage_the_root_fails_closed(self):
        cases = (("no config", None, cli.SHAPE, "active for no recorded work root"),
                 ("no recorded root", "", cli.SHAPE, "active for no recorded work root"),
                 ("unparsable config", "unparsable", cli.SHAPE, "active for no recorded work root"),
                 ("another holder, this root", "root", "claude-local", "is held by claude-local pid"),
                 ("this adapter, another root", "elsewhere", cli.SHAPE, "is held by codex-local pid"))
        for name, root, adapter, why in cases:
            with self.subTest(name):
                shutil.rmtree(self.root.parent.parent, ignore_errors=True)
                root = str(self.home / "agent-a") if root == "elsewhere" else root
                cli.config_path().unlink(missing_ok=True)
                if root is not None:
                    self.configure(ROOT=str(self.root) if root in ("root", "unparsable") else root)
                if root == "unparsable":
                    cli.config_path().write_text(cli.config_path().read_text() + "ROOT='unclosed\n")
                runner = Runner(None, 0, str(self.root), adapter)
                self.runners.append(runner)
                self.root.mkdir(parents=True)
                runner.start_session()
                try:
                    self.assert_refused_untouched(BridgeFake(runner), f"{cm.job_name(L)} is running for", unspent=True)
                finally:
                    runner.stop_session()

    def test_an_independent_holder_of_this_shape_on_the_root_is_refused_while_the_job_serves_another(self):
        a = self.home / "agent-a"
        fake = BridgeFake(Runner(None, 0, str(a)))
        self.runners.append(fake.runner)
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("install", "--work", str(a), "--codex-home", str(self.codex_home))[0], 0)
        self.root.mkdir(parents=True)
        holder = hold(self.root, cli.SHAPE)
        try:
            self.assert_refused_untouched(fake, f"{cm.job_name(L)} is running for", unspent=True)
        finally:
            unhold(holder)

    def test_a_stale_record_of_this_shape_does_not_vouch_for_a_successor_that_has_not_published(self):
        self.configure(ROOT=str(self.root))
        self.root.mkdir(parents=True)
        dead = hold(self.root, cli.SHAPE)
        dead.kill()
        dead.wait(timeout=30)
        dead.stdin.close()
        dead.stdout.close()
        successor = subprocess.Popen([sys.executable, "-c", SUCCESSOR % (str(PKG), str(self.root))],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        runner = Runner(None, 0, str(self.root))
        runner.session = successor                       # the manager reads active while it lives
        self.runners.append(runner)
        try:
            self.assertEqual(successor.stdout.readline().strip(), "held")
            rec = task_io.lock_holder(str(self.root))
            self.assertEqual((rec["adapter"], rec["pid"], rec["live"]), (cli.SHAPE, dead.pid, False))
            self.assertIn("is held by a holder with no live record", cm._managed_here(L, str(self.root)),
                          "install's own check needs the record to be live")
            self.assert_refused_untouched(BridgeFake(runner), f"{cm.job_name(L)} is running for", unspent=True)
        finally:
            runner.stop_session()

    def replace_root(self):
        """A real process parks the root and its seat, seats a fresh root at the same paths with another
        token and holds it with the other adapter: the held directories are no longer what the paths name."""
        p = subprocess.Popen([sys.executable, "-c", REPLACER % (str(PKG), str(self.root), RELAY, AGENT, "claude-local")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def assert_replaced_and_refused(self, target, wrap, why):
        """connect with connect_common.`target` wrapped by `wrap(real, replace)`: it refuses, the replacing
        root's seat keeps its own token, and the parked seat holds its previous bytes, never the new bearer."""
        before = {p.name: p.read_bytes() for p in self.seated_by_another_adapter()}
        late, inode = [], []

        def replace():
            late.append(self.replace_root())
            inode.append(os.stat(self.seat() / "token").st_ino)
        try:
            if target == "os.replace":
                with mock.patch("os.replace", side_effect=wrap(os.replace, replace)):
                    rc, out, err = self.connect(Backend())
            else:
                with mock.patch.object(cm, target, side_effect=wrap(getattr(cm, target), replace)):
                    rc, out, err = self.connect(Backend())
        finally:
            for p in late:
                unhold(p)
        self.assertEqual(len(late), 1, "replaced once")
        self.assertEqual(os.stat(self.seat() / "token").st_ino, inode[0], "the replacing seat's token never replaced")
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertNotIn("connected:", out)
        self.assertIn(why, err)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|replacer-bearer-RRRR7777\n",
                         "the replacing root's seat is untouched")
        parked = Path(str(self.seat().parent.parent) + ".parked") / "channels" / "ag2space"
        self.assertEqual({p.name: p.read_bytes() for p in parked.iterdir()}, before, "the parked seat as it was")

    def test_a_root_replaced_after_connect_holds_it_keeps_both_seats(self):
        def wrap(real, replace):
            return lambda root, adapter: (real(root, adapter), replace())[0]
        self.assert_replaced_and_refused("acquire_lock", wrap, "is no longer what connect holds")

    def test_a_root_replaced_between_the_check_and_the_write_keeps_both_seats(self):
        def wrap(real, replace):
            calls = []

            def checked(owner, root):
                got = real(owner, root)
                if not calls:
                    replace()
                calls.append(got)
                return got
            return checked
        self.assert_replaced_and_refused("root_is_held", wrap, "was replaced while the seat was written")

    def test_a_write_that_fails_partway_after_a_replacement_puts_the_held_seat_back(self):
        def wrap(real, replace):
            failed = []

            def committing(src, dst, *a, **kw):
                if os.path.basename(dst) == ".env" and not failed:
                    failed.append(replace())
                    raise PermissionError(13, "Permission denied")
                return real(src, dst, *a, **kw)
            return committing
        self.assert_replaced_and_refused("os.replace", wrap, "could not be written (Permission denied); its "
                                                             "previous files were put back")

    def test_a_root_replaced_after_the_write_keeps_both_seats(self):
        def wrap(real, replace):
            calls = []

            def checked(owner, root):
                if calls:
                    replace()
                calls.append(True)
                return real(owner, root)
            return checked
        self.assert_replaced_and_refused("root_is_held", wrap, "was replaced while the seat was written")

    def test_a_symlinked_seat_path_is_refused_and_nothing_is_written_through_it(self):
        elsewhere = self.home / "elsewhere-seat"
        seed_seat(elsewhere, "elsewhere-bearer-SSSS8888")
        target = {p: p.read_bytes() for p in elsewhere.rglob("*") if p.is_file()}
        home = cb.bridge_home(self.root)
        for name in ("seat dir", "channel dir", "token"):
            with self.subTest(name):
                home.unlink() if home.is_symlink() else shutil.rmtree(home, ignore_errors=True)
                self.root.mkdir(parents=True, exist_ok=True)
                if name == "seat dir":
                    link, to = home, elsewhere
                else:
                    seed_seat(home, "other-bearer-PPPP6666")
                    link = self.seat() if name == "channel dir" else self.seat() / "token"
                    to = elsewhere / link.relative_to(home)
                    shutil.rmtree(link) if link.is_dir() else link.unlink()
                link.symlink_to(to)
                rc, out, err = self.connect(Backend())
                self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
                self.assertNotIn("connected:", out)
                self.assertIn("the seat is unchanged; the code is spent", err)
                self.assertEqual(os.readlink(link), str(to), "the link itself is left as it was")
                self.assertEqual({p: p.read_bytes() for p in elsewhere.rglob("*") if p.is_file()}, target,
                                 "nothing written through the link")

    def test_a_successor_on_a_replaced_root_keeps_its_seat_write_over_the_older_rollback(self):
        self.seated_by_another_adapter()
        real, successor, early = cm.root_is_held, [], []

        def checked(owner, root):
            if successor or not hasattr(checked, "pre"):
                checked.pre = True
                return real(owner, root)
            p = subprocess.Popen([sys.executable, "-c", SUCCESSOR_WRITER % (str(PKG), str(self.root), RELAY, AGENT,
                                                                             "claude-local")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            successor.append(p)
            self.assertEqual(p.stdout.readline().strip(), "held")
            if select.select([p.stdout], [], [], 3)[0]:     # the older writer's seat lock holds it off
                early.append(p.stdout.readline().strip())
            return real(owner, root)
        try:
            with mock.patch.object(cm, "root_is_held", side_effect=checked):
                rc, out, err = self.connect(Backend())
            p = successor[0]
            line = early[0] if early else (p.stdout.readline().strip() if select.select([p.stdout], [], [], 30)[0]
                                           else "no answer")
        finally:
            for p in successor:
                unhold(p)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertNotIn("connected:", out)
        self.assertIn("was replaced while the seat was written", err)
        self.assertEqual(line, "written ok", "the successor's own write succeeded")
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|successor-bearer-UUUU9999\n",
                         "the successor's credential survives the older writer's rollback")
        self.assertEqual(early, [], "the successor waited for the older writer's seat lock")

    def test_the_rollback_leaves_a_file_that_no_longer_holds_what_this_write_put_there(self):
        self.seated_by_another_adapter()
        real, late, newer = cm.root_is_held, [], f"{RELAY}|newer-bearer-VVVV0000\n"

        def checked(owner, root):
            if hasattr(checked, "pre") and not late:
                late.append(self.replace_root())
                (Path(str(self.seat().parent.parent) + ".parked") / "channels" / "ag2space" / "token").write_text(newer)
            checked.pre = True
            return real(owner, root)
        try:
            with mock.patch.object(cm, "root_is_held", side_effect=checked):
                rc, out, err = self.connect(Backend())
        finally:
            for p in late:
                unhold(p)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        parked = Path(str(self.seat().parent.parent) + ".parked") / "channels" / "ag2space"
        self.assertEqual((parked / "token").read_text(), newer, "bytes this write did not put there are left")
        self.assertNotIn(BEARER, (parked / ".env").read_text() + (parked / "token").read_text())

    def test_a_fifo_seat_file_is_refused_at_once_and_left_as_it_is(self):
        self.seated_by_another_adapter()
        token = self.seat() / "token"
        token.unlink()
        os.mkfifo(token)
        got = []
        t = threading.Thread(target=lambda: got.append(self.connect(Backend())), daemon=True)
        t.start()
        t.join(10)
        if t.is_alive():
            os.close(os.open(token, os.O_WRONLY))     # end the read connect is stuck in
            t.join(30)
            self.fail("connect hung reading the FIFO seat token")
        rc, out, err = got[0]
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertNotIn("connected:", out)
        self.assertIn("token is not a regular file", err)
        self.assertTrue(stat.S_ISFIFO(os.lstat(token).st_mode), "the FIFO is left as it is")

    def held_root(self):
        """The agent's seated root, held by this process as connect holds it."""
        self.seated_by_another_adapter()
        owner = task_io.acquire_lock(str(self.root), cm.CONNECT_HOLDER)
        self.addCleanup(task_io.release_lock, owner)
        return owner

    def test_write_seat_held_refuses_a_fifo_token_within_a_bound_and_no_writer(self):
        owner, token = self.held_root(), self.seat() / "token"
        token.unlink()
        os.mkfifo(token)
        got = []
        t = threading.Thread(target=lambda: got.append(cm.write_seat_held(
            owner, str(self.root), cb.bridge_home(self.root), {"relay_url": RELAY, "bearer": BEARER, "agent_id": AGENT})),
            daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "write_seat_held returned within 5 s, with no writer on the FIFO")
        self.assertEqual(got[0][0], "")
        self.assertIn("(token is not a regular file); the seat is unchanged", got[0][1])
        self.assertTrue(stat.S_ISFIFO(os.lstat(token).st_mode), "the FIFO is left as it is")

    def test_write_seat_held_reads_a_seat_file_up_to_the_bound_and_refuses_one_over_it(self):
        owner, bound = self.held_root(), 64 * 1024
        for size, ok in ((bound - 1, True), (bound, True), (bound + 1, False)):
            with self.subTest(size):
                (self.seat() / "token").write_bytes(b"x" * size)
                seat_env, why = cm.write_seat_held(owner, str(self.root), cb.bridge_home(self.root),
                                                   {"relay_url": RELAY, "bearer": BEARER, "agent_id": AGENT})
                token = (self.seat() / "token").read_bytes()
                if ok:
                    self.assertEqual((why, token), ("", f"{RELAY}|{BEARER}\n".encode()))
                    self.assertEqual(seat_env, str(self.seat() / ".env"))
                else:
                    self.assertEqual(seat_env, "")
                    self.assertIn(f"token is larger than {bound} bytes", why)
                    self.assertEqual(token, b"x" * size, "the oversized file is left as it is")

    def running_job(self):
        """The agent connected once: its managed launcher live on the root."""
        fake = self.fake()
        self.assertEqual(self.connect(Backend(), fake)[0], 0)
        self.assertIsNotNone(fake.runner.pid())
        return fake

    def assert_job_runs(self, fake, pid=None):
        rec = task_io.lock_holder(str(self.root))
        self.assertTrue(rec.get("live") and rec.get("adapter") == L.name, rec)
        self.assertEqual(rec["pid"], fake.runner.pid(), "the managed launcher holds the root")
        if pid is not None:
            self.assertEqual(fake.runner.pid(), pid, "the same launcher, never stopped")

    def seat_lock_holder(self):
        p = subprocess.Popen([sys.executable, "-c", SEAT_HOLDER, str(cb.bridge_home(self.root) / ".seat.lock")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_a_seat_that_cannot_be_written_refuses_before_anything_is_written_or_started(self):
        for name in ("fifo token", "oversized token", "seat lock held", "seat lock held, no channel dir"):
            with self.subTest(name):
                self.seated_by_another_adapter()
                token, holder, fake = self.seat() / "token", None, self.fake()
                try:
                    if name == "fifo token":
                        token.unlink()
                        os.mkfifo(token)
                    elif name == "oversized token":
                        token.write_bytes(b"x" * (64 * 1024 + 1))
                    else:
                        if name.endswith("no channel dir"):
                            shutil.rmtree(self.seat().parent)
                        holder = self.seat_lock_holder()
                    with mock.patch.object(cm, "SEAT_WAIT_S", 0.3):
                        rc, out, err = self.connect(Backend(), fake)
                    self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
                    self.assertNotIn("connected:", out)
                    self.assertIn("the seat is unchanged", err)
                    self.assertIsNone(fake.runner.pid(), "nothing was started")
                finally:
                    if holder is not None:
                        unhold(holder)
                    shutil.rmtree(cb.bridge_home(self.root), ignore_errors=True)

    def test_a_credential_that_would_render_past_the_read_bound_is_refused_unwritten(self):
        owner = self.held_root()
        before = (self.seat() / "token").read_bytes()
        seat_env, why = cm.write_seat_held(owner, str(self.root), cb.bridge_home(self.root),
                                           {"relay_url": RELAY, "bearer": "b" * (64 * 1024), "agent_id": AGENT})
        self.assertEqual(seat_env, "")
        self.assertIn("the seat's token would be larger than 65536 bytes", why)
        self.assertEqual((self.seat() / "token").read_bytes(), before)

    def test_a_stable_root_through_the_same_seam_connects(self):
        self.seated_by_another_adapter()
        seen, real = [], cm.acquire_lock
        with mock.patch.object(cm, "acquire_lock", side_effect=lambda root, adapter: seen.append(root) or real(root, adapter)):
            rc, out, err = self.connect(Backend())
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [str(self.root)])
        self.assertIn("connected: ", out)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|{BEARER}\n")

    def test_a_holder_that_takes_the_root_after_the_clean_check_keeps_the_seat(self):
        self.root.mkdir(parents=True)
        late, real = [], cm._free_root_problem

        def checked_then_taken(root):
            got = real(root)
            if not late:
                late.append(hold(self.root, "claude-local"))
            return got
        try:
            with mock.patch.object(cm, "_free_root_problem", side_effect=checked_then_taken):
                self.assert_refused_untouched(None, f"{self.root} was taken by another process first")
        finally:
            for p in late:
                unhold(p)
        self.assertEqual(len(late), 1)

    def test_a_failed_install_after_the_seat_says_the_seat_recovers_the_spent_code(self):
        a = self.home / "agent-a"
        fake = BridgeFake(Runner(None, 0, str(a)))
        self.runners.append(fake.runner)
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("install", "--work", str(a), "--codex-home", str(self.codex_home))[0], 0)
        fake.runner.stop_session()          # A's job stopped, its config still for A
        rc, out, err = self.connect(Backend(), fake, "--sandbox", "workspace-write", "--model", "gpt-test")
        self.assertEqual(rc, 1, out + err)
        rerun = (f"{cli.COMMAND} install --codex-home {self.codex_home} --sandbox workspace-write --model gpt-test "
                 f"--token-env-file {cb.seat_file(self.root)}")
        self.assertIn(f"the work root is {a}, not {self.root}", err)
        self.assertIn(f"the code is spent and its credential is in the seat {cb.seat_file(self.root)}; once the line "
                      f"above is fixed, run {rerun} (no new code is needed)", err)
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        fake = self.fake()
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, _out, err = self.main(*shlex.split(rerun)[1:])
        self.assertEqual(rc, 0, err)
        cfg = cm.read_config(cli.config_path())
        self.assertEqual((cfg["ROOT"], cfg["CODEX_HOME"], cfg["SANDBOX"], cfg["MODEL"]),
                         (str(self.root), str(self.codex_home), "workspace-write", "gpt-test"))

    def test_a_bridge_that_does_not_wire_names_the_install_with_the_seat(self):
        with mock.patch.object(cm, "console_script",
                               side_effect=lambda shape: "" if shape is B else str(self.bin / shape.command)):
            rc, _out, err = self.connect(Backend())
        self.assertEqual(rc, cli.EXIT_UNUSABLE, err)
        self.assertIn("the connector is installed but its bridge is not: ag2-sparrow was not found beside", err)
        self.assertIn(f"; run {cli.COMMAND} install --codex-home {self.codex_home} --token-env-file "
                      f"{cb.seat_file(self.root)}\n", err)

    def test_a_bridge_wiring_failure_after_install_unwinds_this_attempt_and_restarts_nothing(self):
        fake = self.running_job()
        fake.runner.stop_session()
        before = len(fake.runner.calls)
        with mock.patch.object(cm, "console_script",
                               side_effect=lambda shape: "" if shape is B else str(self.bin / shape.command)):
            rc, out, err = self.connect(Backend(), fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn("its bridge is not: ag2-sparrow was not found beside", err)
        starts = [c for c in fake.runner.calls[before:] if fake.runner.verb(c) in START_VERBS]
        self.assertEqual(len(starts), 1, "install started the job once; nothing started it again")
        self.assertNotIn("next:", out)
        self.assertIn(f"; this attempt was undone: {cm.job_name(L)}, the job its install started, was stopped", err)
        self.assertIsNone(fake.runner.pid(), "the job this attempt started is not left running")

    def test_a_job_started_after_the_seat_write_is_left_running_and_connect_refuses(self):
        fake = self.running_job()
        fake.runner.stop_session()
        rotated = Backend(answer={"relay_url": RELAY, "bearer": "rotated-bearer-OOOO5555", "agent_id": AGENT})
        real, raced = cm.install, []

        def started_then_installed(*a, **kw):
            fake.runner.start_session()          # the manager, sparrowd or the owner starts it in the hand-off
            raced.append((fake.runner.pid(), len(fake.runner.calls)))
            return real(*a, **kw)
        with mock.patch.object(cm, "install", side_effect=started_then_installed):
            rc, out, err = self.connect(rotated, fake)
        pid, mark = raced[0]
        self.assertNotEqual(rc, 0, out + err)
        self.assertEqual(fake.runner.pid(), pid, "the raced launcher is the one still running")
        self.assert_job_runs(fake, pid)
        verbs = [fake.runner.verb(c) for c in fake.runner.calls[mark:]]
        self.assertFalse(set(verbs) & {"stop", "disable", "restart", "bootout", "kickstart", "start", "bootstrap"},
                         verbs)
        self.assertNotIn("stopped:", out)
        self.assertIn("connect stops nothing, so a running job keeps the seat it has read", err)
        self.assertEqual(len(rotated.calls), 1)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|rotated-bearer-OOOO5555\n")
        self.assertIn("the code is spent and its credential is in the seat", err)

    def test_a_job_started_between_connects_check_and_its_start_is_kept_not_restarted(self):
        fake = self.running_job()
        fake.runner.stop_session()
        real, raced = cm.start_supervisor, []

        def raced_then_started(shape, **kw):
            if shape is L and not raced:
                fake.runner.start_session()
                raced.append(fake.runner.pid())
            return real(shape, **kw)
        before = len(fake.runner.calls)
        with mock.patch.object(cm, "start_supervisor", side_effect=raced_then_started):
            rc, out, err = self.connect(Backend(), fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(fake.runner.pid(), raced[0], "the job that raced in is the one running, never restarted")
        self.assertNotIn("stopped:", out)
        self.assertFalse({fake.runner.verb(c) for c in fake.runner.calls[before:]} & {"stop", "restart", "bootout"})

    def test_a_holder_that_is_not_the_managers_job_is_not_taken_for_it(self):
        fake, real, stray = self.fake(), cm.start_supervisor, []

        def stray_first(shape, **kw):
            if shape is L and not stray:
                stray.append(hold(self.root, L.name))   # a launcher no manager started, outside the lock protocol
            return real(shape, **kw)
        with mock.patch.object(cm, "start_supervisor", side_effect=stray_first):
            rc, out, err = self.connect(Backend(), fake)
        try:
            self.assertNotEqual(rc, 0, out + err)
            self.assertIn(f"(pid {stray[0].pid}) is not the process {cm.job_name(L)} runs as", err)
            self.assertIsNone(stray[0].poll(), "the stray is left as it is")
            self.assertNotIn("next:", out)
        finally:
            unhold(stray[0])

    def replace_root_now(self):
        os.rename(self.root, str(self.root) + ".parked")
        os.mkdir(self.root)

    def test_a_root_replaced_after_install_and_before_the_bridge_is_wired_unwinds_this_attempt(self):
        fake, real = self.fake(), cm.install

        def installed_then_replaced(*a, **kw):
            rc = real(*a, **kw)
            self.assertIsNotNone(fake.runner.pid(), "install started the job")
            self.replace_root_now()
            return rc
        with mock.patch.object(cm, "install", side_effect=installed_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertNotIn("next:", out)
        self.assertIn(f"{self.root} was replaced after install started {cm.job_name(L)}; {cm.job_name(L)}, which "
                      f"held the directory that was there, was stopped; nothing at {self.root} was touched", err)
        self.assertIsNone(fake.runner.pid(), "the job this attempt started is not left on the parked root")
        self.assertFalse(cm.service_path(B).exists(), "the bridge was not wired")
        self.assertEqual(task_io.lock_held(str(self.root)), "free", "the replacement is untouched")
        self.assertEqual(os.listdir(self.root), [], "nothing was written into the replacement")

    def test_a_root_replaced_while_the_bridge_is_wired_unwinds_this_attempt(self):
        fake, real = self.fake(), cb.wire_bridge

        def wired_then_replaced(*a, **kw):
            got = real(*a, **kw)
            self.assertTrue(cm.service_path(B).exists())
            self.replace_root_now()
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertNotIn("next:", out)
        self.assertIn(f"the bridge this attempt wired ({cm.job_name(B)}) was removed; {cm.job_name(L)}, which held "
                      f"the directory that was there, was stopped; nothing at {self.root} was touched", err)
        self.assertFalse(cm.service_path(B).exists(), "this attempt's bridge unit is gone")
        self.assertIsNone(fake.runner.pid(), "this attempt's job is stopped")
        self.assertEqual(task_io.lock_held(str(self.root)), "free", "the replacement is untouched")
        self.assertEqual(os.listdir(self.root), [], "nothing was written into the replacement")

    def test_connect_changes_no_jobs_definition_and_refuses_when_it_would(self):
        fake = self.running_job()
        fake.runner.stop_session()
        conf = cli.config_path()
        conf.write_text(conf.read_text().replace("ROOT=", "# edited by hand\nROOT=", 1))
        before = conf.read_bytes()
        rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{conf} differs from what connect would write; connect changes no job's definition", err)
        self.assertEqual(conf.read_bytes(), before)
        self.assertIsNone(fake.runner.pid(), "nothing was started")

    def agent_lock_holder(self):
        """The production agent lock held by another process."""
        p = subprocess.Popen([sys.executable, "-c", AGENT_LOCK_HOLDER % (str(PKG), str(self.root))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.addCleanup(lambda: unhold(p) if p.poll() is None else None)
        return p

    def test_both_adapters_resolve_one_lock_for_one_agent(self):
        from ag2_sparrow.adapters.claude_code import cli as other
        self.assertIs(other.cm.agent_lock_path, cm.agent_lock_path, "one production lock owner")
        self.assertIs(other.cb.agent_root, cb.agent_root)
        root = cb.agent_root(AGENT)[0]
        self.assertEqual(root, str(self.root))
        lock = cm.agent_lock_path(root)
        self.assertEqual(lock, _dirs.agents_dir() / ".locks" / f"{self.root.name}.op")
        self.assertFalse(str(lock).startswith(str(self.root) + os.sep), "outside the root")
        self.root.mkdir(parents=True)
        os.rename(self.root, str(self.root) + ".parked")
        os.mkdir(self.root)
        self.assertEqual(cm.agent_lock_path(str(self.root)), lock, "a replacement at the path takes the same lock")

    def test_install_uninstall_and_wiring_wait_for_the_agent_lock_and_refuse(self):
        fake = self.running_job()
        fake.runner.stop_session()
        cfg = cm.read_config(cli.config_path())
        holder = self.agent_lock_holder()
        with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.3), mock.patch.object(sm, "_run", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            before = len(fake.runner.calls)
            self.assertEqual(cm.install(L, {}, str(self.root)), 1)
            self.assertEqual(cm.uninstall(L), 1)
            wired = cb.wire_bridge_bound(cfg, cli.COMMAND, B, L, None)
        self.assertEqual(err.getvalue().count("another operation on this agent is in progress"), 2, err.getvalue())
        self.assertIn("another operation on this agent is in progress", wired)
        self.assertEqual(fake.runner.calls[before:], [], "nothing reached the manager")
        self.assertTrue(cli.config_path().exists(), "uninstall removed nothing")
        holder.kill()
        holder.wait(timeout=30)
        with cm.agent_lock(str(self.root), wait_s=1):
            pass                                    # a dead holder's flock is gone: nothing stale remains

    def test_a_launcher_does_not_act_on_the_root_until_the_operation_ends(self):
        self.root.mkdir(parents=True)
        holder = self.agent_lock_holder()
        rc, err, _seen = self.launch_with(L)
        self.assertEqual(rc, cm.EXIT_RETRY, err)
        self.assertIn("the launcher does not act on the root, and the manager starts it again", err)
        unit = cm.render_supervisor(L)[0]
        self.assertIn(f"RestartPreventExitStatus={cm.EXIT_HELD}\n", unit, "only 75 stops systemd's restarts")
        self.assertNotIn(str(cm.EXIT_RETRY), unit.split("RestartPreventExitStatus=")[1].splitlines()[0])
        self.assertNotEqual(cm.launchd_exit(cm.EXIT_RETRY), 0, "launchd restarts a non-zero exit")
        unhold(holder)
        rc, err, _seen = self.launch_with(L)
        self.assertEqual(rc, cm.EXIT_UNUSABLE, err)
        self.assertIn("stop here", err, "once the operation ended the launcher took the root")

    def test_the_other_adapter_and_a_launcher_wait_while_connect_holds_the_agent(self):
        real, seen = cm.install, []

        def contended(*a, **kw):
            r = subprocess.run([sys.executable, "-c", CONTENDER % (str(PKG), "claude_code", "cli", str(self.root),
                                                                    str(self.root))],
                               capture_output=True, text=True, timeout=60)
            seen.append(r.stdout + r.stderr)
            return real(*a, **kw)
        with mock.patch.object(cm, "install", side_effect=contended):
            rc, out, err = self.connect(Backend())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("install 1", seen[0])
        self.assertIn("another operation on this agent is in progress", seen[0])
        self.assertIn("launch an operation on this agent held", seen[0])

    def test_unwinding_leaves_a_unit_that_is_no_longer_this_attempts(self):
        fake, real = self.fake(), cb.wire_bridge
        foreign = "[Service]\nExecStart=/a-successor\n# " + L.marker + "\n"

        def wired_then_replaced(*a, **kw):
            got = real(*a, **kw)
            self.replace_root_now()
            cm.supervisor_path(L).write_text(foreign)     # a successor's definition, published meanwhile
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertNotIn("next:", out)
        self.assertIn(f"{cm.job_name(L)} is no longer the job this connect started (its unit or launcher changed), so "
                      f"it was left as it is: stop it by hand", err)
        self.assertEqual(cm.supervisor_path(L).read_text(), foreign, "the successor's unit is untouched")
        self.assertIsNotNone(fake.runner.pid(), "its job was not stopped")

    def test_unwinding_stops_this_attempts_job_and_leaves_a_held_replacement_alone(self):
        fake, real, other = self.fake(), cb.wire_bridge, []

        def wired_then_replaced(*a, **kw):
            got = real(*a, **kw)
            self.replace_root_now()
            other.append(hold(self.root, "claude-local"))
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        try:
            self.assertNotEqual(rc, 0, out + err)
            self.assertIsNone(fake.runner.pid(), "this attempt's job was stopped")
            self.assertIsNone(other[0].poll(), "the replacement's holder is alive")
            rec = task_io.lock_holder(str(self.root))
            self.assertEqual((rec["adapter"], rec["pid"]), ("claude-local", other[0].pid), "and still holds it")
        finally:
            unhold(other[0])

    def test_a_definition_published_by_another_writer_first_is_not_clobbered(self):
        fake, real, theirs = self.fake(), cm.tempfile.mkstemp, "[Service]\nExecStart=/theirs\n"
        sup = cm.supervisor_path(L)

        def raced(*a, **kw):
            if kw.get("prefix") == f".{sup.name}." and not sup.exists():
                sup.parent.mkdir(parents=True, exist_ok=True)
                sup.write_text(theirs)                   # the other writer publishes between check and write
            return real(*a, **kw)
        with mock.patch.object(cm.tempfile, "mkstemp", side_effect=raced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{sup} appeared with other bytes than connect's", err)
        self.assertEqual(sup.read_text(), theirs, "the other writer's bytes are kept")
        self.assertIsNone(fake.runner.pid(), "nothing was started")

    def test_a_loaded_launchd_job_counts_as_started_only_for_connects_start(self):
        loaded = (5, "", "Bootstrap failed: 5: Input/output error\n")
        table = {("launchctl", "bootstrap"): loaded, ("launchctl", "print"): (0, "\tstate = running\n", "")}
        run = lambda cmd, timeout=30: subprocess.CompletedProcess(cmd, *table[(cmd[0], cmd[1])])  # noqa: E731
        with mock.patch.object(cm, "is_macos", return_value=True), mock.patch.object(sm, "_run", side_effect=run):
            self.assertEqual(cm.start_supervisor(L, restart=False), "")
            self.assertIn("launchctl bootstrap failed", cm.start_supervisor(L))

    def _main_with(self, fake, *argv):
        with mock.patch.object(sm, "_run", side_effect=fake):
            return self.main(*argv)

    def launch_with(self, shape, gap=None):
        """The production launcher sequence on the root, `gap()` run just before it takes the root."""
        real, seen = task_io.acquire_lock, []

        def acquire(*a, **kw):
            if gap is not None:
                seen.append(gap())
            return real(*a, **kw)
        err = io.StringIO()
        with mock.patch.object(cm, "LAUNCH_WAIT_S", 0.3), mock.patch.object(task_io, "acquire_lock", side_effect=acquire), \
                contextlib.redirect_stderr(err):
            rc = cm.hold_and_supervise(shape, str(self.root), {"sig": None}, lambda owner, root: (None, "stop here"),
                                       None, None, None, "run", "test")
        return rc, err.getvalue(), seen

    def other_thread_takes_the_agent_lock(self):
        got = []

        def take():
            try:
                with cm.agent_lock(str(self.root), wait_s=0.3):
                    got.append("taken")
            except cm.Busy:
                got.append("busy")
        t = threading.Thread(target=take)
        t.start()
        t.join(10)
        return got[0]

    def test_launcher_keeps_the_agent_lock_from_admission_until_it_holds_the_root(self):
        self.root.mkdir(parents=True)
        rc, err, seen = self.launch_with(L, gap=self.other_thread_takes_the_agent_lock)
        self.assertEqual(rc, cm.EXIT_UNUSABLE, err)
        self.assertIn("stop here", err, "the launcher took the root")
        self.assertEqual(seen, ["busy"], "no actor gets the agent lock between admission and the root")

    def test_install_holds_one_agent_lock_across_its_setup_and_its_bridge(self):
        fake = self.fake()
        seed_seat(cb.bridge_home(self.root), BEARER)
        real, gap = cm.install, []

        def installed_then_uninstall_tries(*a, **kw):
            rc = real(*a, **kw)
            got = []
            t = threading.Thread(target=lambda: got.append(cm.uninstall(L)))
            with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.3), contextlib.redirect_stderr(io.StringIO()) as err:
                t.start()
                t.join(30)
            gap.append((got[0], err.getvalue()))
            return rc
        with mock.patch.object(cm, "install", side_effect=installed_then_uninstall_tries):
            rc, out, err = (lambda fake: self._main_with(fake, "install", "--token-env-file", cb.seat_file(self.root), "--codex-home", str(self.codex_home)))(fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(gap[0][0], 1, "uninstall could not step in between install's setup and its bridge")
        self.assertIn("another operation on this agent is in progress", gap[0][1])
        self.assertTrue(cli.config_path().exists() and cm.service_path(B).exists(), "installed and wired whole")

    def test_uninstall_rereads_the_root_under_its_lock_and_never_acts_on_an_unlocked_one(self):
        a, b, c = self.home / "agents" / "agent-a", self.home / "agents" / "agent-b", self.home / "agents" / "agent-c"
        for swaps, acted in (([b], True), ([b, c], False)):
            with self.subTest(swaps=len(swaps)):
                self.configure(ROOT=str(a))
                real_lock, real_un, todo, ran = cm.agent_lock, cm._uninstall_locked, list(swaps), []

                def lock(root, *args, **kw):
                    got = real_lock(root, *args, **kw)
                    if todo:
                        self.configure(ROOT=str(todo.pop(0)))   # another install moves the root meanwhile
                    return got

                def uninstall_locked(*args, **kw):
                    root = cm.read_config(cli.config_path())["ROOT"]
                    ran.append((root, any(str(cm.agent_lock_path(root)) in str(k) for k in cm._AGENT_HELD)))
                    return 0
                err = io.StringIO()
                with mock.patch.object(cm, "agent_lock", side_effect=lock), \
                        mock.patch.object(cm, "_uninstall_locked", side_effect=uninstall_locked), \
                        contextlib.redirect_stderr(err):
                    rc = cm.uninstall(L)
                if acted:
                    self.assertEqual((rc, ran), (0, [(str(b), True)]), "acted on B with B's lock held")
                else:
                    self.assertEqual((rc, ran), (1, []), "fail closed: nothing acted on")
                    self.assertIn("configured root changed while uninstall waited for its lock", err.getvalue())

    def test_uninstall_with_no_config_waits_out_any_agent_operation(self):
        self.root.mkdir(parents=True)
        holder = self.agent_lock_holder()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cm.uninstall(L), 1)
        self.assertIn("no config names the root", err.getvalue())
        unhold(holder)
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.object(sm, "_run", side_effect=self.fake()):
            self.assertEqual(cm.uninstall(L), 0)
        self.assertIn("nothing to remove", out.getvalue())

    def test_an_identity_mismatch_at_start_leaves_a_unit_that_is_no_longer_this_attempts(self):
        fake, real = self.fake(), cm._fresh_holder
        foreign = "[Service]\nExecStart=/a-successor\n"

        def held_then_moved(*a, **kw):
            rec = real(*a, **kw)
            if rec is not None and not os.path.exists(str(self.root) + ".parked"):
                self.replace_root_now()
                cm.supervisor_path(L).write_text(foreign)
            return rec
        with mock.patch.object(cm, "_fresh_holder", side_effect=held_then_moved):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("is no longer the job this connect started (its unit or launcher changed)", err)
        self.assertIsNotNone(fake.runner.pid(), "not stopped")
        self.assertEqual(cm.supervisor_path(L).read_text(), foreign)

    def test_the_real_launcher_started_by_install_and_by_connect_ends_up_alive(self):
        for how in ("install", "connect"):
            with self.subTest(how):
                fake = self.fake()
                fake.runner.real = True                     # hold_and_supervise, through launch_admission
                if how == "install":
                    seed_seat(cb.bridge_home(self.root), BEARER)
                    rc, out, err = (lambda fake: self._main_with(fake, "install", "--token-env-file", cb.seat_file(self.root), "--codex-home", str(self.codex_home)))(fake)
                else:
                    rc, out, err = self.connect(Backend(), fake)
                self.assertEqual(rc, 0, out + err)
                self.assertIsNotNone(fake.runner.session)
                self.assertIsNone(fake.runner.session.poll(), "the launcher is alive")
                rec = task_io.lock_holder(str(self.root))
                self.assertEqual((rec["owner_pid"], rec["live"]), (fake.runner.pid(), True), "and holds the root")
                fake.runner.stop_session()
                with mock.patch.object(sm, "_run", side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cm.uninstall(L), 0)

    def test_a_launcher_held_past_the_operations_wait_never_acts_on_the_root(self):
        for shim in (False, True):
            with self.subTest("launchd shim child" if shim else "systemd direct"):
                fake = self.fake()
                r = fake.runner
                r.real, r.shim, r.stop_timeout = True, shim, 2       # the stop times out on the paused launcher
                r.pause, r.acted = str(self.home / f"resume-{shim}"), str(self.home / f"acted-{shim}")
                with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.5), mock.patch.object(cm, "is_macos", return_value=shim):
                    rc, out, err = self.connect(Backend(), fake)
                Path(r.pause).touch()                       # the paused launcher resumes after the failure
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not Path(r.acted).exists() \
                        and any(launcher.poll() is None for launcher in r.spawned):
                    time.sleep(0.1)
                acted, held = Path(r.acted).exists(), task_io.lock_held(str(self.root))
                for launcher in r.spawned:                  # cleanup, so one shape cannot leak into the next
                    if launcher.poll() is None:
                        os.killpg(launcher.pid, signal.SIGKILL)
                        launcher.wait(timeout=30)
                self.assertFalse(acted, "the late launcher never held or acted on the root")
                self.assertEqual(held, "free")
                self.assertNotEqual(rc, 0, out + err)
                self.assertNotIn("next:", out)
                self.assertIn("did not acquire", err)

    def test_the_real_launcher_through_launchds_shim_ends_up_alive(self):
        fake = self.fake()
        fake.runner.real, fake.runner.shim = True, True
        with mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self.connect(Backend(), fake)
            self.assertEqual(rc, 0, out + err)
            rec = task_io.lock_holder(str(self.root))
            self.assertTrue(rec["live"])
            self.assertEqual(cm._parent_pid(rec["owner_pid"]), fake.runner.pid(), "the shim's child holds the root")
            self.assertEqual({p.suffix for p in cm.agent_lock_path(str(self.root)).parent.iterdir()}, {".op"},
                             "no start files: the operation hands the launcher nothing but the lock")
            fake.runner.stop_session()

    def test_a_stop_reaches_a_launcher_waiting_in_admission_at_once(self):
        for shim in (False, True):
            with self.subTest("launchd shim child" if shim else "systemd direct"):
                fake = self.fake()
                fake.runner.real, fake.runner.shim = True, shim
                fake.runner.acted = str(self.home / f"acted-{shim}")
                ready = Path(fake.runner.acted + ".ready")

                def refused_once_waiting(*a, **kw):
                    deadline = time.monotonic() + 30
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)                # the launcher is in admission, waiting for this lock
                    started.append(time.monotonic())
                    return "the bridge was refused"
                started = []
                with mock.patch.object(cm, "is_macos", return_value=shim), \
                        mock.patch.object(cb, "wire_bridge", side_effect=refused_once_waiting):
                    rc, out, err = self.connect(Backend(), fake)
                took = time.monotonic() - started[0]
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn("this attempt was undone", err)
                self.assertLess(took, 8, "the launcher left admission on the stop, not at its 20 s wait or the kill")
                self.assertTrue(all(launcher.poll() is not None for launcher in fake.runner.spawned))
                self.assertFalse(Path(fake.runner.acted).exists(), "the stopped launcher never touched the root")

    def test_a_busy_lock_held_by_another_operation_leaves_the_job_to_it(self):
        fake, real, other = self.fake(), cm._fresh_holder, []

        def other_operation_first(*a, **kw):
            if not other:
                other.append(self.agent_lock_holder())  # another operation takes the agent lock and keeps it
            return real(*a, **kw)
        fake.runner.root = None                         # the manager accepts the start; no launcher comes
        before = len(fake.runner.calls)
        with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.5), \
                mock.patch.object(cm, "_fresh_holder", side_effect=other_operation_first):
            rc, out, err = self.connect(Backend(), fake)
        verbs = [fake.runner.verb(c) for c in fake.runner.calls[before:]]
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"another operation holds the agent lock (operation pid {other[0].pid})", err)
        self.assertFalse({"stop", "bootout", "disable"} & set(verbs), "nothing was stopped under another operation")

    def test_unwinding_leaves_a_job_the_manager_restarted_meanwhile(self):
        fake, real, restarted = self.fake(), cb.wire_bridge, []

        def wired_then_restarted(*a, **kw):
            got = real(*a, **kw)
            self.replace_root_now()
            fake.runner.start_session()                  # someone restarted the unit: same bytes, another job
            restarted.append(fake.runner.pid())
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_restarted):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("is no longer the job this connect started (its unit or launcher changed)", err)
        self.assertEqual(fake.runner.pid(), restarted[0], "the restarted job was not stopped")

    def test_an_agent_lock_directory_others_can_use_is_refused_cleanly(self):
        self.root.mkdir(parents=True)
        locks = cm.agent_lock_path(str(self.root)).parent
        locks.mkdir(parents=True, exist_ok=True)
        locks.chmod(0o777)
        self.addCleanup(locks.chmod, 0o700)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cm.install(L, {}, str(self.root)), 1)
        self.assertIn("cannot be used", err.getvalue())
        self.assertIn("is not a directory only this user can use", err.getvalue())
        rc, err, _seen = self.launch_with(L)
        self.assertEqual(rc, cm.EXIT_RETRY, err)
        self.assertIn("cannot be used", err)
        self.assertNotIn("Traceback", err)

    def test_a_start_another_actor_won_is_never_stopped_by_this_attempts_undo(self):
        for shim in (False, True):
            with self.subTest("launchd shim child" if shim else "systemd direct"):
                fake, real, external = self.fake(), cm._start_pending, []
                fake.runner.real, fake.runner.shim = True, shim

                def external_first(shape, *a, **kw):
                    if shape is L and not external:
                        fake.runner.start_session(by_manager=True)   # someone else starts the job after the check
                        external.append(fake.runner.session)
                    return real(shape, *a, **kw)
                with mock.patch.object(cm, "is_macos", return_value=shim), \
                        mock.patch.object(cm, "_start_pending", side_effect=external_first), \
                        mock.patch.object(cb, "wire_bridge", return_value="the bridge was refused"):
                    rc, out, err = self.connect(Backend(), fake)
                try:
                    self.assertNotEqual(rc, 0, out + err)
                    self.assertIn("was already running when this operation asked for it, so it is not this operation's",
                                  err)
                    self.assertIsNone(external[0].poll(), "the external generation is alive")
                    self.assertIs(fake.runner.session, external[0], "and still the manager's job")
                finally:
                    fake.runner.stop_session()

    def test_an_exception_after_the_start_is_undone_under_the_lock(self):
        for how in ("connect", "install"):
            for shim in (False, True):
                with self.subTest(how=how, shape="launchd shim child" if shim else "systemd direct"):
                    fake = self.fake()
                    fake.runner.real, fake.runner.shim = True, shim
                    fake.runner.acted = str(self.home / f"acted-{how}-{shim}")
                    seed_seat(cb.bridge_home(self.root), BEARER)
                    with mock.patch.object(cm, "is_macos", return_value=shim), \
                            mock.patch.object(cb, "wire_bridge", side_effect=OSError("bridge write failed")), \
                            self.assertRaises(OSError):
                        if how == "connect":
                            self.connect(Backend(), fake)
                        else:
                            self._main_with(fake, "install", "--token-env-file", cb.seat_file(self.root), "--codex-home", str(self.codex_home))
                    for launcher in fake.runner.spawned:
                        launcher.wait(timeout=30)
                    self.assertEqual(cm._PENDING.get(threading.get_ident(), []), [], "nothing left pending")
                    self.assertFalse(cm.agent_lock_held(str(self.root)))
                    self.assertIsNone(fake.runner.pid(), "the job this attempt started was stopped")
                    self.assertFalse(Path(fake.runner.acted).exists(), "and never touched the root")

    def test_a_running_job_refuses_connect_before_the_code_is_spent_and_is_left_running(self):
        fake = self.running_job()
        pid, before, backend = fake.runner.pid(), (self.seat() / "token").read_bytes(), Backend()
        rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn(f"{cm.job_name(L)} is running for {self.root}; stop it first (systemctl --user stop "
                      f"{L.service_name}), then connect again; the code was not used", err)
        self.assertEqual(backend.calls, [], "the code is not spent")
        self.assertNotIn("stopped:", out)
        self.assertEqual((self.seat() / "token").read_bytes(), before)
        self.assert_job_runs(fake, pid)

    def test_an_unreadable_job_state_refuses_connect_before_the_code_is_spent(self):
        backend = Backend()
        with mock.patch.object(cm, "manager_state", return_value="unknown"):
            rc, out, err = self.connect(backend)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn(f"state cannot be read; connect writes the seat only while it is stopped: stop it "
                      f"(systemctl --user stop {L.service_name}), then connect again; the code was not used", err)
        self.assertEqual(backend.calls, [])

    def test_a_stopped_job_connects_and_install_starts_it_with_the_new_seat(self):
        fake = self.running_job()
        fake.runner.stop_session()
        rotated = Backend(answer={"relay_url": RELAY, "bearer": "rotated-bearer-OOOO5555", "agent_id": AGENT})
        rc, out, err = self.connect(rotated, fake)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"connected: {AGENT}", out)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|rotated-bearer-OOOO5555\n")
        self.assert_job_runs(fake)

    def test_a_root_replaced_before_install_gets_nothing_installed_or_started(self):
        fake, real = self.fake(), cm.install

        def replaced_then_installed(*a, **kw):
            os.rename(self.root, str(self.root) + ".parked")
            os.mkdir(self.root)
            return real(*a, **kw)
        with mock.patch.object(cm, "install", side_effect=replaced_then_installed):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{self.root} is no longer the directory connect wrote the seat for (it was replaced); nothing "
                      "was applied", err)
        self.assertIsNone(fake.runner.pid(), "nothing was started on the replacement")
        self.assertEqual(task_io.lock_held(str(self.root)), "free")

    def test_a_root_replaced_while_install_starts_the_job_gets_that_job_stopped(self):
        fake, real = self.fake(), cm.start_supervisor

        def replaced_then_started(shape, **kw):
            if shape is L:
                os.rename(self.root, str(self.root) + ".parked")
                os.mkdir(self.root)
            return real(shape, **kw)
        with mock.patch.object(cm, "start_supervisor", side_effect=replaced_then_started):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{self.root} was replaced after install started {cm.job_name(L)}", err)
        self.assertIn(f"{cm.job_name(L)}, which held the directory that was there, was stopped", err)
        self.assertNotIn("next:", out)
        self.assertIsNone(fake.runner.pid(), "no launcher is left to take the replacement")
        self.assertEqual(task_io.lock_held(str(self.root)), "free")

    def test_the_bearer_is_never_in_argv_of_any_command_connect_runs(self):
        fake = self.fake()
        seen = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            seen.append(list(cmd))
            return real_run(cmd, *a, **kw)
        with mock.patch.object(subprocess, "run", side_effect=spy):
            rc, _out, err = self.connect(Backend(), fake)
        self.assertEqual(rc, 0, err)
        self.assertTrue(seen, "the preflight ran codex")
        for cmd in seen + fake.calls + fake.runner.calls:
            self.assertNotIn(BEARER, " ".join(map(str, cmd)))

    def test_verify_and_uninstall_cover_the_wired_bridge(self):
        fake = self.fake()
        self.assertEqual(self.connect(Backend(), fake)[0], 0)
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, out, _err = self.main("verify")
        self.assertEqual(self.checks(out)["bridge unit"], "PASS")
        self.assertEqual(self.checks(out)["relay token"], "PASS")
        self.assertIn("FAIL bridge: no bridge wrote a status under", out, "no live bridge here")
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"removed: {cm.service_path(B)}", out)
        self.assertFalse(cm.service_path(B).exists())
        self.assertIn(f"kept: {self.seat() / '.env'}", out)
        self.assertTrue((self.seat() / "token").exists(), "the credential is kept")


class InstallTokenEnvFile(Base):
    """install --token-env-file: the agent's own root, its bridge wired by connect_bridge."""

    def test_work_and_token_env_file_are_one_or_the_other(self):
        seat = self.home / "seat.env"
        seat.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\nAGENT_MXID={AGENT}\n")
        rc, _out, err = self.main("install", "--work", str(self.work), "--token-env-file", str(seat))
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn("with --work your bridge has its own", err)
        rc, _out, err = self.main("install")
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn("pass --work <dir>", err)
        self.assertFalse(cli.config_path().exists())

    def test_a_seat_naming_another_agent_is_refused_before_anything_is_written(self):
        seat = cb.bridge_home(self.home / "x")
        self.assertEqual(_sources.write_seat_credentials(
            seat, agent_id=AGENT, instance="a" * 32, tier="owner", relay_url=RELAY, bearer=BEARER), "")
        env = Path(_sources.seat_channel_dir(seat)) / ".env"
        with mock.patch.object(cb, "install_identity", return_value="@other:ag2.space"):
            rc, _out, err = self.main("install", "--token-env-file", str(env))
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn(f"names {AGENT}, not @other:ag2.space", err)
        self.assertFalse(cli.config_path().exists())


if __name__ == "__main__":
    unittest.main()
