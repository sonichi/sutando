#!/usr/bin/env python3
"""A pool that comes BACK must be as routable as a pool that was just created.

`register_worker()` is the only writer of state/task-event-handler.json, and a
resume deliberately does not go through it -- resume reuses the existing worker
record, so there is nothing to register. The result is a worker that is alive
and bound to a room while the core watcher still reads "no declaration", which
it cannot tell apart from "no pool installed": every task for that room falls
through to the core. Nothing errors, which is what makes it expensive to find.

So the invariant is the spawn path's, not the roster's: whatever brings a pool
into existence -- a first spawn, a resume, or pool-remedy recovering a worker
(which resumes) -- leaves the declaration on disk.

Run: python3 tests/skills/worker-pool/spawn-publishes-the-handler.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_roster as pr  # noqa: E402
import spawn_worker as sw  # noqa: E402


class FakeTmux:
    """Records argv+env, answers has-session from a known set, and creates the
    session on launch -- the same contract the resume suite's fake uses."""

    def __init__(self, existing=(), runtime="claude"):
        import subprocess
        self._sp = subprocess
        self.calls, self.envs, self.existing, self.runtime = [], [], set(existing), runtime

    def __call__(self, argv, **kw):
        cp = self._sp.CompletedProcess
        self.calls.append(argv)
        self.envs.append(dict(kw.get("env") or {}))
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return cp(argv, 0, self.runtime + "\n", "")
        if argv[0] == "bash" and argv[1].endswith("start-cli.sh"):
            self.existing.add((kw.get("env") or {}).get("SUTANDO_TMUX_SESSION", ""))
            return cp(argv, 0, "Started detached.", "")
        sub = argv[3] if len(argv) > 3 else ""
        if sub == "has-session":
            name = argv[-1].lstrip("=")
            return (cp(argv, 0, "", "") if name in self.existing
                    else cp(argv, 1, "", f"can't find session: {name}"))
        return cp(argv, 0, "", "")


def _cfg(ws: Path) -> Path:
    return pr.task_event_handler_config_path(ws / "state")


def _spawn(ws, runner, **kw):
    return sw.spawn(ws, REPO, cwd=str(REPO), socket="/tmp/t.sock",
                    runner=runner, require_sentinel=False, **kw)


class SpawnDeclaresTheRouter(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def _handler(self) -> str:
        return json.loads(_cfg(self.ws).read_text())["handler"]

    def test_a_resume_republishes_a_declaration_that_went_missing(self):
        """The production shape: the pool predates the publish code, so the
        file was never written, and a reboot brings the worker back by resume."""
        t = FakeTmux()
        first = _spawn(self.ws, t, label="alpha")

        _cfg(self.ws).unlink()                  # the pool that predates the file
        t.existing.clear()                      # the reboot

        _spawn(self.ws, t, resume=first["runtime_session_id"])

        self.assertTrue(_cfg(self.ws).exists(),
                        "a resumed worker is live while the watcher still reads "
                        "'no pool': its room's tasks go to the core instead")
        self.assertTrue(self._handler().endswith("pool_route_handler.py"),
                        f"declaration names {self._handler()!r}, not the router")

    def test_a_first_spawn_declares_it_too(self):
        """create_worker() registers and so publishes, but spawn is also called
        directly; the declaration must not depend on which door was used."""
        _spawn(self.ws, FakeTmux(), label="alpha")
        self.assertTrue(_cfg(self.ws).exists(),
                        "a spawn that never went through register_worker() left "
                        "no declaration")

    def test_it_is_published_before_the_session_starts(self):
        """Ordering matters for the same reason register_worker() publishes
        before its own durable writes: a live session the watcher cannot see is
        worse than a refusal, because it silently mis-routes instead."""
        seen = {}
        t = FakeTmux()
        inner = t.__call__

        def watching(argv, **kw):
            if argv[0] == "bash" and argv[1].endswith("start-cli.sh"):
                seen["declared_at_launch"] = _cfg(self.ws).exists()
            return inner(argv, **kw)

        _spawn(self.ws, watching, label="alpha")
        self.assertTrue(seen.get("declared_at_launch"),
                        "the runtime session was started before the declaration "
                        "existed, leaving a window where the worker is live and "
                        "unroutable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
