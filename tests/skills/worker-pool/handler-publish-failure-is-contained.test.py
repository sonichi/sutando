#!/usr/bin/env python3
"""A failed handler declaration must not spread beyond the worker it happened to.

Two ways the new publication could reach past its own call, both filed as [P2]
on #4580:

  * pool_remedy recovers a BATCH. The publication raises HandlerPublishError,
    which is a RosterError, not a SpawnRefused -- so an un-normalised error
    escapes recover()'s except and aborts the whole apply() loop. The tick that
    selected those workers has already persisted recover_issued_at, so the ones
    after the failure lose the attempt it funded.
  * tick(persist=False) is what `pool_remedy.py --sweep --dry-run` runs. An
    unconditional backfill there activates routing for every bound room while
    reporting a dry run.

Run: python3 tests/skills/worker-pool/handler-publish-failure-is-contained.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_remedy as rem  # noqa: E402
import spawn_worker as sw  # noqa: E402

LAUNCHER_NAME = Path(sw.LAUNCHER).name

sup, sw = rem.sup, rem.sw
pr = sup.pr
SOCK = "/recorded/app/run/tmux.sock"


class FakeTmux:
    """Enough of tmux for spawn(): sessions are absent until the launcher runs."""

    def __init__(self):
        self.calls, self.envs, self.live = [], [], set()

    def __call__(self, argv, **kw):
        cp = subprocess.CompletedProcess
        self.calls.append(argv)
        self.envs.append(dict(kw.get("env") or {}))
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return cp(argv, 0, "claude\n", "")
        if len(argv) > 2 and argv[2] == "watcher-sentinel":
            return cp(argv, 0, "sentinel-" + (kw.get("env") or {})["SUTANDO_INSTANCE_ID"], "")
        if argv[0] == "bash" and argv[1].endswith(LAUNCHER_NAME):
            self.live.add((kw.get("env") or {}).get("SUTANDO_TMUX_SESSION", ""))
            return cp(argv, 0, "Started detached.", "")
        if len(argv) > 3 and argv[3] == "has-session":
            name = argv[-1].lstrip("=")
            return (cp(argv, 0, "", "") if name in self.live
                    else cp(argv, 1, "", f"can't find session: {name}"))
        return cp(argv, 0, "", "")

    def launches(self):
        return [a for a in self.calls if a[0] == "bash" and a[1].endswith(LAUNCHER_NAME)]


class ABatchSurvivesOnePublishFailure(unittest.TestCase):
    """The first worker's publication fails; the SECOND must still be recovered."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.t = FakeTmux()
        self.ids = []
        for label in ("alpha", "beta"):
            out = sw.spawn(self.ws, REPO, cwd=str(REPO), socket=SOCK, label=label,
                           runner=self.t, require_sentinel=False)
            # Recovery acts only on a rostered worker; spawn() alone does not roster it.
            pr.register_worker(self.ws, out["worker_id"], label, runtime=out.get("runtime"))
            self.ids.append(out["worker_id"])
        self.t.live.clear()                       # both workers died
        self.before = len(self.t.launches())

    def _publish_failing_once(self):
        """Fail the FIRST publication only, the way a transient write does."""
        real, state = pr.publish_task_event_handler, {"n": 0}

        def fake(workspace):
            state["n"] += 1
            if state["n"] == 1:
                raise pr.HandlerPublishError("cannot publish the task-event handler: disk")
            return real(workspace)

        return fake, state

    def test_the_second_worker_is_still_recovered(self):
        fake, _ = self._publish_failing_once()
        orig = pr.publish_task_event_handler
        sw.pr.publish_task_event_handler = fake
        try:
            done = rem.apply(self.ws, REPO,
                             {w: rem.ps.RECOVER for w in self.ids}, runner=self.t)
        finally:
            sw.pr.publish_task_event_handler = orig

        rec = done["recoveries"]
        outcomes = [rec[w]["outcome"] for w in self.ids]
        self.assertEqual(outcomes, [rem.FAILED, rem.RECOVERED],
                         "a publish failure on the first worker swallowed the second's "
                         f"recovery; got {outcomes}")
        self.assertEqual(len(self.t.launches()) - self.before, 1,
                         "the surviving worker was never launched")

    def test_the_failure_is_reported_as_a_refusal_not_an_exception(self):
        fake, _ = self._publish_failing_once()
        orig = pr.publish_task_event_handler
        sw.pr.publish_task_event_handler = fake
        try:
            out = rem.recover(self.ws, REPO, self.ids[0], runner=self.t)
        finally:
            sw.pr.publish_task_event_handler = orig
        self.assertEqual(out["outcome"], rem.FAILED)
        self.assertIn("task-event handler", out["why"],
                      "the refusal must say what actually failed")


class ADryRunNeverPublishes(unittest.TestCase):
    """`--sweep --dry-run` reports; it must not activate routing."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.t = FakeTmux()
        out = sw.spawn(self.ws, REPO, cwd=str(REPO), socket=SOCK, label="alpha",
                       runner=self.t, require_sentinel=False)
        pr.register_worker(self.ws, out["worker_id"], "alpha")
        self.cfg = pr.task_event_handler_config_path(self.ws / "state")

    def test_a_dry_run_leaves_a_MISSING_declaration_missing(self):
        self.cfg.unlink()
        sup.tick(self.ws, 1000.0, runner=self.t, persist=False)
        self.assertFalse(self.cfg.exists(),
                         "a dry run published the handler and switched routing on for "
                         "every bound room")

    def test_a_dry_run_leaves_a_STALE_declaration_stale(self):
        self.cfg.write_text(json.dumps({"handler": "/somewhere/else/handler.py"}))
        sup.tick(self.ws, 1000.0, runner=self.t, persist=False)
        self.assertEqual(json.loads(self.cfg.read_text())["handler"],
                         "/somewhere/else/handler.py",
                         "a dry run rewrote an existing declaration")

    def test_a_REAL_tick_still_backfills(self):
        self.cfg.unlink()
        sup.tick(self.ws, 1000.0, runner=self.t, persist=True)
        self.assertTrue(self.cfg.exists(),
                        "the backfill this PR exists for stopped working")


if __name__ == "__main__":
    unittest.main(verbosity=2)
