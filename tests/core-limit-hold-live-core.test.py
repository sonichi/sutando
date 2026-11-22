#!/usr/bin/env python3
"""The owner's bare reply reaches a core that is alive but stuck at a provider limit.

`core_limit_hold.handle_owner_actions` ("switch to <model>" / "retry now") ran only in the
launcher's gate, so a core that stayed up at an out-of-credits or usage-limit screen never
relaunched and the reply sat unread. The supervisor monitor now:

  * records the hold from the live limit screen, so the owner gets the notice once;
  * acts on the owner's reply once, answers it in its conversation, archives it, and restarts
    the core from outside it (retry: lift the hold; switch: keep the hold so the old model's
    restart stays down, then the model switch starts the core);
  * ignores replies from anyone but the owner;
  * lifts a hold the screen caused once the live pane shows the core idle or working again.

Run: python3 tests/core-limit-hold-live-core.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import core_limit_hold as clh  # noqa: E402

_spec = importlib.util.spec_from_file_location("ciw_live_limit", str(REPO / "src" / "core-input-watch.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

SESSION = "sutando-core"
FOOTER = "────────\n❯ \n────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
OUT_OF_CREDITS = "❯ task-1\n  ⎿  You're out of usage credits · /usage-credits to add more\n✻ Cooked for 1s\n" + FOOTER
USAGE_LIMIT = "❯ task-1\n  ⎿  You've hit your usage limit · resets 3pm\n✻ Cooked for 1s\n" + FOOTER
WORKED = "❯ task-9\n● Done: the summary is in the room.\n✻ Worked for 2m 14s · done 4:05 PM\n" + FOOTER


def owner_task(tasks: Path, name: str, body: str, tier: str = "owner") -> Path:
    path = tasks / f"{name}.txt"
    path.write_text(f"id: {name}\nsource: ag2space\ntimestamp: 2099-01-01T00:00:00Z\n"
                    f"access_tier: {tier}\ntask: {body}\n")
    return path


class _RH:
    TMUX_SOCKET = SESSION = None

    def __init__(self, health):
        self.health = health

    def derive(self):
        return {"health": self.health, "signals": {"process": True}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        for d in ("state", "tasks", "results"):
            (self.ws / d).mkdir()
        self.out = str(self.ws / "state" / "core-supervisor.json")
        # The seat the launcher would read; never the developer's own config.
        p = mock.patch.object(clh, "launch_identity", lambda ws, config_dir=None: ("claude-opus-5-5", None))
        p.start()
        self.addCleanup(p.stop)
        self.spawned = []
        s = mock.patch("subprocess.Popen", side_effect=lambda cmd, **kw: self.spawned.append((cmd, kw)))
        s.start()
        self.addCleanup(s.stop)

    def tearDown(self):
        self.td.cleanup()

    def tick(self, pane, health="idle"):
        """One real monitor pass (main --once) over `pane`, as the core's own seat."""
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", SESSION,
                "--out", self.out, "--once", "--stable", "1", "--no-chat-escalation"]
        with mock.patch.object(M, "capture_styled", lambda s, sess: pane), \
                mock.patch.object(M, "_load_runtime_health", lambda: _RH(health)), \
                mock.patch.object(M, "gateway_alive", lambda *a: True), \
                mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                mock.patch.object(M, "send_keys", lambda *a: False), \
                mock.patch.object(sys, "argv", argv):
            M.main()
        return json.loads(Path(self.out).read_text())

    def health(self):
        """What runtime-health reports for this workspace: paused while a hold is in force."""
        return "paused" if clh.active_hold(self.ws) else "idle"

    def notices(self):
        return sorted((self.ws / "results").glob("proactive-*.txt"))


