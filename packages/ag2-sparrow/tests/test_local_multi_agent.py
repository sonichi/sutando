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
import importlib
import re
import io
import os
import subprocess
import sys
import unittest
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
                which = "" if single else f" --agent {ALPHA}"
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
                self.assertIn(str(cm.config_dir(job(CL.name, BETA))), " ".join(execv.call_args[0][1]))
                self.assertEqual(cm.command_for(CL, {}, "stop --confirm"), f"{claude_cli.COMMAND} stop --confirm --single")
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
    if cm.AGENT_KEY_RE.fullmatch(segment):
        return segment
    return f"{segment[:13].rstrip('._-')}-{cm.hashlib.sha256(agent_id.encode()).hexdigest()[:10]}"


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
        self.assertEqual(cm.agent_key(PAIRS[0][0]), "ab_cdefghijk__99dc128207")
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
                 "@Mixed:Case.Server": None, long_id: None}
        keys = set()
        for agent, want in cases.items():
            with self.subTest(agent=agent):
                key = cm.agent_key(agent)
                self.assertEqual(key, cm.agent_key(agent), "stable")
                self.assertTrue(cm.AGENT_KEY_RE.fullmatch(key), key)
                self.assertLessEqual(len(key), cm.AGENT_KEY_MAX)
                if want:
                    self.assertEqual(key, want, "a short plain id keeps its root segment")
                else:
                    self.assertRegex(key, r"__[0-9a-f]{10}$", "anything else carries a digest of the id")
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
