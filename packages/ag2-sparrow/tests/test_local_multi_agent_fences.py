#!/usr/bin/env python3
"""Several agents per host, continued: the per-agent fence held through settle, records proven before reuse, no
bridge left without its session, and shell-safe manual commands."""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_local_multi_agent as lm  # noqa: E402
from test_local_multi_agent import (  # noqa: E402
    ALPHA, BETA, CL, CX, MultiBase, cb, claude_cli, cm, expand, fx, job, per_manager, sm, without_root_var,
)


@expand
class Round8(MultiBase):
    def interleaved(self, target, name, actor):
        """`target` (a module attribute) wrapped: its first call first runs `actor()` in another thread, with the fence
        and the agent lock waited for not at all, and returns what that thread returned; then the real call."""
        real, got, fired = getattr(target, name), [], []

        def first(*a, **kw):
            if not fired:
                fired.append(True)
                with mock.patch.object(cm, "FENCE_WAIT_S", 0), mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0):
                    t = threading.Thread(target=lambda: got.append(actor()), daemon=True)
                    t.start()
                    t.join(120)
                if t.is_alive():
                    got.append((None, "", "the interleaved operation never returned"))
            return real(*a, **kw)
        return mock.patch.object(target, name, side_effect=first), got

    def rogue(self):
        marker = self.home / "rogue-ran"
        path = self.bin / "rogue"
        path.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\necho 'claude 99.0.0 (Claude Code)'\n")
        path.chmod(0o755)
        return path, marker

    def _rogue_never_runs(self, adapter):
        """A repair `install <selector>` reads nothing recorded for use, and runs no recorded executable, until the
        record is proven the job's on its own root: a config naming another root, one install did not write, or
        one naming another agent is refused with nothing run; the same config on its own root lends its executable."""
        b = job(adapter, BETA)
        self.installed(adapter, ALPHA)
        self.installed(adapter, BETA)
        conf, exe = cm.config_path(b), "CLAUDE" if adapter == CL.name else "CODEX"
        good = conf.read_text()
        rogue, marker = self.rogue()
        for name, text in (("another root", good.replace(f"ROOT={self.root_of(BETA)}", f"ROOT={self.root_of(ALPHA)}")),
                           ("not install's", "\n".join(good.splitlines()[2:]) + "\n"),
                           ("another agent", good.replace(f"AGENT_ID={BETA}", f"AGENT_ID={ALPHA}"))):
            with self.subTest(record=name):
                conf.write_text(re.sub(rf"(?m)^{exe}=.*$", f"{exe}={shlex.quote(str(rogue))}", text))
                before = self.files(job(adapter, ALPHA))
                selector = ["--key", b.agent] if name == "another agent" else ["--agent", BETA]
                rc, out, err = self.run_cli(adapter, "install", *selector)
                self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                if name != "another agent":     # that one the selector itself refuses (key_problem)
                    self.assertIn("nothing it records was used or run", err)
                self.assertFalse(marker.exists(), "the recorded executable never ran")
                self.assertEqual(self.files(job(adapter, ALPHA)), before)
        conf.write_text(re.sub(rf"(?m)^{exe}=.*$", f"{exe}={shlex.quote(str(rogue))}", good))
        self.run_cli(adapter, "install", "--agent", BETA)
        self.assertTrue(marker.exists(), "the control: a proven record's executable is the one install checks")
    test_a_recorded_executable_never_runs_before_the_record_is_proven = per_manager(_rogue_never_runs)

    def _settle_is_fenced(self, adapter):
        """An install holds the agent's fence through its settle: while its session start is being settled, another
        install, the other adapter's install and an uninstall of the agent are refused (Busy), so none can land
        between its wiring and its deferred bridge work; afterwards each runs, and the bridge it wired stays."""
        a = job(adapter, ALPHA)
        other = CX.name if adapter == CL.name else CL.name
        self.installed(adapter, ALPHA)
        runner = self.host.launchers[a.job]
        runner.stop_session()
        runner.root = None
        actors = [lambda: self.install(adapter, ALPHA), lambda: self.install(other, ALPHA),
                  lambda: self.run_cli(adapter, "uninstall", "--agent", ALPHA)]
        for actor in actors:
            with self.subTest(actor=actors.index(actor)):
                patch, got = self.interleaved(cm, "_fresh_holder", actor)
                with patch:
                    rc, out, err = self.install(adapter, ALPHA)
                self.assertEqual(rc, 1, out + err)
                self.assertNotEqual(got[0][0], 0, got[0])
                self.assertIn("another operation on this agent is in progress (fence", got[0][2])
        runner.root = str(self.root_of(ALPHA))
        self.installed(adapter, ALPHA)
        self.assertTrue(self.host.bridges[a.job].active and cm.supervisor_path(cb.bridge_shape(a)).exists())
    test_no_operation_lands_inside_an_installs_settle = per_manager(_settle_is_fenced)

    def _declaration_is_fenced(self, adapter):
        """Under sparrowd, an uninstall during an install's settle is refused, so the declaration the install
        writes once its session holds the root never outlives an uninstall that completed first."""
        argv = lm.Round5.sparrowd(self) if adapter == CL.name else \
            ["install", "--codex", str(self.codex), "--codex-home", str(self.codex_home)]
        lm.Round5.sparrowd(self)
        root = self.root_of(ALPHA)
        self.assertEqual(cb._sources.write_seat_credentials(
            cb.bridge_home(root), agent_id=ALPHA, instance="c" * 32, tier="owner",
            relay_url="https://relay.backend.example/relay", bearer="bearer-SEAT0001"), "")
        argv += ["--token-env-file", cb.seat_file(root)]
        decl = cb.bridge_home(root) / cb.DECLARATION
        patch, got = self.interleaved(cm, "_fresh_holder", lambda: self.run_cli(adapter, "uninstall", "--agent", ALPHA))
        with patch:
            rc, out, err = self.run_cli(adapter, *argv)
        self.assertEqual(rc, 0, out + err)
        self.assertNotEqual(got[0][0], 0, got[0])
        self.assertTrue(decl.exists(), f"{got[0]} {out} {err}")
        self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", ALPHA)[0], 0)
        self.assertFalse(decl.exists(), "the uninstall that ran last is the state left")
        self.assertEqual(self.run_cli(adapter, *argv)[0], 0)
        a = job(adapter, ALPHA)
        with mock.patch.object(cm, "_await_free", return_value="the root is still held (test)"):
            rc, out, err = self.run_cli(adapter, *argv)
        self.assertNotEqual(rc, 0, out + err)
        self.assertFalse(decl.exists(), "a failed reinstall leaves no declared bridge without its session")
        self.assertFalse(self.host.launchers[a.job].alive())
    test_a_declaration_never_outlives_an_uninstall_or_its_session = per_manager(_declaration_is_fenced)

    def _no_bridge_without_session(self, adapter):
        """A reinstall that fails after it stopped the session (the root never frees; the manager refuses the
        start) stops the agent's running bridge too, so no bridge runs without its session; the repair restores
        both."""
        a = job(adapter, ALPHA)
        bridge, runner = self.host.bridges[a.job], self.host.launchers[a.job]
        starts = {("systemctl", "restart"): 1, ("launchctl", "bootstrap"): 1, ("launchctl", "kickstart"): 1}
        for fault in ("the root never frees", "the start is refused", "refused before anything moved"):
            with self.subTest(fault=fault):
                self.installed(adapter, ALPHA)
                self.assertTrue(bridge.active and runner.alive())
                with contextlib.ExitStack() as stack:
                    if fault == "the root never frees":
                        stack.enter_context(mock.patch.object(cm, "_await_free", return_value="still held (test)"))
                    elif fault == "the start is refused":
                        runner.table.update(starts)
                    else:
                        stack.enter_context(mock.patch.object(cm, "render_supervisor", return_value=("", "no unit (test)")))
                    rc, out, err = self.install(adapter, ALPHA)
                runner.table.clear()
                self.assertNotEqual(rc, 0, out + err)
                if fault == "refused before anything moved":     # its session still runs, so its bridge is left running
                    self.assertTrue(bridge.active and runner.alive(), err)
                    continue
                self.assertFalse(runner.alive())
                self.assertFalse(bridge.active, err)
                self.assertIn("since its session is not running", err)
                rc, out, err = self.run_cli(adapter, *shlex.split(cm.job_command(a, "install"))[1:])
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(bridge.active and runner.alive())
    test_a_failed_reinstall_leaves_no_bridge_without_its_session = per_manager(_no_bridge_without_session)

    def test_a_configless_uninstall_holds_its_agents_lock_against_the_other_adapter(self):
        """An agent's own job left with no recoverable root (a unit from before units recorded it, no config, no
        bridge unit): its uninstall holds the agent's fence and lock, from its own key, so the other adapter's
        install of the same agent, arriving mid-uninstall, is refused rather than having its new files removed;
        once the uninstall ends it installs and keeps them."""
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                b = job(CL.name, BETA)
                self.installed(CL.name, BETA)
                gone = self.host.launchers[b.job]
                for path in (cm.config_path(b), cm.supervisor_path(cb.bridge_shape(b))):
                    path.unlink()
                without_root_var(cm.supervisor_path(b))
                self.assertEqual(cm.recovered_config(b), {})
                real = cm.stop_supervisor
                arrived, fired = [], []

                def stopping(shape):
                    if shape == b and not fired:
                        fired.append(True)
                        with mock.patch.object(cm, "FENCE_WAIT_S", 0), mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0):
                            t = threading.Thread(target=lambda: arrived.append(self.install(CX.name, BETA)), daemon=True)
                            t.start()
                            t.join(120)
                        if t.is_alive():
                            arrived.append((None, "", "the interleaved install never returned"))
                        fd, why = cm.launch_admission(cb.agent_root(BETA)[0], wait_s=0)
                        cm.end_admission(fd)
                        arrived.append(why)
                    return real(shape)
                with mock.patch.object(cm, "stop_supervisor", side_effect=stopping):
                    rc, out, err = self.run_cli(CL.name, "uninstall", "--key", b.agent)
                self.assertEqual(rc, 0, out + err)
                self.assertNotEqual(arrived[0][0], 0, arrived[0])
                self.assertIn("another operation on this agent is in progress", arrived[0][2])
                self.assertTrue(arrived[1], "and the agent's lock is held: no launcher is admitted mid-uninstall")
                gone.stop_session()
                self.installed(CX.name, BETA)
                self.assertTrue(cm.config_path(job(CX.name, BETA)).exists(), "its files are its own")
                self.assertEqual(self.run_cli(CX.name, "uninstall", "--agent", BETA)[0], 0)

    def test_connect_redeems_nothing_while_another_operation_holds_the_agent(self):
        """connect holds the agent's fence from before it redeems the code: while another operation holds it, the
        code is not spent and no seat is written."""
        held, release = threading.Event(), threading.Event()

        def holder():
            with cm.agent_fence(cm.agent_key(ALPHA)):
                held.set()
                release.wait(30)
        t = threading.Thread(target=holder)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        held.wait(30)
        backend = fx.Backend(answer={"relay_url": fx.RELAY, "bearer": fx.BEARER, "agent_id": ALPHA})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sm, "_run", side_effect=self.host), mock.patch.object(cm, "FENCE_WAIT_S", 0), \
                mock.patch.object(urllib.request, "urlopen", side_effect=backend), \
                mock.patch.object(sys, "stdin", io.StringIO(fx.CODE + "\n")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = claude_cli.main(["connect", "--base", fx.BASE, "--agent", ALPHA, "--code", "-", "--plugin-dir",
                                  str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                                  "--hmac-key-file", str(self.home / "task-hmac.key")])
        self.assertEqual(rc, 1, out.getvalue() + err.getvalue())
        self.assertIn("another operation on this agent is in progress (fence", err.getvalue())
        self.assertEqual(backend.calls, [], "the code was not spent")
        self.assertFalse(os.path.lexists(cb.seat_file(self.root_of(ALPHA))))

    def test_every_printed_rm_is_one_shell_word_for_exactly_its_file(self):
        """The manual `rm` a refusal prints quotes its path: under an agents dir whose name holds whitespace, quotes
        and shell metacharacters, the line, run by a shell in a sandbox, removes exactly that file and runs nothing."""
        sandbox = self.home / "sandbox"
        weird = sandbox / "we ird; touch PWNED $(touch PWNED2) `touch PWNED3` 'q\"&|" / "agents"
        key = cm.agent_key(BETA)
        saved = fx._dirs._injected
        self.addCleanup(setattr, fx._dirs, "_injected", saved)
        fx._dirs.set_dirs(state_dir=self.home / "ws-state", agents_dir=weird)
        decl = weird / f"{key}.bridge" / cb.DECLARATION
        decl.parent.mkdir(parents=True)
        decl.write_text("not json")
        decoy = decl.parent / "decoy"
        decoy.write_text("kept")
        lines = [cm.declaration_problem(cm.for_key(CL, key)),
                 cb._sources.remove_bridge_declaration(decl, written_by="x install")[1]]
        for line in lines:
            with self.subTest(line=line):
                found = re.findall(r"\((rm (?:'[^']*'|\"[^\"]*\"|[^\s'\"()])+)\)", line)
                self.assertEqual(len(found), 1, line)
                command = found[0]
                self.assertEqual(shlex.split(command), ["rm", str(decl)], "one word, exactly the file")
                r = subprocess.run(["/bin/sh", "-c", command], cwd=sandbox, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(decl.exists())
                self.assertEqual(decoy.read_text(), "kept")
                self.assertEqual(sorted(p.name for p in sandbox.rglob("PWNED*")), [], "nothing else ran")
                decl.write_text("not json")


@expand
class Round9(MultiBase):
    def _foreground_kept(self, adapter):
        """A refusal before install touches anything leaves everything as it was: with the managed job stopped and
        a live foreground (debug) session holding the root, a reinstall is refused ("nothing was applied") and the
        holder, the running bridge, the config and both units are byte-identical; under sparrowd, the declaration
        too. Its controls, failures after the install itself stopped the session, still stop the bridge."""
        a = job(adapter, ALPHA)
        root, bridge = self.root_of(ALPHA), self.host.bridges[a.job]
        decl = cb.bridge_home(root) / cb.DECLARATION
        for sparrowd in (False, True):
            with self.subTest(sparrowd=sparrowd):
                if sparrowd:
                    lm.Round5.sparrowd(self)
                    self.assertEqual(cb._sources.write_seat_credentials(
                        cb.bridge_home(root), agent_id=ALPHA, instance="c" * 32, tier="owner",
                        relay_url="https://relay.backend.example/relay", bearer="bearer-SEAT0001"), "")

                def full():
                    if not sparrowd:
                        return self.install(adapter, ALPHA)
                    return self.run_cli(adapter, *self.install_argv(adapter), "--token-env-file", cb.seat_file(root))
                rc, out, err = full()
                self.assertEqual(rc, 0, out + err)
                with mock.patch.object(sm, "_run", side_effect=self.host), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cm.stop_supervisor(a), "")
                holder = fx.hold(str(root), adapter)
                self.addCleanup(lambda h=holder: h.poll() is None and fx.unhold(h))
                self.assertTrue(bridge.active if not sparrowd else decl.exists(), "the bridge serves the foreground session")
                before = (self.files(a), decl.read_bytes() if decl.exists() else None)
                for again in (full, lambda: self.run_cli(adapter, "install", "--agent", ALPHA)):
                    rc, out, err = again()
                    self.assertEqual(rc, 1, out + err)
                    self.assertIn("nothing was applied", err)
                    self.assertNotIn("stopped:", out + err, "nothing was stopped")
                    self.assertIsNone(holder.poll(), "the foreground session still holds the root")
                    self.assertEqual((self.files(a), decl.read_bytes() if decl.exists() else None), before,
                                     "the config, both units and the declaration are byte-identical")
                    self.assertTrue(bridge.active if not sparrowd else decl.exists(), "the bridge still serves it")
                fx.unhold(holder)
                self.assertEqual(full()[0], 0)
                with mock.patch.object(cm, "_await_free", return_value="still held (test)"):
                    rc, out, err = full()
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn("was stopped and nothing else was applied", err)
                self.assertIn("since its session is not running" if not sparrowd else "was withdrawn: its session", err)
                self.assertFalse(bridge.active if not sparrowd else decl.exists())
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", ALPHA)[0], 0)
    test_a_refusal_before_any_change_keeps_a_foreground_session_and_its_bridge = per_manager(_foreground_kept)

    def install_argv(self, adapter):
        if adapter == CL.name:
            return ["install", "--plugin-dir", str(self.plugin), "--claude-config-dir", str(self.home / ".claude-sutando"),
                    "--hmac-key-file", str(self.home / "task-hmac.key")]
        return ["install", "--codex", str(self.codex), "--codex-home", str(self.codex_home)]

if __name__ == "__main__":
    unittest.main()
