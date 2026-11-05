#!/usr/bin/env python3
"""Several agents on one host, on claude-local and codex-local alike: one job per agent
(`ai.ag2.sparrow.<adapter>.<agent key>`, `ag2-sparrow-<adapter>.<agent key>.service`) with its own config
dir, unit, bridge and log. Installing, verifying, reinstalling (the restart) or uninstalling one agent
never names another's job; the agent-less form works with one agent installed and refuses with several;
an earlier single-agent job keeps its fixed name and is verified, reinstalled and removed as it is; and
one agent is still one bridge across adapters. Driven in-process against a fake manager with one fake
per job, under systemd and launchd; no real manager, claude, codex or relay."""
from __future__ import annotations

import contextlib
import functools
import json
import importlib
import plistlib
import shlex
import re
import shutil
import io
import os
import subprocess
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_code_fixtures as fx  # noqa: E402
import test_bridge_exclusive as bx  # noqa: E402

cb, cm, sm = fx.cb, fx.cm, fx.sm

claude_cli, codex_cli = fx.cli, importlib.import_module("ag2_sparrow.adapters.codex.connect_cli")
CL, CX = claude_cli.LOCAL, codex_cli.LOCAL
CLIS = {CL.name: claude_cli, CX.name: codex_cli}
ALPHA, BETA = "@alpha.agent:ag2.space", "@beta.agent:ag2.space"
SEGMENTS = {ALPHA: "alpha.agent_ag2.space", BETA: "beta.agent_ag2.space"}
STARTS = ("restart", "start", "bootstrap", "kickstart")


def job(adapter: str, agent: str) -> cm.Shape:
    """`adapter`'s job for `agent`: its own, or the single-agent one when `agent` is ''."""
    return cm.for_key({CL.name: CL, CX.name: CX}[adapter], cm.agent_key(agent) if agent else "")


def names(cmd, shape: cm.Shape) -> bool:
    """Whether a manager command names exactly `shape`'s job: a single-agent name is a prefix of a per-agent one."""
    own = (shape.service_name, cm.launchd_service(shape), str(cm.supervisor_path(shape)))
    return any(a in own for a in cmd)


