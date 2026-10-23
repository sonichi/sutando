#!/usr/bin/env python3
"""`ag2-sparrow-codex-local`: the installer, the managed launcher over the connector, verify and
uninstall, driven in-process against a fake `codex` (version, login status, and the stub app-server for
the connector's own child) and a fake manager whose start verbs run a stand-in holder. No real codex,
no systemctl or launchctl, no relay."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import plistlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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
from ag2_sparrow import connect_common as cm, holder_lock, service_manager as sm, task_io  # noqa: E402
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
OP_HOLDER = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm\n"
             "from ag2_sparrow.adapters.codex.connect_cli import LOCAL\n"
             "with cm.operation_lock(LOCAL): print('held', flush=True); sys.stdin.read()")
TASK = ("id: %s\ntimestamp: 2026-10-16T00:00:00Z\nsource: chat\nchannel_id: %s\naccess_tier: owner\n"
        "priority: normal\ntask: say hi\n")
NOT_LOADED = (113, "", 'Could not find service "ai.ag2.sparrow.codex-local" in domain for uid: 501\n')
START_VERBS = ("restart", "bootstrap", "kickstart")
STOP_VERBS = ("stop", "bootout")
L = cli.LOCAL


def hold(root, adapter="codex-local"):
    """A lock holder in another process (holder_lock allows one acquisition per process)."""
    p = subprocess.Popen([sys.executable, "-c", STAND_IN % (str(PKG), str(root), adapter)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


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
        self.session, self.enabled = None, False

    def start_session(self):
        self.stop_session()
        self.session = hold(self.root, self.adapter)

    def stop_session(self):
        if self.session is not None:
            unhold(self.session)
            self.session = None

    def verb(self, cmd):
        return cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))

    def answer(self, verb, cmd):
        if verb == "print":
            return (0, f"\tpid = {self.session.pid}\n\tstate = running\n", "") if self.session else NOT_LOADED
        if verb == "show":
            loaded = cm.service_path(L).exists()
            words = {"LoadState": "loaded" if loaded else "not-found", "ActiveState": "active" if self.session else "inactive",
                     "SubState": "running" if self.session else "dead", "UnitFileState": "enabled" if self.enabled else "disabled"}
            props = cmd[cmd.index("-p") + 1].split(",")
            return (0, "".join(words[k] + "\n" for k in props), "")
        return self.default

    def __call__(self, cmd, timeout=30):
        self.calls.append(list(cmd))
        verb = self.verb(cmd)
        got = self.table[(cmd[0], verb)] if (cmd[0], verb) in self.table else self.answer(verb, cmd)
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
        self.addCleanup(lambda: [r.stop_session() for r in self.runners])

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
        self.assertEqual(runner.verbs("systemctl"), ["show", "show-environment", "daemon-reload", "enable", "restart"],
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
        self.assertEqual(verbs[-3:], ["daemon-reload", "enable", "restart"])
        self.assertIn("changed: ", out)
        self.assertEqual(cm.read_config(cli.config_path())["SANDBOX"], "workspace-write")
        self.assertNotEqual(runner.pid(), old, "a fresh holder, not the old connector")
        runner.table[("systemctl", "restart")] = 1
        rc, _, err = self.install_with(runner, False, "--sandbox", "read-only")
        self.assertEqual(rc, 1)
        self.assertIn("did not acquire", err)
        self.assertEqual(cm.read_config(cli.config_path())["SANDBOX"], "read-only", "the files as written; the retry is the same install")

    def test_a_debug_holder_that_won_the_root_is_never_taken_for_the_managed_launcher(self):
        """The manager's start lost to a hand launcher (same adapter family, same install): install fails."""
        runner = self.runner(adapter="codex-local.debug")
        rc, _, err = self.install_with(runner)
        self.assertEqual(rc, 1)
        self.assertIn("did not acquire", err)
        self.assertIn(f"held by launcher pid {runner.pid()}", err)

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


if __name__ == "__main__":
    unittest.main()
