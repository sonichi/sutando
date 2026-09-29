#!/usr/bin/env python3
"""A worker's watcher gate is about ONE instance, not about the host.

`/schedule-crons` step 1.5 gates on running watcher TREES — correct for the
core, and on a pool host always satisfied by the core's own, so a worker that
consults it answers `skip` and never starts the watcher it exists to run. This
gate reads the sentinel THIS instance stamps, resolved by the watcher's own
owner (`util_paths.watcher_sentinel_path`), and liveness arrives as a callable
so neither polarity needs a process.

Run: python3 tests/skills/worker-pool/worker-bootstrap-decision.test.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

GATE = Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts" / "worker_bootstrap.py"

# As a caller outside src/ loads it: its own sys.path bootstrap is part of
# the module, and pre-inserting src/ would leave that unrun.
_spec = importlib.util.spec_from_file_location("worker_bootstrap", GATE)
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

REPO = Path(__file__).resolve().parents[3]
WORKER = "d" * 32
OTHER = "e" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir()
        self.inbox = str(self.ws / "deliveries" / WORKER)

    def sentinel(self, instance):
        import util_paths
        return util_paths.watcher_sentinel_path(self.ws / "state", instance=instance)

    def ask(self, *, instance=WORKER, alive=lambda pid: True,
            watcher_target=None, in_session=lambda pid: True):
        """`watcher_target` defaults to "this pid watches MY inbox", which is
        what every pre-ownership test meant by a live watcher."""
        if watcher_target is None:
            watcher_target = lambda pid: self.inbox          # noqa: E731
        return wb.decide(instance=instance, inbox=self.inbox,
                         workspace=str(self.ws), alive=alive,
                         watcher_target=watcher_target, in_session=in_session)


class TestInstanceScoped(Base):
    def test_no_sentinel_of_its_own_means_start(self):
        self.assertEqual(self.ask()[0], "start")

    def test_another_instances_live_watcher_does_not_suppress_this_one(self):
        """The exact suppression the host-wide gate produces: the CORE's watcher
        is live, and the worker must still start its own."""
        self.sentinel(OTHER).write_text("4242\n")
        self.sentinel(None).write_text("4243\n")   # the canonical core's
        self.assertEqual(self.ask(alive=lambda pid: True)[0], "start")

    def test_its_own_live_watcher_means_skip(self):
        self.sentinel(WORKER).write_text("4242\n")
        self.assertEqual(self.ask(alive=lambda pid: pid == 4242)[0], "skip")


    def test_its_own_dead_sentinel_means_start(self):
        self.sentinel(WORKER).write_text("4242\n")
        decision, why = self.ask(alive=lambda pid: False)
        self.assertEqual(decision, "start")
        self.assertIn("dead", why)

    def test_two_instances_never_read_the_same_file(self):
        self.assertNotEqual(self.sentinel(WORKER), self.sentinel(OTHER))
        self.assertNotEqual(self.sentinel(WORKER), self.sentinel(None))

class TestPidLiveness(Base):
    """The real `_pid_alive`, not the injected one: every other test hands
    `decide` a fake, so the shipped liveness check would go unexercised."""
    def test_this_process_is_alive(self):
        self.assertTrue(wb._pid_alive(os.getpid()))

    def test_a_reaped_child_is_not(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(wb._pid_alive(p.pid))

    def test_a_pid_this_user_cannot_signal_still_exists(self):
        """PermissionError means "it is there and not mine" — the opposite of
        gone. Injected: a host where pid 1 IS signalable would prove nothing."""
        self.assertTrue(self._kill_raising(PermissionError))

    def test_any_other_os_error_is_not_alive(self):
        self.assertFalse(self._kill_raising(OSError("bad pid")))

    def _kill_raising(self, exc):
        def boom(pid, sig):
            raise exc
        real, wb.os.kill = wb.os.kill, boom
        try:
            return wb._pid_alive(4242)
        finally:
            wb.os.kill = real


class TestRefusals(Base):
    def test_a_session_with_no_instance_is_not_a_worker(self):
        self.assertEqual(self.ask(instance="")[0], "unknown")

    def test_no_inbox_is_unknown_not_start(self):
        d, _ = wb.decide(instance=WORKER, inbox="", workspace=str(self.ws))
        self.assertEqual(d, "unknown")

    def test_no_workspace_is_unknown_not_start(self):
        d, why = wb.decide(instance=WORKER, inbox=self.inbox, workspace="")
        self.assertEqual(d, "unknown")
        self.assertIn("workspace", why)

    def test_an_unreadable_sentinel_is_unknown_not_start(self):
        """A directory where the file should be: readable-as-absent would read
        a broken stamp as "no watcher" and start a second one."""
        self.sentinel(WORKER).mkdir(parents=True)
        d, why = self.ask()
        self.assertEqual(d, "unknown")
        self.assertIn("unreadable", why)

    def test_a_sentinel_holding_no_pid_means_start(self):
        for content in ("not-a-pid\n", "   \n"):
            with self.subTest(content=content):
                self.sentinel(WORKER).write_text(content)
                d, why = self.ask()
                self.assertEqual(d, "start")
                self.assertIn("no pid", why)

    def test_an_unresolvable_sentinel_is_unknown_not_start(self):
        """A duplicate watcher on one folder processes every delivery twice, so
        'could not tell' must never read as 'none running'."""
        def boom(state_dir, instance):
            raise RuntimeError("no identity here")
        d, why = wb.decide(instance=WORKER, inbox=self.inbox,
                           workspace=str(self.ws), resolve=boom)
        self.assertEqual(d, "unknown")
        self.assertIn("no identity here", why)


class TestMain(Base):
    """In process, so the CLI's own branches are measured and not just run."""
    def run_main(self, *args):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wb.main(list(args))
        return rc, buf.getvalue().splitlines()

    def _pd(self):
        """pool_delivery, reached exactly as the bootstrap reaches it."""
        import pool_delivery
        return pool_delivery

    def _inbox_dir(self):
        d = Path(self.ws) / "deliveries" / WORKER
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_the_boot_decision_prunes_spent_sentinels_and_keeps_owed_ones(self):
        """A delivered task's sentinel outlived its payload and its result; every
        Stop hook and startup sweep re-walked all of them (#4614)."""
        d = self._inbox_dir()
        tasks = Path(self.ws) / "tasks"; tasks.mkdir(exist_ok=True)
        for i in range(3):                       # spent: payload gone
            (d / f"task-spent{i}.txt").touch()
        (tasks / "task-owed.txt").write_text("id: task-owed\nsource: test\ntask: x\n")
        (d / "task-owed.txt").touch()           # owed: payload present, no result
        rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox,
                                "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "start")
        sweep = [l for l in out if l.startswith("sweep=")]
        self.assertEqual(len(sweep), 1, out)
        self.assertIn("stale=3", sweep[0])
        self.assertIn("kept=1", sweep[0])
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["task-owed.txt"])

    def test_the_prune_never_releases_an_accepted_delivery(self):
        """On `skip` this worker's own watcher is live, so an accepted sentinel is
        work the session is answering; the boot sweep would re-offer it as pending."""
        pd = self._pd()
        d = self._inbox_dir()
        tasks = Path(self.ws) / "tasks"; tasks.mkdir(exist_ok=True)
        (tasks / "task-live.txt").write_text("id: task-live\nsource: test\ntask: x\n")
        (d / f"task-live{pd.ACCEPTED_SUFFIX}").touch()
        out = wb.prune_spent_sentinels(str(self.ws), WORKER)
        self.assertIn("kept=1", out)
        self.assertEqual([p.name for p in d.iterdir()], [f"task-live{pd.ACCEPTED_SUFFIX}"],
                         "an in-flight delivery was renamed back to pending")

    def test_every_way_a_finished_sentinel_can_be_spent(self):
        """`_nothing_can_re_queue` has three ways to say yes, and each is a real
        shape: the payload is gone, a ready result is still on disk, or the
        payload is archived. All three retire; none of them can re-queue."""
        pd = self._pd()
        d = self._inbox_dir()
        tasks = Path(self.ws) / "tasks"; tasks.mkdir(exist_ok=True)
        results = Path(self.ws) / "results"; results.mkdir(exist_ok=True)
        for tid in ("task-gone", "task-result", "task-arch"):
            (d / f"{tid}.txt").touch()
            pd.mark_done(self.ws, WORKER, tid, published=True)
        # task-gone: no payload at all (the common case, the drain took it).
        # task-result: payload present AND a ready result beside it.
        (tasks / "task-result.txt").write_text("id: task-result\nsource: test\ntask: x\n")
        pd.result_path(Path(self.ws), "task-result").write_text("done\n")
        # task-arch: payload present, no live result, but the payload is archived.
        (tasks / "task-arch.txt").write_text("id: task-arch\nsource: test\ntask: x\n")
        arch = pd.archived_payload(Path(self.ws), "task-arch")
        arch.parent.mkdir(parents=True, exist_ok=True); arch.write_text("archived")
        # Each one alone, so a shared fixture cannot mask a branch that never runs.
        self.assertTrue(pd._nothing_can_re_queue(Path(self.ws), "task-gone"), "payload gone")
        self.assertTrue(pd._nothing_can_re_queue(Path(self.ws), "task-result"), "ready result")
        self.assertTrue(pd._nothing_can_re_queue(Path(self.ws), "task-arch"), "archived payload")
        out = wb.prune_spent_sentinels(str(self.ws), WORKER)
        self.assertIn("retired=3", out)
        self.assertEqual(list(d.iterdir()), [])

    def test_a_sentinel_named_by_both_stages_is_judged_once(self):
        """The same task can sit in the folder as `.accepted` and `.txt`; the
        walk must judge it once, not retire it twice."""
        pd = self._pd()
        d = self._inbox_dir()
        (d / f"task-two{pd.ACCEPTED_SUFFIX}").touch()
        (d / "task-two.txt").touch()
        out = wb.prune_spent_sentinels(str(self.ws), WORKER)
        self.assertIn("stale=1", out)
        self.assertNotIn("stale=2", out)
        self.assertEqual(len(list(d.iterdir())), 1, "the second name was judged again")

    def test_a_finished_sentinel_whose_payload_could_be_re_queued_is_kept(self):
        """`residue` calls a done flag alone `finished`, but with the payload still
        in tasks/ and no findable result, removing the sentinel re-queues it."""
        pd = self._pd()
        d = self._inbox_dir()
        tasks = Path(self.ws) / "tasks"; tasks.mkdir(exist_ok=True)
        for tid in ("task-fin", "task-done"):
            (tasks / f"{tid}.txt").write_text(f"id: {tid}\nsource: test\ntask: x\n")
            (d / f"{tid}.txt").touch()
            pd.mark_done(self.ws, WORKER, tid, published=True)
        # task-done's reply is archived, so nothing can re-queue it; task-fin's is not.
        arch = pd.archived_payload(Path(self.ws), "task-done"); arch.parent.mkdir(parents=True, exist_ok=True)
        arch.write_text("archived")
        out = wb.prune_spent_sentinels(str(self.ws), WORKER)
        self.assertIn("retired=1", out)
        self.assertIn("kept=1", out)
        self.assertEqual([p.name for p in d.iterdir()], ["task-fin.txt"])

    def test_a_sweep_failure_is_reported_and_never_changes_the_decision(self):
        """A prune that raises must leave the decision, its exit code and its first
        line untouched; only the sweep= line says what happened."""
        import contextlib
        import io
        for decision, why in (("start", "no sentinel"), ("skip", "live")):
            with patch.object(wb, "decide", return_value=(decision, why)), \
                 patch.object(wb, "prune_spent_sentinels", wraps=wb.prune_spent_sentinels) as spy:
                with patch.dict(sys.modules, {"pool_delivery": None}):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = wb.main(["--instance", WORKER, "--inbox", self.inbox,
                                      "--workspace", str(self.ws)])
                    out = buf.getvalue().splitlines()
            self.assertEqual(rc, 0, decision)
            self.assertEqual(out[0], decision)
            self.assertEqual(spy.call_count, 1, f"the prune must run on {decision}")
            sweep = [l for l in out if l.startswith("sweep=")]
            self.assertEqual(len(sweep), 1, out)
            self.assertTrue(sweep[0].startswith("sweep=skipped ("), sweep[0])

    def test_no_sweep_runs_on_an_unknown_decision(self):
        """`unknown` means the state could not be read; touching the inbox there
        would act on a picture the gate itself refused to trust."""
        import contextlib
        import io
        with patch.object(wb, "decide", return_value=("unknown", "ps did not run")), \
             patch.object(wb, "prune_spent_sentinels") as spy:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wb.main(["--instance", WORKER, "--inbox", self.inbox,
                              "--workspace", str(self.ws)])
        self.assertEqual(rc, 2)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual([l for l in buf.getvalue().splitlines() if l.startswith("sweep=")], [])

    def test_a_worker_with_no_watcher_is_told_to_start(self):
        rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox,
                                "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "start")
        self.assertIn(WORKER, out[1])

    def test_an_unknown_exits_two(self):
        rc, out = self.run_main("--instance", "", "--inbox", self.inbox,
                                "--workspace", str(self.ws))
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_a_loader_that_cannot_answer_leaves_the_gate_unknown(self):
        """Not `start`: a gate that cannot find the workspace cannot know
        whether this instance already has a watcher."""
        import types
        stub = types.ModuleType("workspace_default")
        def boom():
            raise RuntimeError("no workspace here")
        stub.resolve_workspace = boom
        real = sys.modules.get("workspace_default")
        sys.modules["workspace_default"] = stub
        try:
            # A spawner-assigned workspace in the env bypasses the loader;
            # this case is about the loader, so the env must not answer.
            with patch.dict(os.environ):
                os.environ.pop("SUTANDO_WORKSPACE_DIR", None)
                rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox)
        finally:
            if real is None:
                del sys.modules["workspace_default"]
            else:
                sys.modules["workspace_default"] = real
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_no_workspace_given_falls_back_to_the_loader(self):
        """Neither the flag nor the spawner's env var: the canonical loader
        answers, as it does for a worker launched by hand."""
        with patch.dict(os.environ):
            os.environ.pop("SUTANDO_WORKSPACE_DIR", None)
            rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox)
        self.assertIn(out[0], ("start", "skip", "unknown"))
        self.assertIn(rc, (0, 2))


    def _assigned(self):
        """Workspace B, the one the spawner assigns and the watcher inspects,
        holding this instance's sentinel; the configured A (self.ws) holds none."""
        import util_paths
        b = Path(self._t.name) / "assigned"
        (b / "state").mkdir(parents=True)
        util_paths.watcher_sentinel_path(b / "state", instance=WORKER).write_text("4242\n")
        return b

    def test_the_assigned_workspace_in_env_is_the_one_inspected(self):
        """Env-only invocation, as the shipped worker gate runs it: the answer
        must come from B's sentinel, not from A having none."""
        b = self._assigned()
        with patch.dict(os.environ, {"SUTANDO_WORKSPACE_DIR": str(b)}):
            rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox)
        self.assertIn(str(b), out[2], out)
        self.assertNotIn("no sentinel", out[2], out)

    def test_control_the_configured_workspace_alone_would_authorise_a_duplicate(self):
        """The pre-fix shape: env unset and a loader naming A -> `start` with
        B's live sentinel never read. This is what the test above rules out."""
        import types
        b = self._assigned()
        stub = types.ModuleType("workspace_default")
        stub.resolve_workspace = lambda *a, **kw: self.ws
        real = sys.modules.get("workspace_default")
        sys.modules["workspace_default"] = stub
        try:
            with patch.dict(os.environ):
                os.environ.pop("SUTANDO_WORKSPACE_DIR", None)
                rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox)
        finally:
            if real is None:
                del sys.modules["workspace_default"]
            else:
                sys.modules["workspace_default"] = real
        self.assertEqual(out[0], "start")
        self.assertNotIn(str(b), out[2])


