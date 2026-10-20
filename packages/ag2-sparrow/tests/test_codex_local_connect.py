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

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
from ag2_sparrow import connect_common as cm, holder_lock, task_io  # noqa: E402
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
if sys.argv[1:] == ["--version"]:
    print(read("codex-version-banner", "") or "codex-cli " + read("codex-version", "0.160.1"))
    sys.exit(int(read("codex-version-rc", "0")))
if sys.argv[1:3] == ["login", "status"]:
    with open(os.path.join(home, "codex-login.json"), "w") as f:
        json.dump({"env": dict(os.environ)}, f)
    print("Logged in using ChatGPT" if read("codex-login-rc", "0") == "0" else "Not logged in")
    sys.exit(int(read("codex-login-rc", "0")))
if sys.argv[1:3] == ["app-server", "--listen"]:
    with open(os.path.join(home, "codex-server.json"), "w") as f:
        json.dump({"argv": sys.argv, "env": dict(os.environ), "pid": os.getpid()}, f)
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
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:   # surfaced on the test thread
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError("still running after %.0f s" % seconds)
    if "error" in box:
        raise box["error"]
    return box["value"]
TOKEN = "tok-FAKE-RELAY-TOKEN-never-copied"
FORBIDDEN_ARGS = ("--once", "--idle-exit", "--push", "--thread-id", "--ephemeral", "--developer-instructions",
                  "stub", "proxy", "--list-models", "--list-loaded")
STAND_IN = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
            "o = task_io.acquire_lock(%r, %r); print('held', flush=True); sys.stdin.read(); task_io.release_lock(o)")
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
    """A manager command table for connect_common._run: (program, verb) -> rc or (rc, out, err); None =
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
        self.patches = [mock.patch.object(cm, "_run", side_effect=lambda cmd, timeout=30: self.calls.append(cmd)),
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
        with mock.patch.object(cm, "_run", side_effect=runner), mock.patch.object(cm, "is_macos", return_value=mac):
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
        self.assertEqual(cli.connector_argv(cfg, str(self.work))[-5:],
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
            seen.update(argv=argv, env=kw["env"], cwd=kw["cwd"], stdin=kw["stdin"])
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
                                        str(self.work), "--held", "--codex", str(self.codex), "--server", "stdio",
                                        "--sandbox", "read-only"])
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

            def poll(self):
                return self.rc

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

    def test_a_connector_the_attach_cannot_name_is_reaped(self):
        self.configure()
        with mock.patch.object(task_io, "attach_consumer", side_effect=holder_lock.LockError("record changed")):
            code, seen, err = self.run_with()
        self.assertEqual(code, 2, err)
        self.assertIn("no consumer: record changed", err)
        self.assertEqual(task_io.lock_held(str(self.work)), "free")


class HeldConnector(Base):
    """The connector's --held: no lock of its own, a start only once a holder attached it."""

    def test_held_without_a_holder_is_refused_and_takes_no_lock(self):
        self.work.mkdir()
        for d in cli.WORK_DIRS:
            (self.work / d).mkdir(parents=True, exist_ok=True)
        err = io.StringIO()
        with mock.patch.object(cc, "HELD_WAIT_S", 0.3), contextlib.redirect_stderr(err):
            rc = bounded(lambda: cc.main(["--work", str(self.work), "--held", "--server", "stub"]))
        self.assertEqual(rc, 2)
        self.assertIn("no holder of %s attached this process" % self.work, err.getvalue())
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_held_whose_record_names_this_pid_without_verified_liveness_is_refused(self):
        """A stale record (a reused pid, live False) or an unreadable lineage (live None) serves nothing."""
        self.work.mkdir()
        for d in cli.WORK_DIRS:
            (self.work / d).mkdir(parents=True, exist_ok=True)
        mine = task_io.start_time(os.getpid())
        for record, word in (({"start": mine, "live": False}, "liveness false"), ({"start": mine, "live": None}, "liveness unknown"),
                             ({"start": "bogus", "live": True}, "a start time that is not this process's")):
            err = io.StringIO()
            with mock.patch.object(cc, "holder_consumer", return_value=dict(record, pid=os.getpid())), \
                    mock.patch.object(cc, "HELD_WAIT_S", 0.3), contextlib.redirect_stderr(err):
                rc = bounded(lambda: cc.main(["--work", str(self.work), "--held", "--server", "stub", "--idle-exit", "0.2"]))
            self.assertEqual(rc, 2, (record, err.getvalue()))
            self.assertIn("names this pid with %s, not verified" % word, err.getvalue())
            self.assertFalse((self.work / "state" / "app-server-stub.stderr").exists(), "no app-server was started")
        self.assertEqual(task_io.lock_held(str(self.work)), "free")

    def test_held_under_a_holder_that_attached_another_process_is_refused(self):
        self.work.mkdir()
        for d in cli.WORK_DIRS:
            (self.work / d).mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sys, subprocess; sys.path.insert(0, %r); from ag2_sparrow import task_io\n"
            "o = task_io.acquire_lock(%r, 'codex-local'); c = subprocess.Popen(['sleep', '300'])\n"
            "task_io.attach_consumer(o, task_io.holder_record_bytes(o), c.pid, task_io.start_time(c.pid))\n"
            "print('held', flush=True); sys.stdin.read(); c.kill(); c.wait(); task_io.release_lock(o)")
            % (str(PKG), str(self.work))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with mock.patch.object(cc, "HELD_WAIT_S", 0.3), contextlib.redirect_stderr(io.StringIO()):
                rc = bounded(lambda: cc.main(["--work", str(self.work), "--held", "--server", "stub"]))
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        self.assertEqual(rc, 2)


