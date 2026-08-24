#!/usr/bin/env python3
"""The task-watcher probe must not tell a pool to kill its own watchers.

A pool runs one watcher per core. The PID sentinel is single-valued and is held
by whichever core stamped last, so N-1 of N watchers always read as "not tracked
by the sentinel". The pre-fix verdict ended "Keep the sentinel's (pid), stop the
rest", which on a 4-core host advises stopping 3 live watchers — every core but
one stops draining tasks/.

The single-core verdict is unchanged: there the extra tree really is a duplicate.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", _REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class ProbeVerdictUsesLOCALSessionOwnership(unittest.TestCase):
    """Drives the REAL check_task_watcher().

    The decision is "does this untracked tree still have a live owning session",
    answered from the LOCAL process table. An earlier version counted fresh
    `state/cores/*.alive` records instead; that directory is synced across hosts,
    so a peer heartbeat suppressed cleanup of genuinely orphaned local watchers.
    """

    def _verdict(self, ppids, core_names=("main",)):
        """ppids maps each extra root pid -> its parent ('1' means reparented)."""
        hc = _load()
        td = tempfile.mkdtemp()
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        for n in core_names:
            (ws / "state" / "cores" / f"{n}.alive").write_text("{}")
        (ws / "state" / "watch-tasks-stream.pid").write_text("100")
        hc.WORKSPACE_DIR = ws
        # OS-facing edges only; the decision under test is real.
        hc._ps_snapshot = lambda: "PID TT  STAT  TIME COMMAND\n"
        hc._watcher_trees = lambda ps_output=None: {
            r: [r] for r in ["100", *ppids]}
        hc._proc_argv = lambda pid: "bash src/watch-tasks-stream.sh"
        hc._any_core_alive = lambda *a, **k: True
        hc._pid_parent = lambda pid, ps=None: ppids.get(str(pid))
        return hc.check_task_watcher()["detail"]

    def test_every_extra_session_owned_is_left_alone(self):
        d = self._verdict({"200": "199", "300": "299", "400": "399"})
        self.assertIn("Do NOT stop them", d)
        self.assertIn("legitimate", d)
        self.assertNotIn("Stop those", d)

    def test_every_extra_orphaned_gets_the_stop_advice(self):
        """Positive control: without it, a verdict that never says stop passes above."""
        d = self._verdict({"200": "1", "300": "1", "400": "1"})
        self.assertIn("Stop those", d)
        self.assertIn("NO live owning session", d)
        self.assertNotIn("Do NOT stop them", d)

    def test_MIXED_names_only_the_orphans_and_protects_the_owned(self):
        """All-or-nothing was the old shape; a real pool mid-restart is mixed."""
        d = self._verdict({"200": "199", "300": "1", "400": "399"})
        self.assertIn("300", d)
        self.assertIn("must be left alone", d)
        self.assertIn("200", d)
        self.assertIn("400", d)

    def test_LOCALITY_a_fresh_REMOTE_heartbeat_cannot_suppress_local_cleanup(self):
        """The regression this rewrite exists for: one local core, several fresh
        peer heartbeats, and duplicate LOCAL trees that are genuinely orphaned.
        The old count made this the pool branch and left them running."""
        d = self._verdict({"200": "1", "300": "1"},
                          core_names=("main", "peer-host-a", "peer-host-b", "peer-host-c"))
        self.assertIn("Stop those", d)
        self.assertNotIn("legitimate", d)

    def test_unknown_parentage_is_treated_as_orphaned_not_owned(self):
        """Fail closed: an unreadable parent cannot support an ownership claim."""
        d = self._verdict({"200": None})
        self.assertIn("Stop those", d)


class EveryMultiRootBranchSplitsOwnership(unittest.TestCase):
    """The tracked-sentinel branch was split first; these two were not — and they
    are the branches a pool actually lands in, because a pool's sentinel is
    routinely absent (never stamped) or stale (stamped by a core that exited).
    Each emitted ONE undifferentiated pid list ending in "stop them and restart
    one cleanly", naming every root including the session-owned ones.
    """

    def _verdict(self, ppids, sentinel):
        """sentinel: 'absent' (never stamped) or 'dead' (stamped, pid gone)."""
        hc = _load()
        ws = Path(tempfile.mkdtemp())
        (ws / "state" / "cores").mkdir(parents=True)
        (ws / "state" / "cores" / "main.alive").write_text("{}")
        if sentinel == "dead":
            (ws / "state" / "watch-tasks-stream.pid").write_text("999")
        hc.WORKSPACE_DIR = ws
        hc._ps_snapshot = lambda: "PID TT  STAT  TIME COMMAND\n"
        hc._watcher_trees = lambda ps_output=None: {r: [r] for r in ppids}
        hc._proc_argv = lambda pid: None  # the stamped pid is gone
        hc._any_core_alive = lambda *a, **k: True
        hc._pid_parent = lambda pid, ps=None: ppids.get(str(pid))
        return hc.check_task_watcher()["detail"]

    POOL = {"200": "199", "300": "299", "400": "399"}
    ORPHANS = {"200": "1", "300": "1", "400": "1"}
    MIXED = {"200": "199", "300": "1", "400": "399"}

    def test_absent_sentinel_pool_is_left_alone(self):
        d = self._verdict(self.POOL, "absent")
        self.assertIn("Do NOT stop them", d)
        self.assertNotIn("Stop those", d)

    def test_dead_sentinel_pool_is_left_alone(self):
        """This host's live verdict shape: sentinel dead, four owned watchers."""
        d = self._verdict(self.POOL, "dead")
        self.assertIn("Do NOT stop them", d)
        self.assertNotIn("Stop those", d)

    def test_absent_sentinel_all_orphaned_still_says_stop(self):
        """Positive control: without it, a verdict that never advises stopping
        anything would satisfy both tests above."""
        d = self._verdict(self.ORPHANS, "absent")
        self.assertIn("Stop those", d)
        self.assertIn("NO live owning session", d)

    def test_dead_sentinel_all_orphaned_still_says_stop(self):
        d = self._verdict(self.ORPHANS, "dead")
        self.assertIn("Stop those", d)
        self.assertIn("NO live owning session", d)

    def test_a_stop_instruction_never_names_a_session_owned_root(self):
        """The invariant the consuming instruction keys on: whatever pids follow
        'NO live owning session', none of them may be owned. A verdict that
        listed every root — the pre-fix shape — fails here on both branches."""
        for sentinel in ("absent", "dead"):
            d = self._verdict(self.MIXED, sentinel)
            named = re.search(r"NO live owning session \(root pids ([^)]*)\)", d)
            self.assertIsNotNone(named, f"{sentinel}: no ownerless group named in {d!r}")
            stopped = {p.strip() for p in named.group(1).split(",")}
            self.assertEqual(stopped, {"300"}, f"{sentinel}: stop list was {stopped}")
            self.assertIn("must be left alone", d, sentinel)
            for owned in ("200", "400"):
                self.assertIn(owned, d.split("must be left alone")[0].split("Stop those")[1],
                              f"{sentinel}: owned root {owned} missing from the protected group")


if __name__ == "__main__":
    unittest.main(verbosity=2)