class TestCli(Base):
    def test_the_cli_reports_start_and_exits_zero(self):
        r = subprocess.run([sys.executable, str(GATE), "--instance", WORKER,
                            "--inbox", self.inbox, "--workspace", str(self.ws)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "start")

    def test_an_unknown_exits_two_so_a_caller_cannot_read_it_as_start(self):
        r = subprocess.run([sys.executable, str(GATE), "--instance", "",
                            "--inbox", self.inbox, "--workspace", str(self.ws)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout.splitlines()[0], "unknown")




class TestOwnershipIsScopedToTheInbox(Base):
    def test_a_live_pid_that_is_not_a_watcher_does_not_suppress_startup(self):
        """A recycled pid, or a stale stamp: not a watcher at all."""
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: None)
        self.assertEqual(d, "start")
        self.assertIn("not a watch-tasks-stream.sh", why)

    def test_another_workers_watcher_on_the_same_pid_does_not_count_as_mine(self):
        """The reviewer's case: pid reuse by ANOTHER pool/core watcher. Being a
        watcher is not being THIS worker's watcher; treating it as one strands
        every task assigned to this inbox."""
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: str(self.ws / "deliveries" / OTHER))
        self.assertEqual(d, "start")
        self.assertIn(OTHER, why)
        self.assertIn("not this worker's inbox", why)

    def test_a_watcher_whose_inbox_is_not_visible_is_unknown_not_skip(self):
        """Inconclusive ownership is its own answer: starting risks a second
        watcher on one inbox (every task twice), skipping risks none at all."""
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: "")
        self.assertEqual(d, "unknown")
        self.assertIn("not visible", why)

    def test_this_workers_own_watcher_still_means_skip(self):
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: self.inbox)
        self.assertEqual(d, "skip")
        self.assertIn(self.inbox, why)

    def test_the_same_inbox_by_another_name_is_still_mine(self):
        """/tmp and /private/tmp name one directory; a path-string comparison
        would read this host's own watcher as a stranger's."""
        self.sentinel(WORKER).write_text("4242\n")
        d, _ = self.ask(watcher_target=lambda pid: self.inbox + "/")
        self.assertEqual(d, "skip")

    def test_ownership_is_not_consulted_for_a_dead_pid(self):
        self.sentinel(WORKER).write_text("4242\n")
        asked = []
        d, _ = self.ask(alive=lambda pid: False,
                        watcher_target=lambda pid: asked.append(pid) or "")
        self.assertEqual(d, "start")
        self.assertEqual(asked, [])

    def test_the_shipped_probe_rejects_this_non_watcher_process(self):
        """The real probe, not a double: this process is alive and is not a
        watcher, so it must not be read as one."""
        self.assertIsNone(wb._watcher_target(os.getpid()))


