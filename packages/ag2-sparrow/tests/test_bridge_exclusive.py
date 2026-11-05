#!/usr/bin/env python3
"""One agent, one bridge, whichever adapter runs it: with claude-local's bridge for an agent on the host,
codex-local's install or connect for that agent is refused before anything moves, and the reverse, under
systemd and launchd. connect names its agent (--agent) so that check runs before the code is spent for
that agent alone: another agent's bridge does not refuse it, no --agent refuses with the code unread, and
a code redeemed for another agent writes no seat. A same-adapter reinstall, a stale owner whose unit and job are gone, a bridge unit
from before the check, and a bridge that lands mid-connect are covered too. Driven in-process against a
fake manager for both adapters' launchers and bridges; no real manager, codex, claude or relay."""
from __future__ import annotations

import contextlib
import functools
import importlib
import io
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_code_fixtures as fx  # noqa: E402
from claude_code_fixtures import cb, cm, sm  # noqa: E402

claude_cli, codex_cli = fx.cli, importlib.import_module("ag2_sparrow.adapters.codex.connect_cli")

CL, CX = claude_cli.LOCAL, codex_cli.LOCAL
BRIDGES = {CL.name: claude_cli.BRIDGE, CX.name: codex_cli.BRIDGE}
COMMANDS = {CL.name: claude_cli.COMMAND, CX.name: codex_cli.COMMAND}
FAKE_CODEX = '''#!/bin/sh
case "$1 $2" in
  "--version "*) echo "codex-cli 0.160.1" ;;
  "login status") echo "Logged in using ChatGPT" ;;
  *) exit 3 ;;
esac
'''
START = ("restart", "start", "bootstrap", "kickstart")


def verb_of(cmd):
    return cmd[1] if cmd[0] == "launchctl" else next(a for a in cmd[1:] if not a.startswith("--"))


class LauncherJob(fx.Runner):
    """The fixtures' launcher job for either adapter: a start runs a stand-in holding the root as `shape`."""

    def __init__(self, shape, root):
        super().__init__(None, 0, str(root), adapter=shape.name)
        self.shape = shape

    def answer(self, verb, cmd):
        if verb != "show":
            return super().answer(verb, cmd)
        words = {"LoadState": "loaded" if cm.service_path(self.shape).exists() or self.registered else "not-found",
                 "ActiveState": "active" if self.alive() else "inactive",
                 "SubState": "running" if self.alive() else "dead",
                 "UnitFileState": "enabled" if self.enabled else "disabled",
                 "MainPID": str(self.session.pid) if self.alive() else "0", "InvocationID": self.invocation,
                 "ExecMainStartTimestampMonotonic": str(self.since), **self.exit_words()[0]}
        shown = (words[k] if "--value" in cmd else f"{k}={words[k]}" for k in cmd[cmd.index("-p") + 1].split(","))
        return (0, "".join(w + "\n" for w in shown), "")


class BridgeJob:
    """A bridge job's own state in the manager; `unknown` makes every probe of it fail to run."""

    def __init__(self, shape):
        self.shape, self.calls, self.active, self.enabled, self.registered, self.unknown = shape, [], False, False, False, False

    def ours(self, cmd):
        return any(self.shape.service_name in a or self.shape.agent_label in a for a in cmd)

    def started(self):
        return any(verb_of(c) in START for c in self.calls)

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        verb, out = verb_of(cmd), ""
        if self.unknown and verb in ("show", "print"):
            return None
        if verb == "show":
            words = {"LoadState": "loaded" if cm.service_path(self.shape).exists() or self.registered else "not-found",
                     "ActiveState": "active" if self.active else "inactive",
                     "UnitFileState": "enabled" if self.enabled else "disabled"}
            out = "".join(words[k] + "\n" for k in cmd[cmd.index("-p") + 1].split(","))
        elif verb == "print":
            if not self.registered:
                return subprocess.CompletedProcess(cmd, 113, "", 'Could not find service "x" in domain\n')
            out = "\tstate = running\n" if self.active else "\tstate = waiting\n"
        elif verb in ("enable", "disable"):
            self.enabled = verb == "enable"
        elif verb in START:
            self.active = self.registered = True
        elif verb in ("stop", "bootout"):
            self.active = False
            self.registered = self.registered and verb == "stop"
        return subprocess.CompletedProcess(cmd, 0, out, "")