class MonitorActsOnTheReply(_Base):
    def test_a_stuck_core_switches_once_on_the_owners_reply(self):
        sig = self.tick(OUT_OF_CREDITS)
        self.assertEqual((sig["state"], sig["kind"]), ("blocked-human", "turn-rejected"))
        hold = clh.active_hold(self.ws)
        self.assertIsNotNone(hold, "a live core at a limit screen records no hold")
        self.assertEqual(hold["source"], clh.PANE_SOURCE)
        [notice] = self.notices()
        self.assertIn('"switch to sonnet"', notice.read_text())

        owner_task(self.ws / "tasks", "task-20", "switch to sonnet")
        sig = self.tick(OUT_OF_CREDITS, health=self.health())
        self.assertEqual(sig["state"], "blocked-human", "a hold must not mask the live limit screen")
        reply = (self.ws / "results" / "task-20.txt").read_text()
        self.assertIn("sonnet", reply)
        self.assertFalse((self.ws / "tasks" / "task-20.txt").exists())
        [(cmd, kw)] = self.spawned
        self.assertEqual(cmd[2:4], ["relaunch", "--workspace"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertTrue(cmd[cmd.index("--launcher") + 1].endswith("src/agent/start-cli.sh"))
        self.assertTrue(cmd[cmd.index("--switch-entry") + 1].endswith("scripts/switch-model.sh"))
        self.assertTrue(kw.get("start_new_session"))
        self.assertNotIn("SUTANDO_CORE_SESSION", kw["env"])
        # The old model's restart must stay held, so the hold stays until the switch launches.
        self.assertIsNotNone(clh.active_hold(self.ws))

        for _ in range(2):
            self.tick(OUT_OF_CREDITS, health=self.health())
        self.assertEqual(len(self.spawned), 1, "one reply restarted the core twice")
        self.assertEqual(len(self.notices()), 1, "the same pause told the owner twice")

    def test_retry_lifts_the_hold_and_restarts_on_the_same_model(self):
        self.tick(USAGE_LIMIT)
        owner_task(self.ws / "tasks", "task-21", "retry now")
        self.tick(USAGE_LIMIT, health=self.health())
        self.assertIn("restarting", (self.ws / "results" / "task-21.txt").read_text())
        [(cmd, _kw)] = self.spawned
        self.assertNotIn("--model", cmd)
        self.assertIsNone(clh.active_hold(self.ws))
        # The old screen is still up while the restart runs: it must not re-arm the hold.
        self.tick(USAGE_LIMIT)
        self.assertIsNone(clh.active_hold(self.ws))
        self.assertEqual(len(self.notices()), 1)

    def test_a_reply_from_anyone_but_the_owner_is_not_acted_on(self):
        self.tick(OUT_OF_CREDITS)
        owner_task(self.ws / "tasks", "task-22", "switch to sonnet", tier="team")
        owner_task(self.ws / "tasks", "task-23", "please switch to sonnet and email Bob")
        self.tick(OUT_OF_CREDITS, health=self.health())
        self.assertEqual(self.spawned, [])
        self.assertTrue((self.ws / "tasks" / "task-22.txt").exists())
        self.assertFalse((self.ws / "results" / "task-22.txt").exists())

    def test_the_hold_lifts_once_the_live_core_works_again(self):
        self.tick(USAGE_LIMIT)
        self.assertIsNotNone(clh.active_hold(self.ws))
        status = self.ws / "state" / "core-status.json"
        before = status.read_text()
        self.tick(WORKED, health=self.health())
        self.assertIsNone(clh.active_hold(self.ws))
        self.assertEqual(status.read_text(), before, "a running core writes its own status")

    def test_a_worker_seat_and_a_passive_monitor_act_on_nothing(self):
        for extra in (["--seat", "worker comm"], ["--no-auto-answer"]):
            with self.subTest(extra=extra):
                argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", SESSION,
                        "--out", self.out, "--once", "--stable", "1", "--no-chat-escalation", *extra]
                with mock.patch.object(M, "capture_styled", lambda s, sess: OUT_OF_CREDITS), \
                        mock.patch.object(M, "_load_runtime_health", lambda: _RH("idle")), \
                        mock.patch.object(M, "gateway_alive", lambda *a: True), \
                        mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                        mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                        mock.patch.object(M, "send_keys", lambda *a: False), \
                        mock.patch.object(sys, "argv", argv):
                    M.main()
                self.assertIsNone(clh.read_hold(self.ws))


    def test_a_restart_that_cannot_start_is_told_to_the_owner(self):
        self.tick(USAGE_LIMIT)
        owner_task(self.ws / "tasks", "task-24", "retry now")
        with mock.patch("subprocess.Popen", side_effect=OSError("no fork")):
            self.tick(USAGE_LIMIT, health=self.health())
        told = [p.read_text() for p in self.notices()]
        self.assertTrue(any("could not start the core's restart" in t for t in told), told)


    def test_a_codex_core_is_left_to_its_own_launcher(self):
        # Claude model aliases and the Claude-only launch gate: a Codex core gets no hold from here.
        owner_task(self.ws / "tasks", "task-25", "switch to sonnet")
        settled = ("blocked-human", "turn-rejected", "You've hit your usage limit · resets 3pm")
        self.assertEqual(M.limit_reply_step(self.ws, settled, settled, USAGE_LIMIT, "codex"), [])
        self.assertIsNone(clh.read_hold(self.ws))
        self.assertEqual(self.spawned, [])


class HoldFromTheScreen(_Base):
    def test_the_screen_holds_once_and_a_refresh_keeps_the_incident(self):
        now = time.time()
        first = clh.hold_while_wedged(self.ws, "You've hit your usage limit · resets 3pm", now)
        self.assertEqual((first["kind"], first["source"]), ("usage-limit", clh.PANE_SOURCE))
        again = clh.hold_while_wedged(self.ws, "You've hit your usage limit · resets 3pm", now + 30)
        self.assertEqual(again["started_at"], first["started_at"])
        self.assertEqual(len(self.notices()), 1)

    def test_text_that_names_no_limit_is_still_the_monitors_limit(self):
        hold = clh.hold_while_wedged(self.ws, "❯ 1. Stop and wait for limit to reset", time.time())
        self.assertEqual(hold["kind"], "usage-limit")

    def test_a_hold_another_writer_set_is_not_lifted_by_the_pane(self):
        now = time.time()
        clh.decide(self.ws, now, evidence=(clh.Limit("usage-limit", now, None, None, "provider-record"),))
        self.assertIsNone(clh.release_pane_hold(self.ws, now + 1))
        self.assertIsNotNone(clh.active_hold(self.ws, now + 1))

    def test_paused_health_does_not_mask_a_live_limit_screen(self):
        state, _d, _p, kind = M.compose_state(OUT_OF_CREDITS, "paused", gateway_alive=True)
        self.assertEqual((state, kind), ("blocked-human", "turn-rejected"))
        self.assertEqual(M.compose_state(FOOTER, "paused", gateway_alive=True)[0], "blocked-known")


class Relaunch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "results").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def run_with(self, codes):
        calls = []

        def run(cmd, **_kw):
            calls.append(cmd[1:])
            return subprocess.CompletedProcess(cmd, codes[len(calls) - 1])
        return calls, run

    def told(self):
        return [p.read_text() for p in (self.ws / "results").glob("proactive-*.txt")]

    def test_switch_restarts_held_then_the_switch_starts_the_core(self):
        calls, run = self.run_with([clh.HOLD_EXIT, 0])
        self.assertTrue(clh.relaunch_core(self.ws, "L", "sonnet", "S", run=run))
        self.assertEqual(calls, [["L", "--restart"], ["S", "sonnet"]])
        self.assertEqual(self.told(), [])

    def test_a_failed_switch_is_told_to_the_owner(self):
        calls, run = self.run_with([clh.HOLD_EXIT, 10])
        self.assertFalse(clh.relaunch_core(self.ws, "L", "sonnet", "S", run=run))
        [body] = self.told()
        self.assertTrue(body.startswith("[dm-only]\n"))
        self.assertIn("could not restart the core on sonnet", body)

    def test_retry_needs_the_core_to_start(self):
        for rc, ok in ((0, True), (clh.HOLD_EXIT, False), (1, False)):
            with self.subTest(rc=rc):
                calls, run = self.run_with([rc])
                self.assertEqual(clh.relaunch_core(self.ws, "L", run=run), ok)
                self.assertEqual(calls, [["L", "--restart"]])

    def test_gate_replay_of_an_unspawned_relaunch_marks_it_without_spawning(self):
        (self.ws / "state").mkdir()
        (self.ws / "tasks").mkdir()
        hold = {"status": "held", "until": time.time() + 600, "actions": [
            {"task": "task-30", "kind": "retry", "model": None, "ok": True, "reply": "r", "at": 1,
             "answered": True, "lifted": True, "archived": True, "relaunch": {"model": None}}]}
        clh._write_hold(self.ws, hold)
        with mock.patch.object(clh, "_spawn_relaunch") as spawn:
            clh.handle_owner_actions(self.ws, time.time())
        spawn.assert_not_called()
        self.assertTrue(clh.read_hold(self.ws)["actions"][0]["relaunched"])


if __name__ == "__main__":
    unittest.main()