class TestOwnershipIsScopedToThisSession(Base):
    """A watcher on this inbox left by an ENDED session still holds the inbox,
    but its stdout reaches no one: reading it as coverage strands every task."""

    def test_a_watcher_this_session_did_not_start_means_start(self):
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(in_session=lambda pid: False)
        self.assertEqual(d, "start", why)
        self.assertIn("not started by this session", why)

    def test_an_unobservable_ancestry_is_unknown_not_skip(self):
        def blind(pid):
            raise wb.Unobserved("ps failed")
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(in_session=blind)
        self.assertEqual(d, "unknown", why)

    def test_the_session_is_the_nearest_ancestor_that_is_not_a_shell(self):
        table = {50: (40, "python3"), 40: (30, "zsh"), 30: (20, "claude"),
                 20: (1, "tmux"), 60: (30, "zsh"), 61: (60, "bash"),
                 70: (1, "bash")}
        self.assertEqual(wb.session_root(table, 40), 30)
        self.assertTrue(wb.descends_from(table, 61, 30))
        self.assertFalse(wb.descends_from(table, 70, 30))

    def test_a_chain_of_shells_up_to_init_scopes_nothing(self):
        self.assertIsNone(wb.session_root({40: (1, "-zsh")}, 40))

    def test_a_ps_that_raises_leaves_the_session_unobserved(self):
        def ps(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 5))
        self.assertIsNone(wb._process_table(run=ps))
        with self.assertRaises(wb.Unobserved):
            wb._in_this_session(4242, run=ps)

    def test_a_ps_that_exits_non_zero_leaves_the_session_unobserved(self):
        def ps(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "ps: denied")
        self.assertIsNone(wb._process_table(run=ps))
        with self.assertRaises(wb.Unobserved):
            wb._in_this_session(4242, run=ps)

    def test_a_snapshot_with_no_session_root_cannot_disown_the_watcher(self):
        """Every ancestor of this gate is a shell up to init: nothing scopes
        the session, so the watcher keeps the pre-ancestry answer."""
        me = os.getppid()
        def ps(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 0, f"{me} 1 -zsh\n4242 1 bash\n", "")
        self.assertTrue(wb._in_this_session(4242, run=ps))

    def test_a_snapshot_decides_descent_from_the_session_root(self):
        me = os.getppid()
        def ps(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 0, f"{me} 30 zsh\n30 20 claude\n20 1 tmux\n4242 30 bash\n4343 1 bash\n", "")
        self.assertTrue(wb._in_this_session(4242, run=ps))
        self.assertFalse(wb._in_this_session(4343, run=ps))

    def test_a_detached_watcher_on_this_inbox_is_not_this_sessions(self):
        """The real shape: a watcher reparented away from this session."""
        script = self.ws / "watch-tasks-stream.sh"
        script.write_text("#!/bin/sh\nsleep 30\n")
        out = subprocess.run(
            ["bash", "-c", f'nohup bash "{script}" "{self.inbox}" >/dev/null 2>&1 & echo $!'],
            capture_output=True, text=True, check=True).stdout.strip()
        pid = int(out)
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))
        import watcher_identity
        for _ in range(50):
            if watcher_identity.proc_argv_vector(pid) is not None:
                break
            time.sleep(0.02)
        else:
            self.skipTest("no authoritative argv read on this platform")
        table = wb._process_table()
        if wb.session_root(table, os.getppid()) is None:
            self.skipTest("this test process has no non-shell ancestor to scope to")
        if table.get(pid, (0,))[0] != 1:
            self.skipTest("a subreaper adopted the detached process")
        self.sentinel(WORKER).write_text(f"{pid}\n")
        d, why = wb.decide(instance=WORKER, inbox=self.inbox, workspace=str(self.ws),
                           alive=lambda p: True)
        self.assertEqual(d, "start", why)
        self.assertIn("not started by this session", why)