class Host:
    """One fake per job, each answering only the commands that name it exactly; the rest answer 0."""

    def __init__(self, runners):
        self.launchers, self.bridges, self.runners = {}, {}, runners

    def add(self, shape: cm.Shape, root) -> None:
        self.launchers[shape.job] = bx.LauncherJob(shape, root)
        self.bridges[shape.job] = bx.BridgeJob(cb.bridge_shape(shape))
        self.runners.append(self.launchers[shape.job])

    def __call__(self, cmd, timeout=30):
        for name, bridge in self.bridges.items():
            if names(cmd, bridge.shape):
                return bridge(cmd)
        for name, launcher in self.launchers.items():
            if names(cmd, launcher.shape):
                return launcher(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def calls_naming(self, shape: cm.Shape) -> list:
        return list(self.launchers[shape.job].calls) + list(self.bridges[shape.job].calls)


class MultiBase(fx.AgentRootBase):
    def setUp(self):
        super().setUp()
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(self.home / "host-state")
        self.codex = self.bin / "codex"
        self.codex.write_text(bx.FAKE_CODEX)
        self.codex.chmod(0o755)
        self.codex_home = self.home / ".codex-agent"
        self.codex_home.mkdir()
        self.tokens = {}
        for agent in (ALPHA, BETA):
            path = self.home / f"{SEGMENTS[agent]}.env"
            path.write_text(f"REMOTE_TASK_TOKEN={fx.TOKEN}\nAGENT_MXID={agent}\n")
            path.chmod(0o600)
            self.tokens[agent] = path
        self.mac = False
        mac = mock.patch.object(cm, "is_macos", side_effect=lambda: self.mac)
        mac.start()
        self.addCleanup(mac.stop)
        self.host = Host(self.runners)
        for adapter in CLIS:
            for agent in (ALPHA, BETA):
                self.host.add(job(adapter, agent), self.root_of(agent))
            self.host.add(job(adapter, ""), self.root_of(ALPHA))     # an earlier release's job, for alpha

    def root_of(self, agent: str) -> Path:
        return self.agents / SEGMENTS[agent]

    def run_cli(self, adapter: str, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sm, "_run", side_effect=self.host), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = CLIS[adapter].main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def install(self, adapter: str, agent: str, *extra):
        if adapter == CL.name:
            argv = ["install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                    "--hmac-key-file", str(self.home / "task-hmac.key")]
        else:
            argv = ["install", "--codex", str(self.codex), "--codex-home", str(self.codex_home)]
        return self.run_cli(adapter, *argv, "--token-env-file", str(self.tokens[agent]), *extra)

    def install_work(self, adapter: str):
        """An explicit-root install (`--work`): the adapter's single fixed job, recording no agent."""
        self.relay.write_text(f"REMOTE_TASK_TOKEN={fx.TOKEN}\n")
        if adapter == CL.name:
            argv = ["install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                    "--hmac-key-file", str(self.home / "task-hmac.key")]
        else:
            argv = ["install", "--codex", str(self.codex), "--codex-home", str(self.codex_home)]
        rc, out, err = self.run_cli(adapter, *argv, "--work", str(self.work))
        self.assertEqual(rc, 0, out + err)

    def token_for(self, agent: str) -> Path:
        path = self.home / f"{cm.agent_identity.root_segment(agent)}.env"
        path.write_text(f"REMOTE_TASK_TOKEN={fx.TOKEN}\nAGENT_MXID={agent}\n")
        path.chmod(0o600)
        self.tokens[agent] = path
        return path

    def calls(self) -> int:
        return sum(len(j.calls) for j in [*self.host.launchers.values(), *self.host.bridges.values()])

    def installed(self, adapter: str, agent: str, *extra):
        rc, out, err = self.install(adapter, agent, *extra)
        self.assertEqual(rc, 0, out + err)
        return out

    def files(self, shape: cm.Shape) -> dict:
        paths = (cm.config_path(shape), cm.supervisor_path(shape), cm.supervisor_path(cb.bridge_shape(shape)))
        return {str(p): p.read_bytes() if p.exists() else None for p in paths}

    def launcher_pid(self, shape: cm.Shape):
        return self.host.launchers[shape.job].pid()

    def holder_adapter(self, agent: str):
        return fx.task_io_lock_holder(str(self.root_of(agent))).get("adapter")


def per_manager(test):
    """`test(self, adapter)` once per adapter and manager, each in a home of its own (a test method each)."""
    def make(adapter, mac):
        def method(self):
            self.mac = mac
            test(self, adapter)
        method.__doc__ = test.__doc__
        return f"{adapter.replace('-', '_')}_{'launchd' if mac else 'systemd'}", method
    return [make(a, m) for a in (CL.name, CX.name) for m in (False, True)]


def expand(cls):
    """Each `test_x = per_manager(...)` becomes test_x_<adapter>_<manager>, one method each."""
    for name, value in list(vars(cls).items()):
        if name.startswith("test_") and isinstance(value, list):
            delattr(cls, name)
            for suffix, method in value:
                method.__name__ = f"{name}_{suffix}"
                setattr(cls, method.__name__, method)
    return cls


@expand
class SideBySide(MultiBase):
    def _side_by_side(self, adapter):
        """Two agents install side by side: each its own config dir, unit, bridge, key in the unit, and root."""
        a, b = job(adapter, ALPHA), job(adapter, BETA)
        self.installed(adapter, ALPHA)
        self.installed(adapter, BETA)
        for shape, agent in ((a, ALPHA), (b, BETA)):
            conf = cm.config_path(shape)
            self.assertEqual(conf, cm.config_root() / cm.AGENTS_DIR / cm.agent_key(agent) / f"{adapter}.conf")
            self.assertEqual(cm.read_config(conf)["AGENT_ID"], agent)
            self.assertEqual(cm.read_config(conf)["ROOT"], str(self.root_of(agent)))
            unit = cm.supervisor_path(shape).read_text()
            self.assertIn(cm.AGENT_VAR, unit)
            self.assertIn(cm.agent_key(agent), unit)
            self.assertTrue(cm.supervisor_path(cb.bridge_shape(shape)).exists())
            self.assertTrue(self.host.launchers[shape.job].alive(), shape.job)
            self.assertTrue(self.host.bridges[shape.job].active, shape.job)
            self.assertEqual(self.holder_adapter(agent), adapter)
        self.assertNotEqual(cm.job_name(a), cm.job_name(b))
        self.assertFalse(cm.config_path(job(adapter, "")).exists(), "no single-agent config is written")
        self.assertEqual(self.host.calls_naming(job(adapter, "")), [], "nor is the single-agent job asked anything")
        rc, out, err = self.run_cli(adapter, "list")
        self.assertEqual(rc, 0, err)
        for shape, agent in ((a, ALPHA), (b, BETA)):
            self.assertIn(f"{agent}  {cm.job_name(shape)}", out)
            self.assertIn(str(self.root_of(agent)), out)

    test_two_agents_install_side_by_side = per_manager(_side_by_side)

    def _isolated(self, adapter):
        """Verify, reinstall (the restart) and uninstall of one agent never name the other's job nor touch its files."""
        a, b = job(adapter, ALPHA), job(adapter, BETA)
        self.installed(adapter, ALPHA)
        self.installed(adapter, BETA)
        b_files, b_pid = self.files(b), self.launcher_pid(b)
        b_calls = len(self.host.calls_naming(b))
        a_pid = self.launcher_pid(a)
        rc, out, err = self.run_cli(adapter, "verify", "--agent", ALPHA)
        self.assertIn(f"PASS config: {cm.config_path(a)}", out, err)
        self.assertIn("stopped: " + cm.job_name(a), self.installed(adapter, ALPHA))
        self.assertNotEqual(self.launcher_pid(a), a_pid, "the reinstall restarted alpha's launcher")
        rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(cm.config_path(a).exists())
        self.assertFalse(cm.supervisor_path(a).exists())
        self.assertFalse(self.host.bridges[a.job].active, "alpha's bridge is stopped")
        self.assertEqual(len(self.host.calls_naming(b)), b_calls, "no manager command named beta's jobs")
        self.assertEqual(self.files(b), b_files, "beta's config and units are byte-identical")
        self.assertEqual(self.launcher_pid(b), b_pid, "beta's launcher runs on")
        self.assertTrue(self.host.bridges[b.job].active, "beta's bridge runs on")
        rc, out, err = self.run_cli(adapter, "verify")
        self.assertIn(f"PASS config: {cm.config_path(b)}", out, "the agent-less verify finds the one left")
        rc, out, err = self.run_cli(adapter, "uninstall")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(cm.config_path(b).exists())
        self.assertEqual(cm.installed_jobs(a), [])

    test_one_agents_commands_leave_the_other_alone = per_manager(_isolated)

    def _agentless(self, adapter):
        """With several agents installed, an agent-less verb refuses, names each, and asks the manager nothing."""
        self.installed(adapter, ALPHA)
        self.installed(adapter, BETA)
        snapshot = {**self.files(job(adapter, ALPHA)), **self.files(job(adapter, BETA))}
        calls = sum(len(j.calls) for j in [*self.host.launchers.values(), *self.host.bridges.values()])
        verbs = [["verify"], ["uninstall"]] + ([["attach"], ["stop", "--confirm"]] if adapter == CL.name else [["debug-run"]])
        for argv in verbs:
            with self.subTest(argv=argv), mock.patch.object(os, "execv") as execv:
                rc, out, err = self.run_cli(adapter, *argv)
                self.assertFalse(execv.called, "attach opens no pane")
                self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                self.assertIn("2 jobs are installed", err)
                for agent in (ALPHA, BETA):
                    self.assertIn(f"{agent} ({cm.job_name(job(adapter, agent))}; --agent {agent})", err)
                self.assertIn("pass the selector of the one you mean", err)
        self.assertEqual(sum(len(j.calls) for j in [*self.host.launchers.values(), *self.host.bridges.values()]), calls)
        self.assertEqual({**self.files(job(adapter, ALPHA)), **self.files(job(adapter, BETA))}, snapshot)
        rc, out, err = self.run_cli(adapter, "verify", "--agent", "not-a-matrix-id")
        self.assertEqual(rc, cm.EXIT_UNUSABLE)
        self.assertIn("is not a Matrix ID", err)

    test_agentless_verbs_refuse_with_several_agents = per_manager(_agentless)

    def _legacy(self, adapter):
        """An earlier single-agent job keeps its fixed name: listed, verified, reinstalled and removed as it is,
        beside a new agent's own job; once removed, the agent's next install is a job of its own."""
        single, a, b = job(adapter, ""), job(adapter, ALPHA), job(adapter, BETA)
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.installed(adapter, ALPHA)      # what an earlier release wrote: the fixed job
        self.assertEqual(cm.job_name(single), f"ai.ag2.sparrow.{adapter}" if self.mac else f"ag2-sparrow-{adapter}.service")
        self.assertNotIn(cm.AGENT_VAR, cm.supervisor_path(single).read_text())
        self.assertTrue(self.host.bridges[single.job].active)
        rc, out, err = self.run_cli(adapter, "list")
        self.assertIn(f"{ALPHA}  {cm.job_name(single)}", out)
        for argv in (["verify"], ["verify", "--agent", ALPHA]):
            self.assertIn(f"PASS config: {cm.config_path(single)}", self.run_cli(adapter, *argv)[1], argv)
        pid = self.launcher_pid(single)
        self.installed(adapter, ALPHA)
        self.assertNotEqual(self.launcher_pid(single), pid, "the reinstall restarted the single-agent job")
        self.assertFalse(cm.config_dir(a).exists(), "and wrote no job of alpha's own")
        self.assertEqual(self.host.calls_naming(a), [])
        single_files, pid = self.files(single), self.launcher_pid(single)
        self.installed(adapter, BETA)
        self.assertTrue(self.host.launchers[b.job].alive())
        self.assertEqual((self.files(single), self.launcher_pid(single)), (single_files, pid), "beta leaves it as it is")
        rc, out, err = self.run_cli(adapter, "verify")
        self.assertEqual(rc, cm.EXIT_UNUSABLE)
        self.assertIn(f"{ALPHA} ({cm.job_name(single)}; --agent {ALPHA})", err)
        b_files = self.files(b)
        rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(set(self.files(single).values()), {None}, "the single-agent job's files are removed")
        self.assertFalse(self.host.bridges[single.job].active)
        self.assertEqual(self.files(b), b_files)
        self.installed(adapter, ALPHA)
        self.assertTrue(cm.config_path(a).exists(), "alpha's next install is a job of its own")
        self.assertFalse(cm.config_path(single).exists())

    test_a_single_agent_job_from_before_keeps_working = per_manager(_legacy)

    def _cross(self, adapter):
        """One agent, one bridge: the agent installed on one adapter is refused on the other, its own job or the
        single-agent one; another agent is not."""
        other = CX.name if adapter == CL.name else CL.name
        for single in (False, True):
            with self.subTest(single=single):
                if single:
                    with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
                        self.installed(adapter, ALPHA)
                else:
                    self.installed(adapter, ALPHA)
                rc, out, err = self.install(other, ALPHA)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(f"already has a bridge, run by {adapter}", err)
                which = f" --agent {ALPHA}"     # the single job too: --agent selects it while it records the agent
                self.assertIn(f"run {CLIS[adapter].COMMAND} uninstall{which} first", err)
                self.assertFalse(self.host.bridges[job(other, ALPHA).job].started())
                self.assertFalse(cm.config_path(job(other, ALPHA)).exists())
                self.installed(other, BETA)
                for cli_name, agent in ((adapter, ALPHA), (other, BETA)):
                    self.assertEqual(self.run_cli(cli_name, "uninstall", "--agent", agent)[0], 0)

    test_one_agent_is_still_one_bridge_across_adapters = per_manager(_cross)

    def _unreadable(self, adapter):
        """A single-agent job whose config cannot be read is still its agent's (its bridge unit names it): this
        adapter's install goes to that job and refuses on its config; the other adapter's is refused by its bridge."""
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.installed(adapter, ALPHA)
        conf = cm.config_path(job(adapter, ""))
        conf.write_text(conf.read_text() + "ROOT='unclosed\n")
        for name in (adapter, CX.name if adapter == CL.name else CL.name):
            with self.subTest(name=name):
                rc, out, err = self.install(name, ALPHA)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn("fix or remove it by hand" if name == adapter else
                              f"already has a bridge, run by {adapter} ({cm.job_name(cb.bridge_shape(job(adapter, '')))})",
                              err)
                self.assertFalse(cm.config_path(job(name, ALPHA)).exists())
                self.assertFalse(self.host.launchers[job(name, ALPHA).job].calls, "its job is never asked to start")

    test_an_unreadable_single_agent_config_still_holds_its_bridge = per_manager(_unreadable)

    def _explicit(self, adapter):
        """An explicit-root job beside an agent's own: each is selected by its own flag (--work, --agent), the
        refusal names --work for it, and verify and uninstall of either never touch the other."""
        single, b = job(adapter, ""), job(adapter, BETA)
        self.host.add(single, self.work)
        self.install_work(adapter)
        self.assertEqual(cm.recorded_agent(single), "")
        self.installed(adapter, BETA)
        rc, out, err = self.run_cli(adapter, "verify")
        self.assertEqual(rc, cm.EXIT_UNUSABLE)
        self.assertIn(f"no agent recorded ({cm.job_name(single)}; --work {self.work})", err)
        for selector, mine, other in ((["--work", str(self.work)], single, b), (["--agent", BETA], b, single)):
            with self.subTest(selector=selector):
                files, pid, calls = self.files(other), self.launcher_pid(other), len(self.host.calls_naming(other))
                self.assertIn(f"PASS config: {cm.config_path(mine)}", self.run_cli(adapter, "verify", *selector)[1])
                rc, out, err = self.run_cli(adapter, "uninstall", *selector)
                self.assertEqual(rc, 0, out + err)
                self.assertFalse(cm.config_path(mine).exists())
                self.assertEqual((self.files(other), self.launcher_pid(other)), (files, pid), "the other is untouched")
                self.assertEqual(len(self.host.calls_naming(other)), calls, "and its job never named")
                (self.install_work(adapter) if mine == single else self.installed(adapter, BETA))
        self.assertEqual(self.run_cli(adapter, "verify", "--work", str(self.home / "elsewhere"))[0], cm.EXIT_UNUSABLE)
        rc, out, err = self.run_cli(adapter, "uninstall", "--work", str(self.work), "--agent", BETA)
        self.assertEqual(rc, cm.EXIT_UNUSABLE)
        self.assertIn("each select a job: pass one", err)

    test_an_explicit_root_job_beside_an_agents_own_is_selected_by_its_root = per_manager(_explicit)

    def _leftover(self, adapter):
        """A single unit left without its config counts as installed: the agent-less form refuses naming --single,
        and uninstall --single removes it without naming the agent's own job."""
        single, a = job(adapter, ""), job(adapter, ALPHA)
        self.installed(adapter, ALPHA)
        unit, problem = cm.render_supervisor(single)
        self.assertEqual(problem, "")
        cm._write(cm.supervisor_path(single), unit, 0o644)
        files, pid, calls = self.files(a), self.launcher_pid(a), len(self.host.calls_naming(a))
        rc, out, err = self.run_cli(adapter, "verify")
        self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
        self.assertIn(f"no agent recorded ({cm.job_name(single)}; --single)", err)
        rc, out, err = self.run_cli(adapter, "uninstall", "--single")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(cm.supervisor_path(single).exists())
        self.assertEqual((self.files(a), self.launcher_pid(a)), (files, pid))
        self.assertEqual(len(self.host.calls_naming(a)), calls)
        self.assertEqual(cm.installed_jobs(single), [a])

    test_a_single_unit_left_without_its_config_is_selected_by_single = per_manager(_leftover)

    def _recovered(self, adapter):
        """A single-agent job whose config was removed (as an unreadable config's refusal says to) is found again by
        its bridge unit: install keeps that job; uninstall --agent stops it and unwires its bridge; the next
        install is the agent's own job."""
        single, a = job(adapter, ""), job(adapter, ALPHA)
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.installed(adapter, ALPHA)
        bridge = cb.bridge_shape(single)
        cm.config_path(single).unlink()
        self.assertEqual((cm.recorded_agent(single), cm.recorded_root(single)), (ALPHA, str(self.root_of(ALPHA))))
        self.assertIn(f"{ALPHA}  {cm.job_name(single)}", self.run_cli(adapter, "list")[1])
        verify = self.run_cli(adapter, "verify")[1]
        self.assertIn(f"or remove this job: {CLIS[adapter].COMMAND} uninstall --agent {ALPHA}", verify,
                      "verify names the uninstall that recovers it, with list's selector")
        self.installed(adapter, ALPHA)
        self.assertTrue(cm.config_path(single).exists(), "install rewrote the single job's config")
        self.assertFalse(cm.config_dir(a).exists(), "and made no job of the agent's own")
        cm.config_path(single).unlink()
        rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(set(self.files(single).values()), {None}, "its unit and its bridge unit are removed")
        self.assertFalse(self.host.bridges[single.job].active, "its bridge is stopped")
        self.assertIn(f"removed: {cm.supervisor_path(bridge)}", out)
        self.installed(adapter, ALPHA)
        self.assertTrue(cm.config_path(a).exists(), "the next install is the agent's own job")

    test_a_single_job_whose_config_was_removed_is_recovered_by_its_bridge_unit = per_manager(_recovered)

    def run_files(self, adapter: str, shape: cm.Shape) -> None:
        """What a launcher leaves: claude-local's locks, alarm, socket and pane-keys guard; codex-local's none."""
        if adapter != CL.name:
            return
        for name in shape.run_files:
            (cm.config_dir(shape) / name).touch()
        sock = claude_cli.launcher.socket_path(cm.config_dir(shape))
        self.assertEqual(claude_cli.launcher.run_dir_problem(cm.config_dir(shape)), "")
        sock.touch()
        fx.tmux_pane_keys.prepare_guard(sock)

    def _cleanup(self, adapter):
        """uninstall removes the agent's config dir and run dir once they hold only this job's files; a file it
        did not write, the other adapter's config, or an uncertain pane-keys fence keeps them, untouched."""
        a, b = job(adapter, ALPHA), job(adapter, BETA)
        other = CX.name if adapter == CL.name else CL.name
        cases = [("clean", None), ("foreign file", "notes.txt"), ("the other adapter's config", f"{other}.conf")]
        if adapter == CL.name:
            cases.append(("an uncertain fence", "uncertain"))
        for name, extra in cases:
            with self.subTest(case=name):
                self.installed(adapter, ALPHA)
                self.run_files(adapter, a)
                run = cm.run_dir(cm.config_dir(a))
                planted = None
                if extra == "uncertain":
                    planted = fx.tmux_pane_keys.guard_path(claude_cli.launcher.socket_path(cm.config_dir(a))) / extra
                elif extra:
                    planted = cm.config_dir(a) / extra
                if planted is not None:
                    planted.write_text("not uninstall's")
                rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
                self.assertEqual(rc, 0, out + err)
                self.assertFalse(cm.config_path(a).exists())
                if planted is None:
                    self.assertFalse(cm.config_dir(a).exists(), out)
                    self.assertFalse(run.exists() and run != cm.config_dir(a), out)
                    self.assertIn(f"removed: {cm.config_dir(a)}", out)
                else:
                    self.assertEqual(planted.read_text(), "not uninstall's", "an entry it did not write is kept")
                    self.assertIn(f"kept: {planted.parent if extra != 'uncertain' else run}", out)
                    shutil.rmtree(cm.config_dir(a), ignore_errors=True)
                    shutil.rmtree(run, ignore_errors=True)
                self.assertFalse(cm.config_dir(b).exists(), "and nothing of another agent's was there to touch")

    test_uninstall_removes_the_agents_config_and_run_dirs_once_only_its_files_are_left = per_manager(_cleanup)

    def _unmarked(self, adapter):
        """A single job's bridge unit that no longer carries install's marker names no job to recover, yet still
        names its agent: that agent's own install on the same adapter is refused, one agent one bridge."""
        single, a = job(adapter, ""), job(adapter, ALPHA)
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.installed(adapter, ALPHA)
        cm.config_path(single).unlink()
        unit = cm.supervisor_path(cb.bridge_shape(single))
        unit.write_text(unit.read_text().replace("Written by", "Edited from"))
        self.assertEqual(cm.recovered_config(single), {})
        rc, out, err = self.install(adapter, ALPHA)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"already has a bridge, run by {adapter}", err)
        self.assertFalse(cm.config_path(a).exists())
        self.assertFalse(self.host.launchers[a.job].calls)

    test_an_unmarked_single_bridge_unit_still_holds_its_agent = per_manager(_unmarked)

    def test_the_printed_claude_commands_name_the_job(self):
        """With several agents, every command verify prints, and the card's attach line, selects this job."""
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                for agent in (ALPHA, BETA):
                    self.installed(CL.name, agent)
                out = self.run_cli(CL.name, "verify", "--agent", BETA)[1]
                line = f"INFO attach: {claude_cli.COMMAND} attach --agent {BETA}"
                self.assertIn(line, out)
                cfg = cm.read_config(cm.config_path(job(CL.name, BETA)))
                self.assertEqual(claude_cli.launcher.Launcher(cfg, cm.config_dir(job(CL.name, BETA))).attach_command(),
                                 f"{claude_cli.COMMAND} attach --agent {BETA}")
                with mock.patch.object(os, "execv") as execv:
                    rc, out, err = self.run_cli(CL.name, *line.split(claude_cli.COMMAND + " ")[1].split())
                self.assertTrue(execv.called, err)
                self.assertIn(str(claude_cli.launcher.socket_path(cm.config_dir(job(CL.name, BETA)))),
                              execv.call_args[0][1])
                for agent in (ALPHA, BETA):
                    self.assertEqual(self.run_cli(CL.name, "uninstall", "--agent", agent)[0], 0)


# Pairs that shared one key before the digest form took its `__` separator: a long id and a plain one,
# and an id whose case folds away and a plain one.
PAIRS = (("@ab:cdefghijklmnopqrstuvwxyz.example", "@ab:cdefghijkl-99dc128207"),
         ("@Mixed:Case.Server", "@mixed:case.se-9d2a90782e"))
PAIR = PAIRS[0]


def dash_key(agent_id: str) -> str:
    """The digest form without its `__` separator: what let a plain id spell another agent's key."""
    segment = cm.agent_identity.root_segment(agent_id)
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,23}", segment):     # round 1's 24-character bound
        return segment
    return f"{segment[:13].rstrip('._-')}-{cm.hashlib.sha256(agent_id.encode()).hexdigest()[:10]}"


PRINTED = re.compile(r"ag2-sparrow-(?:claude|codex)-local uninstall(?: --(?:agent|work) \S+| --single)?")


class Round4(MultiBase):
    def printed(self, text: str) -> "tuple[str, list]":
        """The first uninstall command `text` prints: (its adapter, its argv), as an operator would copy it."""
        found = PRINTED.search(text)
        self.assertIsNotNone(found, text)
        words = found.group(0).rstrip(",;)").split()
        return ("claude-local" if "claude" in words[0] else "codex-local"), words[1:]

    def run_printed(self, text: str, selector: str):
        adapter, argv = self.printed(text)
        self.assertEqual(" ".join(argv[1:]), selector, f"the printed command names its job: {text}")
        rc, out, err = self.run_cli(adapter, *argv)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("pass the selector", err)

    def test_every_printed_uninstall_command_names_its_job_and_runs_verbatim(self):
        """With two jobs installed, the commands the cross-adapter refusal, uninstall's manual recovery and a
        failed install print each carry the job's selector, and each succeeds when run as printed."""
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                a = job(CL.name, ALPHA)
                for agent in (ALPHA, BETA):
                    self.installed(CL.name, agent)
                rc, out, err = self.install(CX.name, ALPHA)
                self.assertNotEqual(rc, 0)
                self.run_printed(err, f"--agent {ALPHA}")
                self.installed(CL.name, ALPHA)
                runner = self.host.launchers[a.job]
                runner.table.update({("systemctl", "show"): None, ("launchctl", "print"): None})
                rc, out, err = self.run_cli(CL.name, "uninstall", "--agent", ALPHA)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("3. re-run ", err)
                runner.table.clear()
                self.run_printed(err.split("3. re-run ", 1)[1], f"--agent {ALPHA}")
                runner.root = None      # the manager starts the job, but no launcher takes the root
                rc, out, err = self.install(CL.name, ALPHA)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("did not acquire", err)
                self.run_printed(err, f"--agent {ALPHA}")
                runner.root = str(self.root_of(ALPHA))
                self.assertEqual(self.run_cli(CL.name, "uninstall", "--agent", BETA)[0], 0)
                self.assertEqual(cm.installed_jobs(CL), [])
                self.assertFalse((cm.config_root() / cm.AGENTS_DIR).exists(), "the empty agents dir goes too")
                self.assertFalse((cm.config_root() / cm.RUN_DIR).exists(), "and the empty run dir")

    def test_an_explicit_root_job_and_the_agents_own_job_are_never_aliased(self):
        """A claude-local explicit-root job records the host's agent: beside that agent's own job, --agent
        refuses as ambiguous and names each job's own selector (--work for the fixed one, --key for the agent's own),
        each of which selects its own job."""
        single, a = job(CL.name, ""), job(CL.name, ALPHA)
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                self.host.add(single, self.work)
                self.installed(CL.name, ALPHA)
                self.relay.write_text(f"REMOTE_TASK_TOKEN={fx.TOKEN}\nAGENT_MXID={ALPHA}\n")
                rc, out, err = self.run_cli(CL.name, "install", "--plugin-dir", str(self.plugin), "--claude-config-dir",
                                            str(self.home / ".claude-sutando"), "--hmac-key-file",
                                            str(self.home / "task-hmac.key"), "--work", str(self.work))
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(cm.recorded_agent(single), ALPHA)
                for argv in (["verify", "--agent", ALPHA], ["attach", "--agent", ALPHA], ["uninstall", "--agent", ALPHA]):
                    with mock.patch.object(os, "execv") as execv:
                        rc, out, err = self.run_cli(CL.name, *argv)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertFalse(execv.called)
                    self.assertIn(f"two jobs serve {ALPHA}", err)
                    self.assertIn(f"--work {self.work}", err)
                    self.assertIn(f"--key {a.agent}", err)
                rc, out, err = self.run_cli(CL.name, "verify")
                self.assertIn(f"({cm.job_name(single)}; --work {self.work})", err)
                self.assertIn(f"({cm.job_name(a)}; --key {a.agent})", err)
                for selector, mine in ((["--work", str(self.work)], single), (["--key", a.agent], a)):
                    self.assertIn(f"PASS config: {cm.config_path(mine)}", self.run_cli(CL.name, "verify", *selector)[1])
                for selector in (["--work", str(self.work)], ["--key", a.agent]):
                    self.assertEqual(self.run_cli(CL.name, "uninstall", *selector)[0], 0)
                self.assertEqual(cm.installed_jobs(CL), [])

    def test_a_waiter_on_the_operation_lock_and_a_reinstall_are_never_two_owners(self):
        """uninstall removes the agent's operation lock while it holds it: a waiter blocked on it meanwhile takes
        the lock file that is there afterwards, so a reinstall waits for that waiter instead of running beside it."""
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                a = job(adapter, ALPHA)
                self.installed(adapter, ALPHA)
                path, real_open, real_remove = str(cm.oplock_path(a)), os.open, cm.remove_config_dir
                opened, held, done, threads = threading.Event(), threading.Event(), threading.Event(), []

                def waiter():
                    lock = cm.operation_lock(a)
                    lock.__enter__()
                    held.set()
                    done.wait(30)
                    lock.release()

                def watching(p, *args, **kw):
                    fd = real_open(p, *args, **kw)
                    if str(p) == path and threading.current_thread() is not threading.main_thread():
                        opened.set()
                    return fd

                def removing(shape):
                    threads.append(threading.Thread(target=waiter))
                    threads[-1].start()
                    self.assertTrue(opened.wait(10), "the waiter opened the lock uninstall holds")
                    return real_remove(shape)
                with mock.patch.object(os, "open", side_effect=watching), \
                        mock.patch.object(cm, "remove_config_dir", side_effect=removing), \
                        mock.patch.object(cm, "OP_WAIT_S", 30):
                    rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(held.wait(10), "the waiter gets the lock")
                self.assertTrue(os.path.exists(path), "on the file that is there now")
                with mock.patch.object(cm, "OP_WAIT_S", 0.3):
                    rc, out, err = self.install(adapter, ALPHA)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("operation is in progress", err)
                self.assertFalse(cm.config_path(a).exists(), "the reinstall did not run beside the waiter")
                done.set()
                threads[0].join(10)
                self.installed(adapter, ALPHA)
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", ALPHA)[0], 0)

    def test_a_fixed_job_under_sparrowd_is_recovered_by_its_declaration(self):
        """Under a host supervisor the fixed job's bridge is a declaration, not a unit: with the config removed,
        its declaration still names the agent and root, install keeps the job, uninstall removes the declaration,
        and the next install is the agent's own job."""
        saved = fx._dirs._injected
        self.addCleanup(setattr, fx._dirs, "_injected", saved)
        fx._dirs.set_dirs(state_dir=self.home / "ws-state", agents_dir=self.agents, bridge_host="sparrowd")
        os.environ["AGENT_MXID"] = ALPHA
        single, a = job(CL.name, ""), job(CL.name, ALPHA)
        decl = cb.bridge_home(self.root_of(ALPHA)) / cb.DECLARATION
        argv = ["install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                "--hmac-key-file", str(self.home / "task-hmac.key")]
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
                    self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
                self.assertTrue(decl.exists())
                self.assertFalse(cm.supervisor_path(cb.bridge_shape(single)).exists(), "no bridge unit")
                cm.config_path(single).unlink()
                self.assertEqual((cm.recorded_agent(single), cm.recorded_root(single)), (ALPHA, str(self.root_of(ALPHA))))
                self.assertIn(f"or remove this job: {claude_cli.COMMAND} uninstall --agent {ALPHA}",
                              self.run_cli(CL.name, "verify")[1])
                rc, out, err = self.run_cli(CL.name, *argv)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(cm.config_path(single).exists(), "install kept the fixed job")
                self.assertFalse(cm.config_dir(a).exists())
                cm.config_path(single).unlink()
                rc, out, err = self.run_cli(CL.name, "uninstall", "--agent", ALPHA)
                self.assertEqual(rc, 0, out + err)
                self.assertFalse(decl.exists(), "uninstall removed the fixed job's declaration")
                self.assertIn(f"removed: {decl}", out)
                self.assertFalse(cm.supervisor_path(single).exists())
                rc, out, err = self.run_cli(CL.name, *argv)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(cm.config_path(a).exists() and decl.exists(), "the next install is the agent's own")
                self.assertEqual(self.run_cli(CL.name, "uninstall", "--agent", ALPHA)[0], 0)


COMMAND_RE = re.compile(r"ag2-sparrow-(?:claude|codex)-local (?:verify|uninstall|attach|stop --confirm|install|debug-run)"
                        r"(?: --(?:agent|key|work) [^\s;,)]+| --single)?")
SELECTOR_RE = re.compile(r"--(?:agent|key|work) [^\s;)]+|--single")


def without_root_var(path: Path) -> None:
    """A unit as an earlier release wrote it: no ROOT_VAR, the marker kept."""
    text = path.read_text()
    if text.startswith("<?xml"):
        head, marker, rest = text.split("\n", 2)
        body = plistlib.loads((head + "\n" + rest).encode())
        body["EnvironmentVariables"].pop(cm.ROOT_VAR)
        dumped = plistlib.dumps(body, sort_keys=True).decode()
        first, tail = dumped.split("\n", 1)
        path.write_text(f"{first}\n{marker}\n{tail}")
    else:
        path.write_text("".join(l for l in text.splitlines(True) if cm.ROOT_VAR not in l))


class Round5(MultiBase):
    def sparrowd(self):
        saved = fx._dirs._injected
        self.addCleanup(setattr, fx._dirs, "_injected", saved)
        fx._dirs.set_dirs(state_dir=self.home / "ws-state", agents_dir=self.agents, bridge_host="sparrowd")
        return ["install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                "--hmac-key-file", str(self.home / "task-hmac.key")]

    def test_the_operation_lock_stays_put_so_a_fresh_entrant_after_cleanup_still_waits(self):
        """The lock is a stable file outside every dir uninstall removes: an entrant that arrives after uninstall's
        cleanup, while uninstall still holds the lock, waits instead of taking a second one."""
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                a = job(adapter, ALPHA)
                self.installed(adapter, ALPHA)
                path, real_remove, entered = cm.oplock_path(a), cm.remove_config_dir, []
                self.assertEqual(path.parent, cm.config_root() / cm.LOCKS_DIR)

                def removing(shape):
                    line = real_remove(shape)
                    with mock.patch.object(cm, "OP_WAIT_S", 0.3):
                        try:
                            with cm.operation_lock(a):
                                entered.append("second owner")
                        except cm.Busy:
                            entered.append("waited")
                    return line
                with mock.patch.object(cm, "remove_config_dir", side_effect=removing):
                    rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(entered, ["waited"], "the fresh entrant never owned the lock beside uninstall")
                self.assertFalse(cm.config_dir(a).exists())
                self.assertTrue(path.exists(), "the lock file is never removed")

    def test_a_config_less_leftover_holds_its_lock_through_the_run_dir_cleanup(self):
        """uninstall of an agent's own job with no recoverable root takes no agent lock; its operation lock still
        covers the run-dir cleanup, so a reinstall racing it is refused until uninstall is done."""
        a = job(CL.name, ALPHA)
        self.installed(CL.name, ALPHA)
        self.run_files(CL.name, a)
        cm.config_path(a).unlink()
        cm.supervisor_path(cb.bridge_shape(a)).unlink()
        self.assertEqual(cm.recovered_config(a), {})
        real, raced = claude_cli.launcher.remove_run_dir, []

        def cleanup(config_dir):
            with mock.patch.object(cm, "OP_WAIT_S", 0.3):
                raced.append(self.install(CL.name, ALPHA))
            return real(config_dir)
        with mock.patch.object(claude_cli.launcher, "remove_run_dir", side_effect=cleanup):
            rc, out, err = self.run_cli(CL.name, "uninstall", "--key", a.agent)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(raced[0][0], 1, raced)
        self.assertIn("operation is in progress", raced[0][2])
        self.assertFalse(cm.run_dir(cm.config_dir(a)).exists(), "the cleanup ran, and nothing raced it")
        self.assertFalse(cm.config_path(a).exists(), "the reinstall wrote nothing")

    def test_an_unbound_declaration_residue_is_never_adopted(self):
        """Fixed job B (a unit from before units recorded their root) lost its config and declaration; agent A's
        own job is reduced to its declaration. Nothing proves A's declaration is B's: neither is recovered, no
        command touches B's job or A's declaration, and the refusal names the manual path."""
        argv = self.sparrowd()
        single, a = job(CL.name, ""), job(CL.name, ALPHA)
        b_decl, a_decl = (cb.bridge_home(self.root_of(x)) / cb.DECLARATION for x in (BETA, ALPHA))
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                self.host.add(single, self.root_of(BETA))
                os.environ["AGENT_MXID"] = BETA
                with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
                    self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
                without_root_var(cm.supervisor_path(single))
                os.environ["AGENT_MXID"] = ALPHA
                self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
                gone = self.host.launchers[a.job]     # A's job is gone from the manager too
                gone.stop_session()
                gone.enabled = gone.registered = gone.loaded = False
                for path in (cm.config_path(a), cm.supervisor_path(a), cm.config_path(single), b_decl):
                    path.unlink()
                kept = (cm.supervisor_path(single).read_bytes(), self.launcher_pid(single), a_decl.read_bytes())
                self.assertEqual((cm.recovered_config(single), cm.recorded_agent(single)), ({}, ""))
                self.assertEqual(cm.recovered_config(a), {})
                for args in (["uninstall", "--agent", ALPHA], ["verify", "--agent", ALPHA], ["verify"],
                             ["uninstall", "--single"]):
                    rc, out, err = self.run_cli(CL.name, *args)
                    self.assertNotEqual(rc, 0, out + err)
                    self.assertEqual((cm.supervisor_path(single).read_bytes(), self.launcher_pid(single),
                                      a_decl.read_bytes()), kept, args)
                    self.assertIn(f"remove it by hand (rm {a_decl})", out + err, args)
                    self.assertNotIn(f"--agent {ALPHA}", out, "nothing advertises a command on A's behalf")
                a_decl.unlink()     # the manual path the refusal names; then the fixed job goes
                self.assertEqual(self.run_cli(CL.name, "uninstall", "--single")[0], 0)

    def run_files(self, adapter, shape):
        SideBySide.run_files(self, adapter, shape)

    def test_connects_next_line_names_the_job_it_connected(self):
        """With another agent installed, connect's closing `verify` line carries the new job's selector and,
        run verbatim, verifies that job."""
        self.installed(CL.name, BETA)
        backend = fx.Backend(answer={"relay_url": fx.RELAY, "bearer": fx.BEARER, "agent_id": ALPHA})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sm, "_run", side_effect=self.host), \
                mock.patch.object(urllib.request, "urlopen", side_effect=backend), \
                mock.patch.object(cm, "redeem_code", side_effect=functools.partial(cm.redeem_code, sleep=lambda s: None)), \
                mock.patch.object(sys, "stdin", io.StringIO(fx.CODE + "\n")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = claude_cli.main(["connect", "--base", fx.BASE, "--agent", ALPHA, "--code", "-", "--plugin-dir",
                                  str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                                  "--hmac-key-file", str(self.home / "task-hmac.key")])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        line = f"next: {claude_cli.COMMAND} verify --agent {ALPHA} "
        self.assertIn(line, out.getvalue())
        rc, vout, verr = self.run_cli(CL.name, "verify", "--agent", ALPHA)
        self.assertNotIn("pass the selector", verr)
        self.assertIn(f"PASS config: {cm.config_path(job(CL.name, ALPHA))}", vout)

    def jobs_and_commands(self, adapter):
        """Every job now and every command printed for it: verify's rows and attach line, list's selectors and
        the several-jobs refusal's, each as a verify and an uninstall."""
        base, self.verified = job(adapter, ""), {}
        jobs = cm.installed_jobs(base)
        listed = self.run_cli(adapter, "list")[1]
        refusal = self.run_cli(adapter, "verify")[2] if len(jobs) > 1 else ""
        printed = []
        for j in jobs:
            selector = cm.job_selector(j)
            self.assertTrue(selector, f"{cm.job_name(j)} has a selector")
            self.assertIn(f"({selector})", listed)
            if refusal:
                self.assertIn(f"({cm.job_name(j)}; {selector})", refusal)
            _rc, out, err = self.run_cli(adapter, "verify", *shlex.split(selector))
            self.verified[j] = out + err
            printed += [(c, j) for c in COMMAND_RE.findall(out + err)]
            for verb in ("verify", "uninstall"):
                printed.append((f"{CLIS[adapter].COMMAND} {verb} {selector}", j))
        with mock.patch.dict(os.environ):     # `run` by hand: the advice names each job's hand verb
            os.environ.pop(cm.MANAGED_VAR, None)
            hand = " ".join(self.run_cli(adapter, "run")[1:])
        for j in jobs:
            line = cm.job_command(j, CLIS[adapter].LOCAL.hand_verb)
            self.assertIn(line, hand)
            printed.append((line, j))
            if adapter == CL.name and cm.config_path(j).exists() and not cm.load_config(j)[1]:
                card = claude_cli.launcher.Launcher(cm.read_config(cm.config_path(j)), cm.config_dir(j)).attach_command()
                self.assertNotIn("--work", card, "the card's line leaves the host: no path")
                printed.append((card, j))
        return jobs, printed

    def snapshot(self, adapter):
        """Every job's unit bytes, launcher pid and installed set: what a command that selects nothing may not move."""
        return {j.job: (cm.supervisor_path(j).read_bytes() if cm.supervisor_path(j).exists() else None,
                        self.launcher_pid(j) if j.job in self.host.launchers else None)
                for j in cm.installed_jobs(job(adapter, ""))}

    def check_selects(self, adapter, text, mine):
        words = shlex.split(text)[1:]
        verb, selector = (words[:2], words[2:]) if words[0] == "stop" else (words[:1], words[1:])
        kw = {"agent_id": "", "work": "", "single": False, "key": ""}
        if selector:
            flag = selector[0][2:]
            kw[{"agent": "agent_id"}.get(flag, flag)] = True if flag == "single" else selector[1]
        got, why = cm.select_job(job(adapter, ""), **kw)
        self.assertEqual(got, mine, f"{text}: {why}")
        return verb, selector

    def test_every_printed_command_selects_exactly_the_job_that_printed_it(self):
        """Across the install states (one job; two agents' own; an explicit-root job beside the same agent's own;
        two config-less leftovers; a config-less fixed job), each command printed for a job, run verbatim,
        selects that job and no other."""
        def one(adapter):
            self.installed(adapter, ALPHA)

        def two(adapter):
            self.installed(adapter, ALPHA)
            self.installed(adapter, BETA)

        def keyed_and_fixed(adapter):
            self.host.add(job(adapter, ""), self.work)
            self.installed(adapter, ALPHA)
            self.relay.write_text(f"REMOTE_TASK_TOKEN={fx.TOKEN}\nAGENT_MXID={ALPHA}\n")
            self.assertEqual(self.run_cli(adapter, "install", "--plugin-dir", str(self.plugin), "--claude-config-dir",
                                          str(self.home / ".claude-sutando"), "--hmac-key-file",
                                          str(self.home / "task-hmac.key"), "--work", str(self.work))[0], 0)
            self.assertEqual(cm.recorded_agent(job(adapter, "")), ALPHA, "both jobs serve one agent")

        def two_leftovers(adapter):
            two(adapter)
            for agent in (ALPHA, BETA):
                cm.config_path(job(adapter, agent)).unlink()
                cm.supervisor_path(cb.bridge_shape(job(adapter, agent))).unlink()

        def fixed_leftover(adapter):
            self.host.add(job(adapter, ""), self.work)
            self.install_work(adapter)
            cm.config_path(job(adapter, "")).unlink()

        def corrupt(adapter):
            two(adapter)
            conf = cm.config_path(job(adapter, BETA))
            conf.write_text(conf.read_text() + "ROOT='unclosed\n")

        states = [(name, fn, adapter) for name, fn in (("one", one), ("two", two), ("two leftovers", two_leftovers),
                                                       ("fixed leftover", fixed_leftover), ("beta's config corrupt", corrupt))
                  for adapter in CLIS]
        states.append(("keyed and fixed, one agent", keyed_and_fixed, CL.name))
        for name, build, adapter in states:
            with self.subTest(state=name, adapter=adapter):
                build(adapter)
                jobs, printed = self.jobs_and_commands(adapter)
                self.assertTrue(jobs)
                if adapter == CL.name and name in ("one", "two", "keyed and fixed, one agent"):
                    self.assertTrue(any(" attach" in c for c, _ in printed), "verify printed an attach line")
                if name in ("two leftovers", "fixed leftover"):
                    for j in jobs:
                        self.assertIn(f"or remove this job: {cm.uninstall_command(j)}", self.verified[j])
                if name == "beta's config corrupt":
                    install = cm.job_command(job(adapter, BETA), "install")
                    self.assertIn(install, self.verified[job(adapter, BETA)], "the repair line names beta's job")
                    printed.append((install, job(adapter, BETA)))
                uninstalls = {}
                for text, mine in printed:
                    verb, selector = self.check_selects(adapter, text, mine)
                    if verb == ["uninstall"]:
                        uninstalls.setdefault(mine, text)
                        continue
                    before = self.snapshot(adapter)
                    with mock.patch.object(os, "execv") as execv:
                        rc, out, err = self.run_cli(adapter, *verb, *selector)
                    self.assertNotIn("pass the selector", err, text)
                    if verb == ["attach"] and execv.called:
                        self.assertIn(str(claude_cli.launcher.socket_path(cm.config_dir(mine))), execv.call_args[0][1])
                    elif verb == ["attach"]:     # a job left without its config: refused on that job's own config
                        self.assertIn(str(cm.config_path(mine)), err, text)
                    elif verb == ["verify"]:
                        self.assertIn(str(cm.config_path(mine)), out, text)
                    elif verb == ["install"]:
                        after = self.snapshot(adapter)
                        self.assertEqual({k: v for k, v in after.items() if k != mine.job},
                                         {k: v for k, v in before.items() if k != mine.job}, f"{text} moved another job")
                for mine, text in uninstalls.items():
                    others = [j for j in cm.installed_jobs(job(adapter, "")) if j != mine]
                    before = self.snapshot(adapter)
                    rc, out, err = self.run_cli(adapter, *shlex.split(text)[1:])
                    left = cm.installed_jobs(job(adapter, ""))
                    if rc != 0:     # refused (a config it must not guess past): it moved nothing
                        self.assertEqual(self.snapshot(adapter), before, f"{text}: {out}{err}")
                        self.assertNotIn("pass the selector", err)
                        cm.config_path(mine).unlink()
                        rc, out, err = self.run_cli(adapter, *shlex.split(text)[1:])
                        left = cm.installed_jobs(job(adapter, ""))
                    self.assertEqual(rc, 0, f"{text}: {out}{err}")
                    self.assertNotIn(mine, left, text)
                    self.assertEqual(left, others, f"{text} removed only its own job")
                self.relay.write_text(f"REMOTE_TASK_TOKEN={fx.TOKEN}\nAGENT_MXID={fx.AGENT}\n")


PKG_SRC = Path(__file__).resolve().parents[1] / "ag2_sparrow"
# A string that names one of the adapters' commands followed by a verb is a command someone can copy; every such
# line must come from cm.job_command. These are the exceptions, each a provenance mark or an internal argv.
EMITTER_RE = re.compile(r"(\{[^{}]*command[^{}]*\}|\{COMMAND\}|%s|(?:command|COMMAND)\s*\+\s*[\"']\s*|"
                        r"ag2-sparrow-(?:claude|codex)-local) (?:install|uninstall|verify|attach|stop|debug-run|run|"
                        r"connect|list|hold|inbox)\b")
EMITTERS_ALLOWED = {
    ("connect_common.py", 'return f"# Written by {self.writer or self.command} install;'): "the unit's provenance marker",
    ("connect_common.py", 'return f"<!-- Written by {self.writer or self.command} install;'): "the plist's marker",
    ("connect_common.py", 'written_by=f"{shape.command} install")'): "the declaration's provenance",
    ("connect_common.py", 'return (f"{shape.command} run is the manager\'s entrypoint'): "names the refused verb",
    ("connect_bridge.py", 'as {command} connect writes it"'): "names the seat's writer",
    ("connect_bridge.py", 'path, written_by=f"{command} install", host=mode'): "the declaration's provenance",
    ("connect_bridge.py", 'remove_bridge_declaration(path, written_by=f"{command} install")'): "the declaration's provenance",
    ("adapters/claude_code/hold.py", '"""`ag2-sparrow-claude-local hold --generation'): "docstring",
    ("adapters/claude_code/hold.py", 'return "%s hold --generation=%s --" % (command, nonce)'): "the pane's own argv",
    ("adapters/claude_code/inbox.py", '"""`ag2-sparrow-claude-local inbox <verb>`'): "docstring",
    ("adapters/claude_code/launcher.py", "START_COMMAND_RE = re.compile(r'^\"?ag2-sparrow-claude-local hold"): "argv check",
}


@expand
class Round6(MultiBase):
    sparrowd = Round5.sparrowd

    def test_no_command_line_is_formatted_outside_the_one_formatter(self):
        """Every command a line can print comes from cm.job_command (so it carries the job's selector): no other
        string in the adapters or the shared modules formats one, beyond the provenance marks listed."""
        files = [PKG_SRC / "connect_common.py", PKG_SRC / "connect_bridge.py", PKG_SRC / "adapters/codex/connect_cli.py",
                 *sorted((PKG_SRC / "adapters/claude_code").glob("*.py"))]
        found = set()
        for path in files:
            rel = str(path.relative_to(PKG_SRC))
            for line in path.read_text().splitlines():
                if EMITTER_RE.search(line) and not line.strip().startswith("#"):
                    match = [k for k in EMITTERS_ALLOWED if k[0] == rel and k[1] in line]
                    self.assertTrue(match, f"{rel}: a command formatted outside cm.job_command: {line.strip()}")
                    found.update(match)
        self.assertEqual(found, set(EMITTERS_ALLOWED), "every exception is still there (drop the stale ones)")

    def tamper_bridge_agent(self, shape, agent):
        """The job's own marked bridge unit, its agent changed: the marker stays, the identity no longer holds."""
        unit = cm.supervisor_path(cb.bridge_shape(shape))
        text = unit.read_text()
        old = cm.unit_environ(text)["AGENT_MXID"]
        unit.write_text(text.replace(old, agent))

    def _misbound(self, adapter):
        """A fixed job's bridge unit bound to B's root but naming agent A (as the bridge runtime would refuse):
        nothing is recovered, `--agent A` never reaches the fixed job, and its own uninstall refuses with the
        manual path instead of tearing down a bridge it cannot prove its own."""
        single = job(adapter, "")
        self.host.add(single, self.root_of(BETA))
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.installed(adapter, BETA)
        self.tamper_bridge_agent(single, ALPHA)
        cm.config_path(single).unlink()
        kept = (cm.supervisor_path(single).read_bytes(), cm.supervisor_path(cb.bridge_shape(single)).read_bytes(),
                self.launcher_pid(single))
        self.assertEqual((cm.recovered_config(single), cm.recorded_agent(single)), ({}, ""))
        rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), cm.supervisor_path(cb.bridge_shape(single)).read_bytes(),
                          self.launcher_pid(single)), kept, "--agent A never reaches B's job")
        rc, out, err = self.run_cli(adapter, "uninstall", "--single")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"rm {cm.supervisor_path(cb.bridge_shape(single))}", err)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), cm.supervisor_path(cb.bridge_shape(single)).read_bytes(),
                          self.launcher_pid(single)), kept, "and its own uninstall refuses before it moves anything")

    test_a_bridge_unit_naming_another_agent_than_its_root_is_never_recovered = per_manager(_misbound)

    def test_a_declaration_naming_another_agent_than_its_root_is_never_recovered(self):
        """Under sparrowd, the fixed job's declaration beside its own root B, edited to name agent A: the same
        identity rule as the bridge runtime refuses it, so nothing is recovered, `--agent A` never reaches B's job,
        and B's uninstall refuses naming the declaration's manual path."""
        argv = self.sparrowd()
        single = job(CL.name, "")
        decl = cb.bridge_home(self.root_of(BETA)) / cb.DECLARATION
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                self.host.add(single, self.root_of(BETA))
                os.environ["AGENT_MXID"] = BETA
                with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
                    self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
                body = json.loads(decl.read_text())
                body["env"]["AGENT_MXID"] = ALPHA
                decl.write_text(json.dumps(body))
                cm.config_path(single).unlink()
                kept = (cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single))
                self.assertEqual((cm.recovered_config(single), cm.recorded_agent(single)), ({}, ""))
                self.run_cli(CL.name, "uninstall", "--agent", ALPHA)
                rc, out, err = self.run_cli(CL.name, "uninstall", "--single")
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(f"rm {decl}", err)
                self.assertEqual((cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single)),
                                 kept)
                decl.unlink()
                self.assertEqual(self.run_cli(CL.name, "uninstall", "--single")[0], 0)

    def test_a_declaration_serving_another_root_is_never_recovered(self):
        """The fixed job's declaration beside its own root B, edited to feed another root's tasks: it serves not the
        root the job's unit records, so nothing is recovered and B's uninstall refuses naming it."""
        argv = self.sparrowd()
        single = job(CL.name, "")
        decl = cb.bridge_home(self.root_of(BETA)) / cb.DECLARATION
        self.host.add(single, self.root_of(BETA))
        os.environ["AGENT_MXID"] = BETA
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
        body = json.loads(decl.read_text())
        body["env"]["AGENT_CONNECT_TASK_DIR"] = str(self.root_of(ALPHA) / "tasks")
        decl.write_text(json.dumps(body))
        cm.config_path(single).unlink()
        kept = (cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single))
        self.assertEqual(cm.recovered_config(single), {})
        rc, out, err = self.run_cli(CL.name, "uninstall", "--single")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"rm {decl}", err)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single)), kept)

    def test_an_unreadable_or_unbound_declaration_fails_closed_and_names_its_manual_path(self):
        """An agent's own job reduced to an unreadable declaration, and a legacy fixed job whose declaration its
        unit cannot vouch for: uninstall refuses (non-zero), names the declaration and its `rm`, and moves nothing."""
        argv = self.sparrowd()
        a, single = job(CL.name, ALPHA), job(CL.name, "")
        a_decl, b_decl = (cb.bridge_home(self.root_of(x)) / cb.DECLARATION for x in (ALPHA, BETA))
        os.environ["AGENT_MXID"] = ALPHA
        self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
        gone = self.host.launchers[a.job]
        gone.stop_session()
        gone.enabled = gone.registered = gone.loaded = False
        for path in (cm.config_path(a), cm.supervisor_path(a)):
            path.unlink()
        a_decl.chmod(0)
        self.addCleanup(lambda: a_decl.exists() and a_decl.chmod(0o644))
        rc, out, err = self.run_cli(CL.name, "uninstall", "--key", a.agent)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"the bridge declaration at {a_decl} cannot be used", err)
        self.assertIn(f"rm {a_decl}", err)
        self.assertTrue(os.path.lexists(a_decl))
        a_decl.chmod(0o644)
        a_decl.unlink()
        self.host.add(single, self.root_of(BETA))
        os.environ["AGENT_MXID"] = BETA
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
        without_root_var(cm.supervisor_path(single))
        cm.config_path(single).unlink()
        kept = (cm.supervisor_path(single).read_bytes(), b_decl.read_bytes(), self.launcher_pid(single))
        rc, out, err = self.run_cli(CL.name, "uninstall", "--single")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"rm {b_decl}", err)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), b_decl.read_bytes(), self.launcher_pid(single)), kept,
                         "the legacy fixed job's unit is not removed while its declaration is left unnamed")
        b_decl.unlink()
        self.assertEqual(self.run_cli(CL.name, "uninstall", "--single")[0], 0)