class LauncherProcess(Base):
    """The launcher as a real process over the real connector and the stub app-server."""

    def launcher(self):
        env = dict(os.environ, PYTHONPATH=str(PKG))
        proc = subprocess.Popen([sys.executable, "-c", "import sys; from ag2_sparrow.adapters.codex import connect_cli\n"
                                 "sys.exit(connect_cli.main(['run']))"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        for _ in range(400):
            if self.server():
                break
            time.sleep(0.05)
        self.assertIsNotNone(self.server(), "the connector started its app-server through the fake codex")
        return proc

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
        try:
            self.assertEqual(self.server()["argv"][0], str(selected) + ".py", "the wrapper's body: the selected executable ran")
            self.assertFalse((self.home / "codex-shadow.json").exists(), "the shadowing codex never ran")
        finally:
            os.kill(launcher.pid, signal.SIGTERM)
            launcher.communicate(timeout=60)

    def test_sigterm_reaps_the_connector_and_frees_the_work_root(self):
        self.configure()
        launcher = self.launcher()
        consumer = None
        for _ in range(100):
            consumer = task_io.holder_consumer(str(self.work))
            if consumer and consumer.get("live"):
                break
            time.sleep(0.05)
        rec = task_io.lock_holder(str(self.work))
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
        for _ in range(100):
            if task_io.lock_held(str(self.work)) == "free":
                break
            time.sleep(0.05)
        self.assertEqual(task_io.lock_held(str(self.work)), "free")
        self.assertIsInstance(holder_lock.read_holder(str(self.work)), holder_lock.Missing, "no stale record")
        for _ in range(100):
            if not task_io.pid_alive(connector_pid):
                break
            time.sleep(0.05)
        self.assertFalse(task_io.pid_alive(connector_pid), "the connector is still alive")


class VerifyUninstall(Base):
    def test_verify_every_part_and_the_broken_ones(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        with mock.patch.object(cm, "_run", side_effect=runner):
            rc, out, _ = self.main("verify")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.checks(out)), {"config", "codex", "login", "inbox", "results", "relay token", "supervisor"})
        self.assertEqual(set(self.checks(out).values()), {"PASS"})
        self.assertIn("INFO sandbox: read-only", out)
        self.assertIn("INFO non-owner: refused unless", out)
        self.assertIn(f"INFO job: {L.service_name} active", out)
        self.assertIn(f"INFO session: {self.work} held by launcher pid {runner.pid()}, no live connector attached "
                      "(manager active/running)", out)
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
            with mock.patch.object(cm, "_run", side_effect=self.runner(table)):
                rc, _, err = self.main("uninstall")
            self.assertEqual(rc, 1, table)
            self.assertIn("were kept so a rerun knows the work root", err)
            self.assertTrue(unit.exists() and cli.config_path().exists())
        with mock.patch.object(cm, "_run", side_effect=runner):
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
            with mock.patch.object(cm, "_run", side_effect=self.runner({("launchctl", "bootout"): 3, ("launchctl", "print"): 0})):
                self.assertEqual(self.main("uninstall")[0], 1)
            self.assertTrue(plist.exists())
            with mock.patch.object(cm, "_run", side_effect=mac):
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