class TestTheShippedStartupNamesTheInbox(Base):
    """F1: the ownership check reads the watched inbox from argv, so the
    instruction that starts a worker's watcher has to put it there."""

    def test_the_worker_startup_step_passes_the_inbox(self):
        """Not "the line mentions the variable" — the shipped COMMAND has to
        name the inbox, read by the same parser the ownership check uses."""
        skill = (REPO / "skills" / "startup" / "SKILL.md").read_text(encoding="utf-8")
        line = next(ln for ln in skill.splitlines()
                    if "watch-tasks-stream.sh" in ln and "On `start` only" in ln)
        m = re.search(r"`command:\s*'([^']+)'`", line)
        self.assertIsNotNone(m, f"no quoted `command:` in the startup step: {line[:120]}")
        # The shell's own tokenisation is the argv vector that command becomes.
        cmd = m.group(1)
        self.assertNotEqual(
            wb._target_from_argv(cmd, 4242, argv_vector=lambda pid: shlex.split(cmd)), "",
            "the shipped worker startup names no inbox in the watcher command, so "
            "decide() reads `unknown` forever and the worker never starts one")

    def test_a_watcher_started_that_way_is_recognised_as_this_workers(self):
        """The argv the instruction produces, parsed by the shipped probe."""
        argv = f'bash src/watch-tasks-stream.sh {self.inbox}'
        self.assertEqual(wb._target_from_argv(
            argv, 4242, argv_vector=lambda pid: shlex.split(argv)), self.inbox)

    def test_a_tagged_watcher_is_read_by_its_tag_not_by_the_roles_value(self):
        """`--role session --inbox X X` is the shape /startup --worker starts and
        the shape a hand re-arm uses. Reading the first dash-less token made the
        inbox "session", so this gate said start over a live watcher (#4698)."""
        for argv in (f"bash src/watch-tasks-stream.sh --role session --inbox {self.inbox} {self.inbox}",
                     f"bash src/watch-tasks-stream.sh {self.inbox} --role session --inbox {self.inbox}",
                     f"bash src/watch-tasks-stream.sh --role=session --inbox={self.inbox} {self.inbox}"):
            self.assertEqual(wb._target_from_argv(
                argv, 4242, argv_vector=lambda pid, a=argv: shlex.split(a)), self.inbox, argv)

    def test_the_tag_wins_over_a_positional_that_disagrees(self):
        """Only the tag is a reliable cross-process identity: an inbox that came
        from $SUTANDO_TASKS_DIR leaves no positional at all."""
        argv = f"bash src/watch-tasks-stream.sh /somewhere/else --role session --inbox {self.inbox}"
        self.assertEqual(wb._target_from_argv(
            argv, 4242, argv_vector=lambda pid: shlex.split(argv)), self.inbox)

    def test_a_tagged_live_watcher_makes_the_gate_skip_not_start(self):
        """The end-to-end shape of #4698: the gate must not start a duplicate."""
        argv = f"bash src/watch-tasks-stream.sh --role session --inbox {self.inbox} {self.inbox}"
        self.sentinel(WORKER).write_text("4242\n")
        decision, why = wb.decide(
            instance=WORKER, inbox=self.inbox, workspace=str(self.ws),
            alive=lambda pid: True, in_session=lambda pid: True,
            watcher_target=lambda pid: wb._target_from_argv(
                argv, pid, argv_vector=lambda _p: shlex.split(argv)))
        self.assertEqual(decision, "skip", why)

    def test_an_argv_without_an_inbox_is_still_unknown(self):
        self.assertEqual(wb._target_from_argv("bash src/watch-tasks-stream.sh"), "")

    def test_a_spaced_inbox_survives_when_the_real_argv_is_read(self):
        spaced = "/Users/me/Library/Application Support/ws/deliveries/own"
        vec = ["bash", "src/watch-tasks-stream.sh", spaced]
        self.assertEqual(wb._target_from_argv(
            f"bash src/watch-tasks-stream.sh {spaced}", 4242, argv_vector=lambda pid: vec),
            spaced)

    def test_a_flattened_argv_with_operands_is_not_disowned_but_unobserved(self):
        """A spaced script path and a script plus an operand are the same flat
        string; the shared policy refuses to decide, and so must this gate."""
        with self.assertRaises(wb.Unobserved):
            wb._target_from_argv(f"bash src/watch-tasks-stream.sh {self.inbox}")

    def test_a_spaced_inbox_is_still_this_workers_own(self):
        spaced = str(self.ws / "Application Support" / "deliveries" / WORKER)
        self.sentinel(WORKER).write_text("4242\n")
        vec = ["bash", "src/watch-tasks-stream.sh", spaced]
        decision, _ = wb.decide(
            instance=WORKER, inbox=spaced, workspace=str(self.ws),
            alive=lambda pid: True, in_session=lambda pid: True,
            watcher_target=lambda pid: wb._target_from_argv(
                f"bash src/watch-tasks-stream.sh {spaced}", pid, argv_vector=lambda p: vec))
        self.assertEqual(decision, "skip")


