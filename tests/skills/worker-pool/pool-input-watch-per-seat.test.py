#!/usr/bin/env python3
"""Every live worker seat gets its own core-input-watch, and its cards name the seat.

The core's input watcher is what turns a gate on a pane (a limit menu, a
permission dialog, a login) into a card for the owner, and only the core had
one. The pool tick now ensures one per live worker seat, idempotently by its
tmux session, spelled so the core launchers' liveness match (`--socket <sock>`)
never mistakes a worker's watcher for the core's and skips starting it. A card
raised with `--seat` names that seat and its terminal jump names its session;
without it the core's card is unchanged.
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import importlib.util
import json
import re
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy", SCRIPTS / "pool_remedy.py")
wi = rem.wi
ciw = _load("core_input_watch", REPO / "src" / "core-input-watch.py")

WID = "7c54b230a8d94ea9b86f52d70134ac68"
SOCK = "/tmp/pool-input-watch-test.sock"
NAME = f"sutando-worker-{WID}"


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Tmux:
    """has-session answers from `alive`; every other tmux call succeeds and is recorded."""

    def __init__(self, alive):
        self.alive, self.calls = set(alive), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if "has-session" in argv:
            return Done(0 if argv[-1].lstrip("=") in self.alive else 1, err="can't find session")
        return Done(0)

    def started(self):
        return [c for c in self.calls if "new-session" in c]


def scratch():
    ws = Path(tempfile.mkdtemp(prefix="pool-input-watch-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: {"state": "live", "label": "comm"}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK, tmux_session=NAME)
    return ws


def core_liveness_patterns():
    """The launchers' own `pgrep -f` regexes, read from the scripts, with the socket filled in."""
    out = []
    for f in ("src/agent/claude/cli/start-cli.sh", "src/agent/codex/cli/start-cli.sh"):
        for m in re.finditer(r'pgrep -f "(core-input-watch[^"]*)"', (REPO / f).read_text()):
            out.append(m.group(1).replace("${TMUX_SOCKET}", re.escape(SOCK)).replace("\\\\", "\\"))
    return out


class EnsurePerSeat(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(rem, "ensure_input_watch"), "pool_remedy has no ensure_input_watch")
        self.ws = scratch()

    def test_a_live_seat_without_one_gets_one(self):
        t = Tmux({NAME})
        out = rem.ensure_input_watch(self.ws, REPO, WID, runner=t)
        self.assertEqual(out["outcome"], rem.WATCHING)
        [cmd] = t.started()
        self.assertEqual(cmd[1:7], ["-S", SOCK, "new-session", "-d", "-s", NAME + "-input"])
        words = shlex.split(cmd[-1])
        self.assertTrue(words[1].endswith("src/core-input-watch.py"), words)
        self.assertIn(f"--socket={SOCK}", words)
        self.assertIn(f"--session={NAME}", words)
        self.assertIn(f"--out={self.ws}/state/core-supervisor.{NAME}.json", words)
        self.assertIn(f"--seat=worker comm ({WID})", words)

    def test_idempotent_by_its_tmux_session(self):
        t = Tmux({NAME, NAME + "-input"})
        self.assertEqual(rem.ensure_input_watch(self.ws, REPO, WID, runner=t)["outcome"], rem.WATCHING)
        self.assertEqual(t.started(), [])

    def test_a_seat_that_is_gone_gets_none(self):
        t = Tmux(set())
        self.assertEqual(rem.ensure_input_watch(self.ws, REPO, WID, runner=t)["outcome"], rem.NOT_RUNNING)
        self.assertEqual(t.started(), [])

    def test_only_seats_that_answered_alive(self):
        t = Tmux({NAME})
        got = rem.ensure_input_watches(self.ws, REPO, {WID: {"session_alive": True},
                                                       "dead": {"session_alive": False}}, runner=t)
        self.assertEqual(list(got), [WID])

    def test_the_core_launchers_never_read_it_as_the_core_watcher(self):
        pats = core_liveness_patterns()
        self.assertEqual(len(pats), 2, pats)
        cmd = rem.input_watch_command(self.ws, REPO, WID, SOCK, "worker comm")
        argv = " ".join(cmd)
        for p in pats:
            self.assertIsNone(re.search(p, argv), p)
        core = f"python3 {REPO}/src/core-input-watch.py --socket {SOCK} --session sutando-core --out x"
        for p in pats:
            self.assertIsNotNone(re.search(p, core), f"control: {p} must still match the core's own")


