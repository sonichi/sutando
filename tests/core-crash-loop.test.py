#!/usr/bin/env python3
"""A crash loop reaches the owner as one card per incident (P0-6).

Before: the backend supervisor's spent recovery budget (`terminal` in supervisor-status.json)
and a core that keeps dying never left the host. Now the core's monitor raises one core-blocked
card per incident: the service and its last error, or, when the recovery record says
`crash_loop` for the core (Claude Code or Codex), the restart count and Restart core. A stale
status file, another app session's terminal, or a core that is up again raises nothing and
closes what is open.
"""
import importlib.util as u
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_crash", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

import core_crash_loop as C
import core_recovery_state as crs
from hitl.manager import HitlManager, HitlStore
from hitl.schema import STATUS_RESOLVED

CORE = "sutando-core"
RUN = "run-3f6b1c2a"


def status(now, terminal=True, session=RUN, beat_age=2.0, error="exited (code 1): TypeError: x is undefined"):
    svc = {"recoveryBudget": {"used": 5, "cap": 5, "windowEndsAt": None},
           "lifecycle": {"verdict": "failed", "reason": error}}
    if terminal:
        svc["terminal"] = {"state": "failed", "reason": "crash-loop", "appSessionId": session}
    return {"schemaVersion": 1, "heartbeatAt": int((now - beat_age) * 1000), "appSessionId": RUN,
            "services": {"voice-agent": svc, "agent-api": {"recoveryBudget": {"used": 0, "cap": 5}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "state").mkdir()
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, doc):
        (self.ws / "state" / C.STATUS_FILE).write_text(json.dumps(doc))

    def tick(self, now=None, runtime="claude", pane=None):
        return M.crash_loop_step(self.mgr, self.ws, CORE, runtime, pane, time.time() if now is None else now)


class SupervisorTerminal(Base):
    def test_a_spent_budget_raises_one_card_with_the_service_and_its_last_error(self):
        now = time.time()
        self.write(status(now))
        [a] = self.tick(now=now)
        [b] = self.tick(now=now + 3)
        self.assertEqual(a.id, b.id, "one card per incident")
        self.assertEqual(a.kind, "core-blocked")
        self.assertEqual(a.subject["crash_loop"], "voice-agent")
        self.assertIn("voice-agent keeps crashing", a.message)
        self.assertIn("voice-agent keeps crashing, so the app stopped restarting it (crash-loop).", a.message)
        self.assertIn("Last error: exited with an error", a.message, "named by its kind, never quoted")
        self.assertNotIn("TypeError", a.message)
        self.assertIn("until the s.space app restarts", a.message)
        self.assertEqual(a.actions, [], "no button: nothing on the card can clear a terminal service")

    def test_a_secret_or_a_path_in_the_supervisors_error_never_reaches_the_card(self):
        now = time.time()
        for error, kind in (("spawn /Users/someone/app/bin/node ENOENT token=sk-ant-oat01-SECRETSECRET",
                             "missing file or module"),
                            ("Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789", "not recorded"),
                            ("JavaScript heap out of memory at /home/x/y.js", "out of memory")):
            with self.subTest(kind=kind):
                self.write(status(now, error=error))
                [card] = self.tick(now=now)
                self.assertIn(f"Last error: {kind}", card.message)
                for leak in ("/Users/someone", "sk-ant", "ghp_", "/home/x", "Bearer"):
                    self.assertNotIn(leak, card.message)
                self.mgr.resolve(card.id)
        doc = status(now)
        doc["services"]["voice-agent"]["terminal"]["reason"] = "gave up: /Users/someone/x"
        doc["services"]["../etc"] = doc["services"].pop("voice-agent")
        self.write(doc)
        [card] = self.tick(now=now + 1)
        self.assertNotIn("/Users/someone", card.message)
        self.assertIn("a service keeps crashing", card.message)

    def test_stale_status_or_another_session_raises_nothing(self):
        now = time.time()
        self.write(status(now, beat_age=C.STATUS_FRESH_S + 5))
        self.assertEqual(self.tick(now=now), [])
        self.write(status(now, session="run-older"))
        self.assertEqual(self.tick(now=now), [])
        (self.ws / "state" / C.STATUS_FILE).write_text("{not json")
        self.assertEqual(self.tick(now=now), [])

    def test_a_fresh_app_session_closes_the_card(self):
        now = time.time()
        self.write(status(now))
        [a] = self.tick(now=now)
        self.write(status(now + 10, terminal=False))
        self.assertEqual(self.tick(now=now + 10), [])
        self.assertEqual(self.mgr.get(a.id).status, STATUS_RESOLVED)


class CoreCrashes(Base):
    def record(self, runtime="claude", restarts=5, sup="crashed", now=None):
        now = time.time() if now is None else now
        rec = crs.derive({"state": sup}, None, None, None, None, now=now, runtime=runtime,
                         restarts_10m=restarts, prev=crs.read(self.ws, now, max_age_s=None))
        crs.publish(self.ws, rec, now)
        return rec

    def test_the_recovery_records_crash_loop_raises_one_card_with_restart_core(self):
        self.assertEqual(self.record(restarts=crs.CRASH_LOOP_RESTARTS - 1)["state"], "starting")
        self.assertEqual(self.tick(), [], "a few restarts are the supervisor's job, not the owner's")
        rec = self.record()
        [card] = self.tick()
        self.assertEqual([a.id for a in card.actions], ["restart_core"])
        self.assertEqual(card.subject["incident"], rec["incident"], "one incident, the record's")
        self.assertIn("started 5 times in the last 10 minutes", card.message)
        self.record(restarts=6)
        [again] = self.tick()
        self.assertEqual(again.id, card.id, "the loop going on is the same card")

    def test_a_codex_core_crash_loop_is_carded_the_same_way(self):
        self.record(runtime="codex")
        [card] = self.tick(runtime="codex")
        self.assertEqual((card.runtime, [a.id for a in card.actions]), ("codex", ["restart_core"]))

    def test_the_last_error_is_named_never_quoted(self):
        self.record()
        [card] = self.tick(pane="API Error: 500 {\"error\":\"overloaded\"} token=sk-ant-oat01-SECRET")
        self.assertIn("Last error: api error", card.message)
        self.assertNotIn("sk-ant", card.message)

    def test_the_card_closes_once_the_core_is_up(self):
        self.record()
        [card] = self.tick()
        self.record(sup="running", restarts=0)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.mgr.get(card.id).status, STATUS_RESOLVED)

    def test_a_seat_monitor_never_raises_it(self):
        # Behaviour, not source text: one monitor tick (--once) of a seat on a host whose recovery
        # record says crash_loop calls no crash-loop step; the core's tick does.
        import os
        from unittest import mock
        self.record()
        calls = []

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "ok"}

        def tick(*extra):
            argv = ["core-input-watch.py", "--socket", "/nonexistent.sock", "--once",
                    "--out", str(self.ws / "state" / "core-supervisor.json"), *extra]
            with mock.patch.object(M, "crash_loop_step", lambda *a, **k: calls.append(a[2]) or []), \
                    mock.patch.object(M, "capture_styled", lambda *a: "❯ \n"), \
                    mock.patch.object(M, "_load_runtime_health", lambda: _RH()), \
                    mock.patch.object(M, "gateway_alive", lambda *a: True), \
                    mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                    mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                    mock.patch.object(M, "send_keys", lambda *a: False), \
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.ws / "cfg")}), \
                    mock.patch.object(sys, "argv", argv):
                M.main()
        tick("--session", "sutando-worker-b35461a4", "--seat", "worker Mira (b35461a4)")
        self.assertEqual(calls, [], "a seat's monitor ran the crash-loop step")
        tick("--session", CORE)
        self.assertEqual(calls, [CORE], "the core's monitor runs it")

if __name__ == "__main__":
    unittest.main()