class Jobs:
    """One manager for both adapters: a command naming a bridge's unit or label is that bridge's (checked
    first: a launcher's label is a prefix of its bridge's), one naming a launcher's is that launcher's."""

    def __init__(self, root, runners):
        self.launchers = {s.name: LauncherJob(s, root) for s in (CL, CX)}
        self.bridges = {name: BridgeJob(shape) for name, shape in BRIDGES.items()}
        runners.extend(self.launchers.values())

    def __call__(self, cmd, timeout=30):
        for job in self.bridges.values():
            if job.ours(cmd):
                return job(cmd)
        for job in self.launchers.values():
            if any(job.shape.service_name in a or job.shape.agent_label in a for a in cmd):
                return job(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0, "", "")


class Exclusive(fx.AgentRootBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(self.home / "host-state")
        self.codex = self.bin / "codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o755)
        self.codex_home = self.home / ".codex-agent"
        self.codex_home.mkdir()
        self.mac = False    # every path and probe below, not only the CLI's, follows the manager under test
        mac = mock.patch.object(cm, "is_macos", side_effect=lambda: self.mac)
        mac.start()
        self.addCleanup(mac.stop)

    def jobs(self):
        return Jobs(self.root, self.runners)

    def run_cli(self, name, argv, jobs, mac, stdin=None, backend=None):
        out, err = io.StringIO(), io.StringIO()
        main = claude_cli.main if name == CL.name else codex_cli.main
        self.mac = mac
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sm, "_run", side_effect=jobs))
            if backend is not None:
                stack.enter_context(mock.patch.object(urllib.request, "urlopen", side_effect=backend))
                stack.enter_context(mock.patch.object(cm, "redeem_code", side_effect=functools.partial(
                    cm.redeem_code, sleep=lambda s: None)))
                stack.enter_context(mock.patch.object(sys, "stdin", stdin or io.StringIO(fx.CODE + "\n")))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def install(self, name, jobs, mac=False):
        if name == CL.name:
            argv = ["install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                    "--hmac-key-file", str(self.home / "task-hmac.key")]
        else:
            argv = ["install", "--codex", str(self.codex), "--codex-home", str(self.codex_home),
                    "--token-env-file", str(self.relay)]
        return self.run_cli(name, argv, jobs, mac)

    def connect(self, name, jobs, backend, mac=False, agent=fx.AGENT, stdin=None):
        named = ["--agent", agent] if agent else []
        if name == CL.name:
            argv = ["connect", "--base", fx.BASE, *named, "--code", "-", "--plugin-dir", str(self.plugin),
                    "--claude-config-dir", str(self.home / ".claude-sutando"),
                    "--hmac-key-file", str(self.home / "task-hmac.key")]
        else:
            argv = ["connect", "--experimental-connect", "--base", fx.BASE, *named, "--code", "-", "--codex",
                    str(self.codex), "--codex-home", str(self.codex_home)]
        return self.run_cli(name, argv, jobs, mac, stdin=stdin, backend=backend)

    def bridged(self, name, mac):
        """`name` installed for the agent, its bridge running, then its session stopped: the bridge runs on."""
        jobs = self.jobs()
        rc, out, err = self.install(name, jobs, mac)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(jobs.bridges[name].active, "the first adapter's bridge runs")
        self.assertEqual(cm.supervisor_path(BRIDGES[name]).suffix, ".plist" if mac else ".service")
        jobs.launchers[name].stop_session()
        return jobs

    def snapshot(self):
        files = {}
        for d in (cb.bridge_home(self.root), self.root):
            files.update({f"{d}:{k}": v for k, v in fx.tree_bytes(d).items()})
        for path in [self.relay, *(cm.config_path(s) for s in (CL, CX)), *(cm.supervisor_path(s) for s in (CL, CX)),
                     *(cm.supervisor_path(b) for b in BRIDGES.values())]:
            files[str(path)] = path.read_bytes() if path.exists() else None
        return files

    def assert_refused_untouched(self, rc, err, first, second, jobs, before):
        self.assertNotEqual(rc, 0, err)
        self.assertIn(f"already has a bridge, run by {first}", err)
        self.assertIn(f"run {COMMANDS[first]} uninstall first", err)
        self.assertFalse(jobs.bridges[second].started(), "no second bridge is started")
        self.assertFalse(any(verb_of(c) in START for c in jobs.launchers[second].calls), "no second launcher")
        self.assertTrue(jobs.bridges[first].active, "the first bridge is left running")
        self.assertEqual(self.snapshot(), before, "the seat, the root and every unit and config are byte-identical")

    def switch(self, first, second, mac):
        jobs = self.bridged(first, mac)
        before = self.snapshot()
        rc, out, err = self.install(second, jobs, mac)
        self.assert_refused_untouched(rc, out + err, first, second, jobs, before)
        self.assertIn("nothing was changed", err)

    def test_codex_install_is_refused_while_claude_locals_bridge_serves_the_agent_systemd(self):
        self.switch(CL.name, CX.name, mac=False)

    def test_codex_install_is_refused_while_claude_locals_bridge_serves_the_agent_launchd(self):
        self.switch(CL.name, CX.name, mac=True)

    def test_claude_install_is_refused_while_codex_locals_bridge_serves_the_agent_systemd(self):
        self.switch(CX.name, CL.name, mac=False)

    def test_claude_install_is_refused_while_codex_locals_bridge_serves_the_agent_launchd(self):
        self.switch(CX.name, CL.name, mac=True)

    def connect_switch(self, first, second, mac):
        jobs = self.bridged(first, mac)
        before, backend, stdin = self.snapshot(), fx.Backend(), io.StringIO(fx.CODE + "\n")
        rc, out, err = self.connect(second, jobs, backend, mac, stdin=stdin)
        self.assert_refused_untouched(rc, out + err, first, second, jobs, before)
        self.assertEqual(backend.calls, [], "the code is never redeemed")
        self.assertEqual(stdin.tell(), 0, "nor read")
        self.assertIn("the code was not used", err)

    def test_codex_experimental_connect_is_refused_before_the_code_is_spent_systemd(self):
        self.connect_switch(CL.name, CX.name, mac=False)

    def test_codex_experimental_connect_is_refused_before_the_code_is_spent_launchd(self):
        self.connect_switch(CL.name, CX.name, mac=True)

    def test_claude_connect_is_refused_before_the_code_is_spent_systemd(self):
        self.connect_switch(CX.name, CL.name, mac=False)

    def test_claude_connect_is_refused_before_the_code_is_spent_launchd(self):
        self.connect_switch(CX.name, CL.name, mac=True)

    def record(self, root, agent):
        """codex-local's config as install wrote it for `root`, its job stopped."""
        cm._write(cm.config_path(CX), cm.render_config(CX, {
            "CODEX": str(self.codex), "INBOX_DIR": f"{root}/tasks", "RESULTS_DIR": f"{root}/results",
            "AGENT_ID": agent, "BRIDGE": cb.UNIT, "ROOT": str(root)}), 0o600)

    def test_a_connect_whose_recorded_root_is_the_served_agents_is_refused_before_the_code(self):
        jobs = self.bridged(CL.name, False)
        self.record(self.root, fx.AGENT)
        before, backend = self.snapshot(), fx.Backend()
        rc, out, err = self.connect(CX.name, jobs, backend)
        self.assert_refused_untouched(rc, out + err, CL.name, CX.name, jobs, before)
        self.assertEqual(backend.calls, [])

    def test_a_connect_for_another_agent_redeems_its_code_beside_the_served_agents_bridge(self):
        other = "@other.agent:ag2.space"
        for recorded in (False, True):
            with self.subTest(recorded=recorded):
                jobs = self.bridged(CL.name, False)
                if recorded:
                    self.record(self.root.parent / "other.agent_ag2.space", other)
                backend = fx.Backend(answer={"relay_url": fx.RELAY, "bearer": fx.BEARER, "agent_id": other})
                rc, out, err = self.connect(CX.name, jobs, backend, agent=other)
                self.assertEqual(len(backend.calls), 1, out + err)
                self.assertNotIn("already has a bridge", err)
                self.assertIn(f"connected: {other}", out)
                self.assertTrue(jobs.bridges[CL.name].active, "the served agent's bridge is left running")
                for path in (cm.config_path(CX), cm.supervisor_path(BRIDGES[CX.name]), cm.supervisor_path(CX)):
                    path.unlink(missing_ok=True)

    def test_a_connect_without_agent_is_refused_before_the_code_is_read(self):
        for name in (CL.name, CX.name):
            for agent in ("", "not-a-matrix-id"):
                with self.subTest(name=name, agent=agent):
                    jobs, backend, stdin = self.jobs(), fx.Backend(), io.StringIO(fx.CODE + "\n")
                    before = self.snapshot()
                    rc, out, err = self.connect(name, jobs, backend, agent=agent, stdin=stdin)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn("copy the connect command from the app again", err)
                    self.assertIn("the code was not used", err)
                    self.assertEqual(stdin.tell(), 0, "the code is never read")
                    self.assertEqual(backend.calls, [])
                    self.assertEqual(self.snapshot(), before)

    def test_a_code_redeemed_for_another_agent_writes_no_seat(self):
        for name in (CL.name, CX.name):
            with self.subTest(name=name):
                jobs, other = self.jobs(), "@other.agent:ag2.space"
                other_home = cb.bridge_home(self.root.parent / "other.agent_ag2.space")
                before = self.snapshot()
                rc, out, err = self.connect(name, jobs, fx.Backend(), agent=other)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(f"the code is for {fx.AGENT}, not {other}; the code is spent and nothing was written",
                              err)
                self.assertEqual(self.snapshot(), before, "no seat, unit or config for the code's agent")
                self.assertFalse(other_home.exists(), "nor for the agent named")
                self.assertFalse(any(b.started() for b in jobs.bridges.values()))

    def test_a_bridge_that_lands_during_the_redemption_is_refused_before_the_seat_is_written(self):
        jobs = self.bridged(CL.name, False)
        parked = {p: p.read_bytes() for p in (cm.config_path(CL), cm.supervisor_path(BRIDGES[CL.name]))}
        for p in parked:
            p.rename(p.with_name(p.name + ".parked"))
        jobs.bridges[CL.name].active = jobs.bridges[CL.name].registered = False
        redeem = cm.redeem_code

        def landed(*a, **kw):
            for p in parked:
                p.with_name(p.name + ".parked").rename(p)
            jobs.bridges[CL.name].active = jobs.bridges[CL.name].registered = True
            return redeem(*a, **kw)
        before_seat = fx.tree_bytes(cb.bridge_home(self.root))
        backend = fx.Backend()
        with mock.patch.object(cb.cm, "redeem_code", side_effect=landed):
            rc, out, err = self.connect(CX.name, jobs, backend)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(backend.calls), 1)
        self.assertIn(f"already has a bridge, run by {CL.name}", err)
        self.assertIn("the code is spent and nothing was written", err)
        self.assertEqual(fx.tree_bytes(cb.bridge_home(self.root)), before_seat, "the seat is byte-identical")
        self.assertFalse(jobs.bridges[CX.name].started())
        self.assertFalse(cm.config_path(CX).exists())

    def test_wiring_refuses_when_another_adapters_bridge_appeared_after_the_install_check(self):
        jobs = self.bridged(CL.name, False)
        cfg = {"AGENT_ID": fx.AGENT, "BRIDGE": cb.UNIT, "TOKEN_ENV_FILE": str(self.relay), "TOKEN_VAULT_KEY": "",
               "BRIDGE_STATE_DIR": str(cb.bridge_home(self.root) / "state"), "ROOT": str(self.root)}
        with mock.patch.object(sm, "_run", side_effect=jobs), contextlib.redirect_stdout(io.StringIO()):
            problem = cb.wire_bridge_bound(cfg, COMMANDS[CX.name], BRIDGES[CX.name], CX, None)
        self.assertIn(f"already has a bridge, run by {CL.name}", problem)
        self.assertFalse(cm.supervisor_path(BRIDGES[CX.name]).exists())
        self.assertFalse(jobs.bridges[CX.name].started())

    def test_a_same_adapter_reinstall_still_works(self):
        for name, mac in ((CL.name, False), (CX.name, False), (CX.name, True)):
            with self.subTest(name=name, mac=mac):
                jobs = self.bridged(name, mac)
                rc, out, err = self.install(name, jobs, mac)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(jobs.bridges[name].active)
                jobs.launchers[name].stop_session()
                self.assertEqual(self.run_cli(name, ["uninstall"], jobs, mac)[0], 0)

    def stale(self, mac):
        """claude-local's config still names the agent's root, its bridge unit and job are gone."""
        jobs = self.bridged(CL.name, mac)
        cm.supervisor_path(BRIDGES[CL.name]).unlink()
        jobs.bridges[CL.name].active = jobs.bridges[CL.name].registered = jobs.bridges[CL.name].enabled = False
        return jobs

    def test_a_stale_owner_whose_unit_and_job_are_gone_lets_the_other_adapter_install(self):
        for mac in (False, True):
            with self.subTest(mac=mac):
                jobs = self.stale(mac)
                rc, out, err = self.install(CX.name, jobs, mac)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(jobs.bridges[CX.name].active)
                jobs.launchers[CX.name].stop_session()
                for name in (CX.name, CL.name):
                    self.assertEqual(self.run_cli(name, ["uninstall"], jobs, mac)[0], 0)

    def test_a_stale_owner_whose_job_is_still_loaded_or_unreadable_is_refused(self):
        for state in ("loaded", "unknown"):
            for mac in (False, True):
                with self.subTest(state=state, mac=mac):
                    jobs = self.stale(mac)
                    jobs.bridges[CL.name].active = jobs.bridges[CL.name].registered = True
                    jobs.bridges[CL.name].unknown = state == "unknown"
                    before = self.snapshot()
                    rc, out, err = self.install(CX.name, jobs, mac)
                    self.assert_refused_untouched(rc, out + err, CL.name, CX.name, jobs, before)
                    jobs.bridges[CL.name].unknown = jobs.bridges[CL.name].active = jobs.bridges[CL.name].registered = False
                    self.assertEqual(self.run_cli(CL.name, ["uninstall"], jobs, mac)[0], 0)

    def test_a_kept_bridge_unit_whose_job_is_stopped_is_refused(self):
        for mac in (False, True):
            with self.subTest(mac=mac):
                jobs = self.bridged(CL.name, mac)
                jobs.bridges[CL.name].active = jobs.bridges[CL.name].registered = jobs.bridges[CL.name].enabled = False
                before = self.snapshot()
                rc, out, err = self.install(CX.name, jobs, mac)
                self.assertNotEqual(rc, 0)
                self.assertIn(f"already has a bridge, run by {CL.name}", err)
                self.assertFalse(jobs.bridges[CX.name].started())
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.run_cli(CL.name, ["uninstall"], jobs, mac)[0], 0)

    def test_a_bridge_unit_from_before_the_check_is_found_without_a_config_naming_the_agent(self):
        for mac in (False, True):
            with self.subTest(mac=mac):
                jobs = self.bridged(CL.name, mac)
                conf = cm.config_path(CL)
                kept = conf.read_bytes()
                conf.unlink()
                before = self.snapshot()
                rc, out, err = self.install(CX.name, jobs, mac)
                self.assert_refused_untouched(rc, out + err, CL.name, CX.name, jobs, before)
                conf.write_bytes(kept)
                self.assertEqual(self.run_cli(CL.name, ["uninstall"], jobs, mac)[0], 0)

    def test_a_bridge_for_another_agent_does_not_refuse_an_install(self):
        jobs = self.bridged(CL.name, False)
        other = str(self.root.parent / "other.agent_ag2.space")
        for path in (cm.config_path(CL), cm.supervisor_path(BRIDGES[CL.name])):
            path.write_text(path.read_text().replace(str(self.root), other).replace(fx.AGENT, "@other.agent:ag2.space"))
        self.assertEqual(cb.bridge_owner_problem(CX, str(self.root), fx.AGENT), "")
        self.assertIn(f"run by {CL.name}", cb.bridge_owner_problem(CX, None))

    def test_a_bridge_declared_for_a_host_supervisor_refuses_until_its_adapter_uninstalls(self):
        cm._write(cm.config_path(CL), cm.render_config(CL, {"AGENT_ID": fx.AGENT, "BRIDGE": "sparrowd",
                                                            "ROOT": str(self.root)}), 0o600)
        problem = cb.bridge_owner_problem(CX, str(self.root), fx.AGENT)
        self.assertIn(f"run by {CL.name} (declared for sparrowd)", problem)
        self.assertIn(f"run {COMMANDS[CL.name]} uninstall first", problem)
        cm.config_path(CL).unlink()
        self.assertEqual(cb.bridge_owner_problem(CX, str(self.root), fx.AGENT), "")

    def test_the_unit_environment_is_read_back_from_both_managers_renderings(self):
        env = {"AGENT_CONNECT_TASK_DIR": '/a "b"/100%/x\\y/tasks', "AGENT_MXID": fx.AGENT}
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                text, problem = cm.render_supervisor(BRIDGES[CX.name], env)
                self.assertEqual(problem, "")
                got = cb._unit_environ(text)
                self.assertEqual({k: got[k] for k in env}, env)
                self.assertEqual(cb._writer(text), COMMANDS[CX.name])


if __name__ == "__main__":
    unittest.main()