class RetryInjected(unittest.TestCase):
    def test_the_seats_watcher_carries_the_pools_restart_for_this_worker(self):
        ws = Path(tempfile.mkdtemp(prefix="pool-retry-"))
        cmd = rem.input_watch_command(ws, REPO, WID, SOCK, "worker comm")
        watch = shlex.split(cmd[-1].split(" & ", 1)[0])
        [flag] = [a for a in watch if a.startswith("--retry-cmd=")]
        retry = shlex.split(flag.split("=", 1)[1])
        self.assertTrue(retry[1].endswith("skills/worker-pool/scripts/pool_manage.py"), retry)
        self.assertEqual(retry[-2:], ["retry", WID])
        self.assertEqual(retry[retry.index("--workspace") + 1], str(ws))


class RetryVerb(unittest.TestCase):
    """pool_manage retry: the owner's "retry now" restarts a live limited seat; a stopped one is left."""

    def setUp(self):
        from unittest import mock
        self.pm = _load("pool_manage", SCRIPTS / "pool_manage.py")
        self.calls = []
        self.patches = [
            mock.patch.object(self.pm, "_worker", lambda ws, t: (WID, {"state": "active"})),
            mock.patch.object(self.pm.sup, "run_socket", lambda ws, w: SOCK),
            mock.patch.object(self.pm.sup, "set_paused", lambda ws, w, v: self.calls.append(("paused", v))),
            mock.patch.object(self.pm.sw, "session_state", lambda *a, **k: "exists"),
            mock.patch.object(self.pm, "_take_down", lambda ws, w, r: self.calls.append("down") or
                              {"down": True, "sessions": {}, "closed_runs": []}),
            mock.patch.object(self.pm.rem, "restart", lambda ws, repo, w, **k: self.calls.append("restart") or
                              {"result": "restarted", "detail": {}}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_live_seat_is_taken_down_unpaused_and_restarted(self):
        from unittest import mock
        with mock.patch.object(self.pm.sup, "is_paused", lambda ws, w: False):
            out = self.pm.retry("ws", REPO, WID)
        self.assertEqual(out["result"], "restarted")
        self.assertEqual(self.calls, ["down", ("paused", False), "restart", ("paused", False)])

    def test_a_stopped_worker_is_not_restarted(self):
        from unittest import mock
        with mock.patch.object(self.pm.sup, "is_paused", lambda ws, w: True):
            out = self.pm.retry("ws", REPO, WID)
        self.assertEqual(out["result"], "failed")
        self.assertEqual(self.calls, [])


class RetryIsDetachedAndTransactional(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.pm = _load("pool_manage", SCRIPTS / "pool_manage.py")
        self.ws = Path(tempfile.mkdtemp(prefix="pool-retry-tx-"))
        (self.ws / "results").mkdir()
        self.paused = []
        for p in (mock.patch.object(self.pm, "_worker", lambda ws, t: (WID, {"state": "active", "name": "Mira"})),
                  mock.patch.object(self.pm.sup, "is_paused", lambda ws, w: False),
                  mock.patch.object(self.pm.sup, "run_socket", lambda ws, w: SOCK),
                  mock.patch.object(self.pm.sup, "set_paused", lambda ws, w, v: self.paused.append(v)),
                  mock.patch.object(self.pm.sw, "session_state", lambda *a, **k: "exists")):
            p.start()
            self.addCleanup(p.stop)

    def test_the_verb_hands_the_restart_to_a_detached_child_and_returns(self):
        seen = []
        out = self.pm.dispatch_retry(self.ws, REPO, WID, popen=lambda argv, **kw: seen.append((argv, kw)))
        self.assertEqual(out["result"], "dispatched")
        [(argv, kw)] = seen
        self.assertTrue(kw["start_new_session"], "the child must outlive the session that asked")
        self.assertEqual(argv[-5:-2], ["retry", WID, "--in-place"])
        self.assertEqual(self.paused, [], "nothing is taken down by the asking process")

    def test_a_restart_that_fails_or_raises_never_leaves_the_seat_paused_and_is_told(self):
        from unittest import mock
        down = lambda ws, w, r: (self.paused.append(True), {"down": True, "sessions": {}, "closed_runs": []})[1]  # noqa: E731
        with mock.patch.object(self.pm, "_take_down", down), \
                mock.patch.object(self.pm.rem, "restart", lambda *a, **k: {"result": "failed", "detail": {}}):
            out = self.pm.retry(self.ws, REPO, WID)
        self.assertEqual((out["result"], self.paused[-1]), ("failed", False))
        [note] = list((self.ws / "results").glob("proactive-*.txt"))
        self.assertIn("could not restart Mira", note.read_text())
        self.paused.clear()
        with mock.patch.object(self.pm, "_take_down", down), \
                mock.patch.object(self.pm.rem, "restart", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.pm.retry(self.ws, REPO, WID)
        self.assertEqual(self.paused[-1], False)

    def test_a_busy_pool_lock_is_a_failed_retry_the_owner_is_told_about(self):
        from unittest import mock

        def busy(*a, **k):
            raise self.pm.rem.PoolBusy("held by a sweep")
        with mock.patch.object(self.pm.rem, "pool_lock", busy), \
                mock.patch.object(self.pm.rem, "restart", lambda *a, **k: self.fail("restarted under a busy lock")):
            out = self.pm.retry(self.ws, REPO, WID)
        self.assertEqual((out["result"], self.paused[-1:]), ("failed", [False]))
        [note] = list((self.ws / "results").glob("proactive-*.txt"))
        self.assertIn("could not restart Mira: the pool was busy", note.read_text())

    def test_a_take_down_that_cannot_run_is_a_failed_retry_with_a_fixed_reason(self):
        from unittest import mock
        with mock.patch.object(self.pm, "_take_down", side_effect=OSError("/some/private/path: denied")):
            out = self.pm.retry(self.ws, REPO, WID)
        self.assertEqual((out["result"], out["why"]), ("failed", "the seat could not be taken down or started"))
        [note] = list((self.ws / "results").glob("proactive-*.txt"))
        self.assertNotIn("/some/private/path", note.read_text(), "the error's own text never reaches the owner")

    def test_a_cards_retry_reports_under_its_key_and_the_card_is_the_one_surface(self):
        from unittest import mock
        sys.path.insert(0, str(REPO / "src"))
        import detached_action
        down = lambda ws, w, r: {"down": True, "sessions": {}, "closed_runs": []}  # noqa: E731
        for restarted, outcome in (("restarted", detached_action.DONE), ("failed", detached_action.FAILED)):
            with self.subTest(restarted=restarted):
                key = f"restart:hitl_x:{restarted}"
                detached_action.begin(self.ws, key)
                with mock.patch.dict(os.environ, {detached_action.KEY_ENV: key}), \
                        mock.patch.object(self.pm, "_take_down", down), \
                        mock.patch.object(self.pm.rem, "restart", lambda *a, **k: {"result": restarted, "detail": {}}):
                    self.pm.retry(self.ws, REPO, WID)
                got = detached_action.status(self.ws, key)
                self.assertEqual(got.state, outcome)
                if outcome == detached_action.FAILED:
                    self.assertIn("could not restart Mira", got.reason)
                self.assertEqual(list((self.ws / "results").glob("proactive-*.txt")), [], "no second surface")

    def test_a_cards_retry_waits_for_its_watcher_to_confirm_the_tap_before_any_take_down(self):
        from unittest import mock
        sys.path.insert(0, str(REPO / "src"))
        import detached_action
        import recovery_gate
        key, gate = "restart:hitl_h:1", "card:hitl_h:retry_now:1"
        detached_action.begin(self.ws, key)
        recovery_gate.admit(self.ws, gate, 10, now=10)  # accepted, never finished: the watcher died
        env = {detached_action.KEY_ENV: key, detached_action.GATE_ENV: gate}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(detached_action, "HANDOFF_WAIT_S", 0.0), \
                mock.patch.object(self.pm, "_handoff_confirmed",
                                  lambda ws, g: detached_action.wait_for_handoff(ws, g, wait_s=0)), \
                mock.patch.object(self.pm, "_take_down", lambda *a: self.fail("took the seat down")):
            self.assertEqual(self.pm.retry(self.ws, REPO, WID)["result"], "failed")
        self.assertEqual(detached_action.status(self.ws, key), (detached_action.FAILED, detached_action.HANDOFF, None))
        self.assertEqual(self.paused, [])
        self.assertEqual(list((self.ws / "results").glob("proactive-*.txt")), [], "the card says it, not a DM")
        recovery_gate.finish(self.ws, gate, "done", now=11)
        key2 = "restart:hitl_h:2"
        detached_action.begin(self.ws, key2)
        down = lambda ws, w, r: {"down": True, "sessions": {}, "closed_runs": []}  # noqa: E731
        with mock.patch.dict(os.environ, {detached_action.KEY_ENV: key2, detached_action.GATE_ENV: gate}), \
                mock.patch.object(self.pm, "_take_down", down), \
                mock.patch.object(self.pm.rem, "restart", lambda *a, **k: {"result": "restarted", "detail": {}}):
            self.assertEqual(self.pm.retry(self.ws, REPO, WID)["result"], "restarted", "confirmed: it runs")

    def test_a_child_whose_tap_already_ended_takes_nothing_down(self):
        from unittest import mock
        sys.path.insert(0, str(REPO / "src"))
        import detached_action
        key = "restart:hitl_y:1"
        detached_action.begin(self.ws, key, now=1.0, deadline_s=1)  # long past its deadline
        with mock.patch.dict(os.environ, {detached_action.KEY_ENV: key}), \
                mock.patch.object(self.pm, "_take_down", lambda *a: self.fail("took the seat down")):
            self.assertEqual(self.pm.retry(self.ws, REPO, WID)["result"], "failed")
        self.assertEqual(self.paused, [])

    def test_an_unexpected_error_still_ends_the_cards_retry(self):
        from unittest import mock
        sys.path.insert(0, str(REPO / "src"))
        import detached_action
        key = "restart:hitl_z:1"
        detached_action.begin(self.ws, key)
        with mock.patch.dict(os.environ, {detached_action.KEY_ENV: key}), \
                mock.patch.object(self.pm, "_take_down", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            self.pm.retry(self.ws, REPO, WID)
        self.assertEqual(detached_action.status(self.ws, key).state, detached_action.FAILED)
        self.assertEqual(self.paused[-1], False)

    def test_a_stopped_worker_is_not_dispatched(self):
        from unittest import mock
        with mock.patch.object(self.pm.sup, "is_paused", lambda ws, w: True):
            self.assertEqual(self.pm.dispatch_retry(self.ws, REPO, WID, popen=lambda *a, **k: None)["result"],
                             "failed")


class SeatOnTheCard(unittest.TestCase):
    def manager(self):
        from hitl.manager import HitlManager, HitlStore
        return HitlManager(HitlStore(Path(tempfile.mkdtemp(prefix="hitl-seat-"))))

    def test_a_seat_names_itself_and_its_terminal(self):
        req = ciw.escalate(self.manager(), "blocked-human", "awaiting user: selection", "selection",
                           "Pick one:\n❯ 1. Stop and wait\n  2. Switch\n", NAME, seat="worker comm")
        self.assertEqual(req.device, {"id": NAME, "name": "worker comm"})
        self.assertTrue(req.title.startswith("worker comm · "), req.title)
        self.assertIn(NAME, req.message)
        self.assertEqual((req.subject or {}).get("seat"), "worker comm")
        jump = [a for a in req.actions if a.kind == "open_terminal"]
        self.assertEqual([a.label for a in jump], [f"Open terminal ({NAME})"])

    def test_without_a_seat_the_core_card_is_unchanged(self):
        req = ciw.escalate(self.manager(), "blocked-human", "d", "selection", "Pick one:", "sutando-core")
        self.assertEqual(req.device, {"id": "sutando-core", "name": "sutando-core"})
        self.assertNotIn("seat", req.subject or {})
        self.assertEqual([a.label for a in req.actions if a.kind == "open_terminal"], ["Open terminal"])

    def test_two_seats_on_one_dialog_are_two_cards(self):
        m = self.manager()
        a = ciw.escalate(m, "blocked-human", "d", "selection", "Pick one:", NAME, seat="worker comm")
        b = ciw.escalate(m, "blocked-human", "d", "selection", "Pick one:", "sutando-worker-b", seat="worker b")
        self.assertNotEqual(a.id, b.id)


if __name__ == "__main__":
    unittest.main()