class Selection(MultiBase):
    def test_a_job_whose_config_records_an_agent_of_another_key_is_never_acted_on(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                a = job(adapter, ALPHA)
                cm._write(cm.config_path(a), cm.render_config(a, {"AGENT_ID": BETA, "ROOT": str(self.root_of(BETA)),
                                                                  "CODEX": str(self.codex), "CLAUDE": str(self.claude),
                                                                  "SESSION_ID": fx.SESSION_ID}), 0o600)
                before = cm.config_path(a).read_bytes()
                for argv in (["verify"], ["uninstall"], ["verify", "--work", str(self.root_of(BETA))]):
                    rc, out, err = self.run_cli(adapter, *argv)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"records {BETA}, whose job is not {cm.job_name(a)}", err)
                with mock.patch.dict(os.environ, {cm.AGENT_VAR: a.agent}):
                    rc, out, err = self.run_cli(adapter, "run")
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn("whose job is not", err)
                self.assertEqual(cm.config_path(a).read_bytes(), before)
                self.assertEqual(self.host.calls_naming(a), [])
                cm.config_path(a).unlink()

    def test_a_64_character_key_keeps_the_tmux_socket_short(self):
        """The socket of an agent's own job sits in a short run dir named by its key's digest, so a key as long
        as a root segment gets fits the platform's socket path limit; a single job keeps its socket in place."""
        socks = []
        for agent in ("@" + "a" * 30 + ":" + "b" * 33, "@" + "a" * 30 + ":" + "b" * 32 + "c"):
            shape = job(CL.name, agent)
            self.assertEqual(len(shape.agent), 64)
            sock = claude_cli.launcher.socket_path(cm.config_dir(shape))
            self.assertEqual(sock.parent.parent, cm.config_root() / cm.RUN_DIR)
            self.assertLessEqual(len(str(sock)) - len(str(self.home)), 64, sock)
            socks.append(sock)
        self.assertNotEqual(*socks)
        self.assertEqual(claude_cli.launcher.socket_path(cm.config_dir(CL)), cm.config_dir(CL) / "claude-local.sock")
        self.assertEqual(claude_cli.launcher.run_dir_problem(cm.config_dir(job(CL.name, ALPHA))), "")
        self.assertEqual(oct(os.stat(cm.run_dir(cm.config_dir(job(CL.name, ALPHA)))).st_mode & 0o777), "0o700")

    def test_another_job_of_this_adapter_whose_config_names_the_agent_refuses_its_install(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                other = job(adapter, BETA)     # a config moved by hand under another agent's key
                cm._write(cm.config_path(other), cm.render_config(other, {
                    "AGENT_ID": ALPHA, "BRIDGE": "sparrowd", "ROOT": str(self.root_of(ALPHA))}), 0o600)
                rc, out, err = self.install(adapter, ALPHA)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(f"{ALPHA} already has a bridge, run by {adapter} (declared for sparrowd)", err)
                self.assertFalse(cm.config_path(job(adapter, ALPHA)).exists())
                cm.config_path(other).unlink()

    def test_the_colliding_pairs_get_two_keys_and_a_digest_key_is_never_a_literal_one(self):
        for first, second in PAIRS:
            self.assertEqual(dash_key(first), dash_key(second), "the pair the dash form gave one key")
            self.assertNotEqual(cm.agent_key(first), cm.agent_key(second))
        self.assertEqual(cm.agent_key(PAIRS[0][0]), "ab_cdefghijklmnopqrstuvwxyz.example")
        digest_form = re.compile(r"__[0-9a-f]{10}$")
        ids = [f"@{loc}:{srv}" for loc in ("a", "ab", "x-y", "a.b", "q" * 20, "A", "Mixed", "a_b", "Q" * 20)
               for srv in ("s", "ab.cd", "x-1.example", "y" * 20, "S.Y", "z" * 60)]
        for agent in ids:
            segment, key = cm.agent_identity.root_segment(agent), cm.agent_key(agent)
            literal = key == segment
            self.assertEqual(literal, not digest_form.search(key), f"{agent} -> {key}")
            self.assertTrue("__" not in segment or re.search(r"__[0-9a-f]{12}$", segment),
                            "only root_segment's own digest form holds the separator, followed by 12 hex")
        self.assertEqual(len({cm.agent_key(a) for a in ids}), len(set(ids)), "distinct ids, distinct keys")

    def test_each_colliding_pair_installs_verifies_and_uninstalls_side_by_side(self):
        for (first, second), adapter in zip(PAIRS, CLIS):
            with self.subTest(pair=first, adapter=adapter):
                for agent in (first, second):
                    self.token_for(agent)
                    self.host.add(job(adapter, agent), self.agents / cm.agent_identity.root_segment(agent))
                    self.installed(adapter, agent)
                for agent in (first, second):
                    self.assertIn(f"PASS config: {cm.config_path(job(adapter, agent))}",
                                  self.run_cli(adapter, "verify", "--agent", agent)[1])
                kept = (self.files(job(adapter, second)), self.launcher_pid(job(adapter, second)))
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", first)[0], 0)
                self.assertFalse(cm.config_path(job(adapter, first)).exists())
                self.assertEqual((self.files(job(adapter, second)), self.launcher_pid(job(adapter, second))), kept)
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", second)[0], 0)

    def test_a_job_whose_config_records_another_agent_is_never_acted_on(self):
        """Should two agents ever share a key, a verb for one refuses the job the other installed."""
        with mock.patch.object(cm, "agent_key", dash_key):
            for (first, second), adapter in [(p, a) for p in PAIRS for a in CLIS]:
                for agent in (first, second):
                    self.token_for(agent)
                shared = job(adapter, first)
                self.host.add(shared, self.agents / cm.agent_identity.root_segment(first))
                with self.subTest(adapter=adapter):
                    self.installed(adapter, first)
                    files, pid, calls = self.files(shared), self.launcher_pid(shared), len(self.host.calls_naming(shared))
                    for argv in (["verify", "--agent", second], ["uninstall", "--agent", second]):
                        rc, out, err = self.run_cli(adapter, *argv)
                        self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                        self.assertIn(f"records {first}, not {second}", err)
                    rc, out, err = self.install(adapter, second)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"records {first}, not {second}", err)
                    self.assertEqual((self.files(shared), self.launcher_pid(shared)), (files, pid), "the first is untouched")
                    self.assertEqual(len(self.host.calls_naming(shared)), calls)
                    self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", first)[0], 0)

    def test_install_agent_must_be_the_token_files(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                rc, out, err = self.install(adapter, ALPHA, "--agent", BETA)
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn(f"--agent {BETA} is not the agent this install serves ({ALPHA})", err)
                self.assertFalse(cm.config_root().joinpath(cm.AGENTS_DIR).exists())

    def test_install_key_and_single_must_name_the_job_its_token_makes(self):
        """A printed repair line `install --key K` or `install --single` never installs over another job: run with
        a token for another agent, install refuses and writes nothing."""
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                for extra in (["--key", job(adapter, BETA).agent], ["--single"]):
                    rc, out, err = self.install(adapter, ALPHA, *extra)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn("nothing was changed", err)
                    self.assertEqual(cm.installed_jobs(job(adapter, "")), [])
                rc, out, err = self.install(adapter, ALPHA, "--key", job(adapter, ALPHA).agent)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", ALPHA)[0], 0)

    def test_with_none_installed_the_agentless_form_is_the_single_agent_job(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                rc, out, err = self.run_cli(adapter, "uninstall")
                self.assertEqual(rc, 0, err)
                self.assertIn(f"nothing to remove: {adapter} is not installed", out)
                rc, out, err = self.run_cli(adapter, "list")
                self.assertIn("no agent is installed", out)

    def test_run_serves_the_agent_its_unit_names(self):
        configs = {}
        for agent in (ALPHA, BETA):
            for adapter in CLIS:
                shape = job(adapter, agent)
                cm._write(cm.config_path(shape), cm.render_config(shape, {"AGENT_ID": agent, "ROOT": str(self.root_of(agent)),
                                                                         "CODEX": str(self.codex), "CLAUDE": str(self.claude),
                                                                         "SESSION_ID": fx.SESSION_ID}), 0o600)
                configs[shape.job] = shape
        seen = []
        held = lambda cfg, root, *a, **kw: seen.append(("codex", root)) or 0  # noqa: E731
        launcher = mock.MagicMock(side_effect=lambda cfg, config_dir: seen.append(("claude", cfg["ROOT"], config_dir))
                                  or mock.MagicMock(serve=lambda: 0))
        with mock.patch.object(codex_cli, "_held", side_effect=held), \
                mock.patch.object(codex_cli, "version_problem", return_value=""), \
                mock.patch.object(claude_cli, "version_problem", return_value=""), \
                mock.patch.object(claude_cli.launcher, "Launcher", launcher), \
                mock.patch.object(cm, "load_config", side_effect=lambda shape: (cm.read_config(cm.config_path(shape)), "")):
            for agent in (BETA, ALPHA):
                os.environ[cm.AGENT_VAR] = cm.agent_key(agent)
                self.assertEqual(self.run_cli(CX.name, "run")[0], 0)
                self.assertEqual(self.run_cli(CL.name, "run")[0], 0)
            os.environ[cm.AGENT_VAR] = "../escape"
            for adapter in CLIS:
                rc, out, err = self.run_cli(adapter, "run")
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn("is not an agent key", err)
        del os.environ[cm.AGENT_VAR]
        self.assertEqual(seen, [("codex", str(self.root_of(BETA))),
                                ("claude", str(self.root_of(BETA)), cm.config_dir(job(CL.name, BETA))),
                                ("codex", str(self.root_of(ALPHA))),
                                ("claude", str(self.root_of(ALPHA)), cm.config_dir(job(CL.name, ALPHA)))])

    def test_the_agent_key(self):
        long_id = "@" + "x" * 40 + ":" + "y" * 40 + ".example"
        cases = {ALPHA: "alpha.agent_ag2.space", "@support-cc.agent:s.space": "support-cc.agent_s.space",
                 "@support-cc-2.agent:s.space": "support-cc-2.agent_s.space",
                 "@" + "a" * 30 + ":" + "b" * 33: "a" * 30 + "_" + "b" * 33,
                 "@Mixed:Case.Server": None, long_id: None}
        keys = set()
        for agent, want in cases.items():
            with self.subTest(agent=agent):
                key = cm.agent_key(agent)
                self.assertEqual(key, cm.agent_key(agent), "stable")
                self.assertTrue(cm.AGENT_KEY_RE.fullmatch(key), key)
                self.assertLessEqual(len(key), cm.AGENT_KEY_MAX)
                self.assertEqual(key, cm.agent_identity.root_segment(agent), "the key is the agent's root segment")
                if want:
                    self.assertEqual(key, want)
                shape = cm.for_key(CL, key)
                self.assertTrue(sm.UNIT_RE.fullmatch(shape.service_name), shape.service_name)
                self.assertTrue(sm.UNIT_RE.fullmatch(cb.bridge_shape(shape).service_name))
                self.assertEqual(shape.agent_label, f"ai.ag2.sparrow.claude-local.{key}")
                keys.add(key)
        self.assertEqual(len(keys), len(cases), "distinct agents, distinct keys")
        self.assertNotEqual(cm.agent_key(long_id), cm.agent_key(long_id.replace("y", "z")))
        self.assertFalse(sm.UNIT_RE.fullmatch("ag2-sparrow-claude-local.--x.service"))
        self.assertFalse(sm.UNIT_RE.fullmatch("ag2-sparrow-claude-local.a b.service"))


if __name__ == "__main__":
    unittest.main()