class TestThroughTheProcessInspectionBoundary(Base):
    """`decide` with its SHIPPED inspector: a real `ps`, a real argv read.
    Only the OS answer is varied -- by a real process, or at the `ps` call."""

    def _decide(self, pid):
        self.sentinel(WORKER).write_text(f"{pid}\n")
        return wb.decide(instance=WORKER, inbox=self.inbox, workspace=str(self.ws),
                         alive=lambda p: True)

    def _spawn(self, argv):
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        import watcher_identity
        for _ in range(50):
            if watcher_identity.proc_argv_vector(p.pid) is not None:
                break
            time.sleep(0.02)
        return p

    def test_a_ps_that_exits_1_is_unknown_never_start(self):
        def ps(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "ps: permission denied")
        with patch.object(subprocess, "run", ps):
            d, why = self._decide(4242)
        self.assertEqual(d, "unknown", why)
        self.assertIn("could not be observed", why)

    def test_a_ps_that_times_out_is_unknown_never_start(self):
        def ps(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 10))
        with patch.object(subprocess, "run", ps):
            d, why = self._decide(4242)
        self.assertEqual(d, "unknown", why)
        self.assertIn("timed out", why)

    def test_an_observer_that_mentions_the_script_is_not_a_watcher(self):
        """`python3 observer.py src/watch-tasks-stream.sh <this inbox>`: the
        argv mentions the script and names THIS inbox, and is still not one."""
        obs = self.ws / "observer.py"
        obs.write_text("import time; time.sleep(30)\n")
        p = self._spawn([sys.executable, str(obs), "src/watch-tasks-stream.sh", self.inbox])
        d, why = self._decide(p.pid)
        self.assertEqual(d, "start", why)
        self.assertIn("not a watch-tasks-stream.sh", why)

    def test_a_genuine_watcher_on_this_inbox_is_skipped(self):
        script = self.ws / "watch-tasks-stream.sh"
        script.write_text("#!/bin/sh\nsleep 30\n")
        p = self._spawn(["bash", str(script), self.inbox])
        import watcher_identity
        if watcher_identity.proc_argv_vector(p.pid) is None:
            self.skipTest("no authoritative argv read on this platform")
        d, why = self._decide(p.pid)
        self.assertEqual(d, "skip", why)

    def test_a_genuine_watcher_on_another_inbox_starts_ours(self):
        script = self.ws / "watch-tasks-stream.sh"
        script.write_text("#!/bin/sh\nsleep 30\n")
        p = self._spawn(["bash", str(script), str(self.ws / "deliveries" / OTHER)])
        import watcher_identity
        if watcher_identity.proc_argv_vector(p.pid) is None:
            self.skipTest("no authoritative argv read on this platform")
        d, why = self._decide(p.pid)
        self.assertEqual(d, "start", why)

    def test_the_cli_exit_code_carries_unknown(self):
        """rc 2 is what `/startup --worker` acts on; an unobserved inspection
        must not come out as rc 0 with `start`."""
        self.sentinel(WORKER).write_text(f"{os.getpid()}\n")    # alive, for real
        def ps(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "")
        import contextlib
        import io
        out = io.StringIO()
        with patch.object(subprocess, "run", ps), contextlib.redirect_stdout(out):
            rc = wb.main(["--instance", WORKER, "--inbox", self.inbox, "--workspace", str(self.ws)])
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue().splitlines()[0], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=0)
