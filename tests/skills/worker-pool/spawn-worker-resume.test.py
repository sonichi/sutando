#!/usr/bin/env python3
"""Resuming a worker keeps its identity; only the run is new.

A reboot kills every worker process and leaves the records and the runtime
transcripts intact. Bringing one back must therefore NOT mint a new worker:
the owner's stable handles -- the worker id, its label, its delivery folder --
are what she addresses and binds rooms to, so a "resume" that allocates a new
id is a fresh worker wearing the same transcript, and every binding that names
the old id still points at nothing.

So the invariant under test is: resume reuses worker_id, label and inbox, adds
one incarnation to the EXISTING record, and hands the runtime --resume for the
recorded session -- while a fresh spawn keeps allocating a new id as before.

Run: python3 tests/skills/worker-pool/spawn-worker-resume.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spawn_worker as sw  # noqa: E402
import worker_identity as wi  # noqa: E402

_launcher = __import__("spawn-worker-launcher.test".replace(".test", "_test"), fromlist=["*"]) \
    if False else None  # the fakes are re-declared below rather than imported


class FakeTmux:
    """Same contract as the launcher suite's fake: records argv+env, answers
    has-session from a known set, and creates the session on launch."""

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

    def launches(self):
        return [e for a, e in zip(self.calls, self.envs)
                if a[0] == "bash" and a[1].endswith("start-cli.sh")]


def _spawned(ws, repo, runner, label="alpha"):
    """One real spawn, so the resume acts on a record a real spawn produced."""
    return sw.spawn(ws, repo, cwd=str(repo), socket="/tmp/t.sock", label=label,
                    runner=runner, require_sentinel=False)


class ResumeKeepsIdentity(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.repo = REPO

    def test_resume_reuses_the_worker_id_and_inbox(self):
        t = FakeTmux()
        first = _spawned(self.ws, self.repo, t)
        t.existing.clear()          # the reboot: records survive, processes do not

        again = sw.spawn(self.ws, self.repo, cwd=str(self.repo), socket="/tmp/t.sock",
                         runner=t, require_sentinel=False,
                         resume=first["runtime_session_id"])

        self.assertEqual(again["worker_id"], first["worker_id"],
                         "resume minted a NEW worker id; every binding naming the old "
                         "id now points at nothing")
        self.assertEqual(again["delivery_dir"], first["delivery_dir"],
                         "resume moved the inbox, so deliveries land where nobody reads")
        self.assertEqual(again["runtime_session_id"], first["runtime_session_id"],
                         "resume did not keep the recorded session")

    def test_resume_recovers_the_label_from_the_roster(self):
        """The owner's chosen name lives in the ROSTER (register_worker), not in
        the identity records, and she must not have to retype it per restart."""
        import pool_roster as pr
        t = FakeTmux()
        first = _spawned(self.ws, self.repo, t)
        pr.register_worker(self.ws, first["worker_id"], "Mars-the-product-dev")
        t.existing.clear()
        again = sw.spawn(self.ws, self.repo, cwd=str(self.repo), socket="/tmp/t.sock",
                         runner=t, require_sentinel=False,
                         resume=first["runtime_session_id"])
        self.assertEqual(again["label"], "Mars-the-product-dev",
                         "the owner had to retype the label on every restart")

    def test_an_explicit_label_still_wins_over_the_roster(self):
        import pool_roster as pr
        t = FakeTmux()
        first = _spawned(self.ws, self.repo, t)
        pr.register_worker(self.ws, first["worker_id"], "old-name")
        t.existing.clear()
        again = sw.spawn(self.ws, self.repo, cwd=str(self.repo), socket="/tmp/t.sock",
                         runner=t, require_sentinel=False, label="renamed",
                         resume=first["runtime_session_id"])
        self.assertEqual(again["label"], "renamed")

    def test_resume_hands_the_runtime_the_resume_env_not_a_new_session_id(self):
        t = FakeTmux()
        first = _spawned(self.ws, self.repo, t)
        t.existing.clear()
        sw.spawn(self.ws, self.repo, cwd=str(self.repo), socket="/tmp/t.sock",
                 runner=t, require_sentinel=False, resume=first["runtime_session_id"])
        env = t.launches()[-1]
        self.assertEqual(env.get("SUTANDO_CLAUDE_RESUME"), first["runtime_session_id"],
                         "the launcher was not told to resume, so the history is lost")
        self.assertNotIn("SUTANDO_CLAUDE_SESSION_ID", sorted(k for k, v in env.items() if v),
                         "a session id alongside resume: start-cli prefers resume, but "
                         "sending both states two intents")

    def test_resume_adds_an_incarnation_rather_than_a_second_worker(self):
        t = FakeTmux()
        first = _spawned(self.ws, self.repo, t)
        t.existing.clear()
        sw.spawn(self.ws, self.repo, cwd=str(self.repo), socket="/tmp/t.sock",
                 runner=t, require_sentinel=False, resume=first["runtime_session_id"])
        wid = first["worker_id"]
        self.assertEqual(len(wi.incarnations(self.ws, wid)), 2,
                         "a resumed run must be a second incarnation of one worker")
        self.assertEqual(len(wi.sessions(self.ws, wid)), 1,
                         "resume recorded a NEW session; it is the same conversation")
        self.assertEqual(len([d for d in (self.ws / "state/workers").iterdir()]), 1,
                         "a second worker directory was created")

    def test_resume_refuses_a_session_this_worker_never_had(self):
        t = FakeTmux()
        _spawned(self.ws, self.repo, t)
        t.existing.clear()
        with self.assertRaises(sw.SpawnRefused):
            sw.spawn(self.ws, self.repo, cwd=str(self.repo), socket="/tmp/t.sock",
                     runner=t, require_sentinel=False,
                     resume="ffffffff-0000-0000-0000-000000000000")

    def test_control_a_fresh_spawn_still_mints_a_new_worker(self):
        """The mutation control: if resume's branch swallowed the normal path,
        two plain spawns would share an id and this fails."""
        t = FakeTmux()
        a = _spawned(self.ws, self.repo, t)
        b = _spawned(self.ws, self.repo, t)
        self.assertNotEqual(a["worker_id"], b["worker_id"])
        # KEYS only: assertNotIn prints the container, and the container is a real
        # spawn environment — a failure would paste every secret it holds.
        self.assertNotIn("SUTANDO_CLAUDE_RESUME",
                         sorted(k for k, v in t.launches()[-1].items() if v),
                         "a fresh spawn asked the runtime to resume something")

    def test_a_spawn_does_not_hand_the_worker_the_cores_own_session_flag(self):
        """A core exports SUTANDO_CORE_SESSION=1 and the launcher reads the
        INHERITED value, so an unscrubbed one makes a worker's launcher believe
        it was invoked from inside a core. The guard refuses on the literal "1"
        alone, so the empty value is what reads as "not a core"."""
        t = FakeTmux()
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_SESSION": "1"}):
            _spawned(self.ws, self.repo, t)
        self.assertEqual(t.launches()[-1].get("SUTANDO_CORE_SESSION"), "",
                         "the worker inherited the core's session flag")


if __name__ == "__main__":
    unittest.main(verbosity=2)
