#!/usr/bin/env python3
"""The core's recovery record (src/core_recovery_state.py): every failure state derived from the
records that already exist, the one atomic writer of state/core-recovery.json, the monitor tick
that calls it, and the literal contract fixtures under contracts/core_recovery.v1/.

Run: python3 tests/core-recovery-state.test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
os.environ["SUTANDO_TEST_MODE"] = "1"

import cli_wedge
import core_recovery_state as crs
from hitl.replies import REPLY_FIELD
from hitl.schema import ActionReply

FIXTURES = REPO / "contracts" / "core_recovery.v1"
NOW = 1763730120.0
HOLD = {"status": "held", "kind": "usage-limit", "model": "opus", "reset_at": 1763737200,
        "until": 1763737260, "started_at": 1763730000, "account": "3f9a2b0c1d2e4f56"}


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def derive(sup=None, hold=None, status=None, quota=None, wedge=None, **kw):
    return crs.derive(sup, hold, status, quota, wedge, now=kw.pop("now", NOW), **kw)


class States(unittest.TestCase):
    """One row per state the spec's failure table names."""

    def test_running_and_idle(self):
        self.assertEqual(derive({"state": "running"})["state"], "running")
        self.assertEqual(derive({"state": "idle-ready"})["state"], "idle")
        self.assertEqual(derive({"state": "running"})["actions"], [])

    def test_a_usage_limit_on_the_pane_is_paused(self):
        rec = derive({"state": "blocked-human", "kind": "usage-limit",
                      "prompt": "You've hit your usage limit · resets 3pm"}, runtime="claude",
                     runtimes_available=("claude", "codex"))
        self.assertEqual(rec["state"], "paused_limit")
        self.assertEqual(rec["actions"], ["retry_now", "switch_model", "switch_runtime", "restart_core"])
        self.assertEqual(rec["switch_runtimes"], ["codex"])

    def test_a_turn_refused_for_a_limit_is_paused(self):
        rec = derive({"state": "blocked-human", "kind": "turn-rejected",
                      "prompt": "You've hit your session limit"})
        self.assertEqual(rec["state"], "paused_limit")

    def test_a_held_relaunch_is_paused_with_its_reset_time(self):
        rec = derive({"state": "crashed"}, HOLD, restarts_10m=9)
        self.assertEqual((rec["state"], rec["reset_at"], rec["since"]), ("paused_limit", 1763737200, 1763730000))

    def test_an_expired_hold_is_not_a_pause(self):
        self.assertEqual(derive({"state": "crashed"}, dict(HOLD, until=NOW - 1))["state"], "starting")

    def test_a_credit_refusal_on_the_pane_is_out_of_credits(self):
        rec = derive({"state": "blocked-human", "kind": "usage-limit",
                      "prompt": "You're out of usage credits. Add more to keep going"})
        self.assertEqual(rec["state"], "out_of_credits")

    def test_a_credit_refusal_in_the_proxy_record_is_out_of_credits(self):
        quota = {"recent_rejections": [{"status": 429, "ts": "2025-11-21T13:00:00Z",
                                        "snippet": "Your credit balance is too low to access the API"}]}
        now = 1763730000.0 + 60  # 2025-11-21T13:01:00Z
        self.assertEqual(derive({"state": "crashed"}, dict(HOLD, until=now + 600), quota=quota, now=now)["state"],
                         "out_of_credits")
        stale = now + crs.QUOTA_EVIDENCE_WINDOW_S + 60
        self.assertEqual(derive({"state": "crashed"}, dict(HOLD, until=stale + 600), quota=quota, now=stale)["state"],
                         "paused_limit", "an old refusal says nothing about this one")

    def test_a_credit_banner_the_wedge_read_is_out_of_credits(self):
        wedge = cli_wedge.frame_abnormal("Out of credits — add credits to continue")
        self.assertIsNotNone(wedge)
        self.assertEqual(derive({"state": "running"}, wedge=wedge)["state"], "running",
                         "a running core is running, whatever text its transcript holds")
        self.assertEqual(derive({"state": "hung"}, wedge=wedge)["state"], "out_of_credits")

    def test_signed_out(self):
        self.assertEqual(derive({"state": "logged-out"})["state"], "signed_out")
        rec = derive({"state": "blocked-human", "kind": "login", "prompt": "Select login method"})
        self.assertEqual((rec["state"], rec["actions"]), ("signed_out", ["restart_core"]))

    def test_an_unrecognised_prompt_is_blocked(self):
        rec = derive({"state": "blocked-human", "kind": "selection", "prompt": "❯ 1. Allow\n  2. Deny"})
        self.assertEqual((rec["state"], rec["actions"]), ("blocked_prompt", ["escape_prompt", "restart_core"]))

    def test_a_gate_the_monitor_answers_itself_is_not_a_block(self):
        self.assertEqual(derive({"state": "blocked-known", "kind": "press-enter"})["state"], "idle")

    def test_one_crash_is_starting_and_a_loop_is_a_crash_loop(self):
        self.assertEqual(derive({"state": "crashed"}, restarts_10m=1)["state"], "starting")
        self.assertEqual(derive({"state": "crashed"}, restarts_10m=crs.CRASH_LOOP_RESTARTS - 1)["state"], "starting")
        rec = derive({"state": "crashed"}, restarts_10m=crs.CRASH_LOOP_RESTARTS)
        self.assertEqual((rec["state"], rec["actions"]), ("crash_loop", ["restart_core"]))

    def test_a_spent_recovery_budget_is_a_crash_loop(self):
        status = {"services": {"credential-proxy": {"terminal": {"state": "failed", "reason": "crash-loop"}}}}
        self.assertEqual(derive({"state": "crashed"}, status=status, restarts_10m=0)["state"], "crash_loop")
        self.assertEqual(derive({"state": "running"}, status=status)["state"], "running")

    def test_what_cannot_be_read_is_unknown(self):
        for sup in (None, {}, {"state": "hung"}, {"state": "unobserved"}, {"state": "gateway-down"},
                    {"state": "a-state-from-the-future"}, ["not", "a", "dict"]):
            self.assertEqual(derive(sup)["state"], "unknown", sup)

    def test_every_state_is_reachable(self):
        seen = {derive({"state": "running"})["state"], derive({"state": "idle-ready"})["state"],
                derive({"state": "crashed"}, HOLD)["state"],
                derive({"state": "blocked-human", "kind": "usage-limit", "prompt": "out of credits"})["state"],
                derive({"state": "logged-out"})["state"], derive({"state": "blocked-human", "kind": "x"})["state"],
                derive({"state": "crashed"}, restarts_10m=9)["state"], derive({"state": "crashed"})["state"],
                derive(None)["state"]}
        self.assertEqual(seen, set(crs.STATES))


CODEX_LIMIT = "You've hit your usage limit. Upgrade to Pro, or try again in 2 hours."
CODEX_IDLE = "\n\u203a Ask Codex to do anything\n\n  gpt-5-codex high \u00b7 100% left \u00b7 ~/x\n"


class Codex(unittest.TestCase):
    """Parity: the same states from a Codex core, with only the actions its host side can carry out."""

    def test_a_codex_limit_leaves_the_idle_prompt_and_is_still_a_pause(self):
        rec = derive({"state": "idle-ready"}, wedge=cli_wedge.frame_abnormal(CODEX_LIMIT), runtime="codex",
                     runtimes_available=("claude", "codex"))
        self.assertEqual((rec["state"], rec["actions"], rec["switch_runtimes"]),
                         ("paused_limit", ["switch_runtime", "restart_core"], ["claude"]))
        self.assertEqual(derive({"state": "idle-ready"}, wedge=cli_wedge.frame_abnormal(CODEX_LIMIT),
                                runtime="claude")["state"], "idle",
                         "a Claude refusal is the monitor's to read; its idle prompt stands")

    def test_a_codex_core_reads_no_claude_record(self):
        rec = derive({"state": "crashed"}, HOLD, runtime="codex", model="opus")
        self.assertEqual(rec["state"], "starting", "the Claude relaunch hold does not hold a Codex core")
        for key in ("model", "account", "reset_at"):
            self.assertNotIn(key, rec)

    def test_the_same_states_read_from_a_codex_core(self):
        for sup, want in (({"state": "running"}, "running"), ({"state": "idle-ready"}, "idle"),
                          ({"state": "logged-out"}, "signed_out"),
                          ({"state": "blocked-human", "kind": "selection"}, "blocked_prompt"),
                          ({"state": "hung"}, "unknown")):
            self.assertEqual(derive(sup, runtime="codex")["state"], want, sup)
        self.assertEqual(derive({"state": "crashed"}, runtime="codex", restarts_10m=5)["state"], "crash_loop")
        self.assertEqual(derive({"state": "blocked-human", "kind": "selection"}, runtime="codex")["actions"],
                         ["escape_prompt", "restart_core"])

    def test_a_banner_above_the_last_codex_answer_is_outrun(self):
        pane = CODEX_LIMIT + "\n\n\u2022 Done: the PR is open.\n" + CODEX_IDLE
        self.assertIsNone(cli_wedge.frame_abnormal(crs.codex_tail(pane)))
        self.assertIsNotNone(cli_wedge.frame_abnormal(crs.codex_tail("\u2022 Earlier answer\n" + CODEX_LIMIT + CODEX_IDLE)))

    def test_gather_falls_back_to_the_launchers_runtime_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "state").mkdir()
            (ws / "state" / "core-runtime.json").write_text(json.dumps({"runtime": "codex"}))
            with mock.patch.object(crs, "runtimes_on_host", lambda: ("codex",)):
                rec = crs.gather(ws, {"state": "idle-ready"}, now=NOW, runtime=None,
                                 pane="\u2022 Earlier answer\n" + CODEX_LIMIT + CODEX_IDLE)
            self.assertEqual((rec["runtime"], rec["state"], rec["actions"]), ("codex", "paused_limit", ["restart_core"]))

    def test_an_unknown_runtime_offers_only_the_neutral_actions(self):
        rec = derive({"state": "blocked-human", "kind": "usage-limit", "prompt": "hit your usage limit"},
                     runtime="agy", runtimes_available=("claude", "codex"))
        self.assertEqual((rec["actions"], "runtime" in rec), (["restart_core"], False))


class Fields(unittest.TestCase):
    def test_an_unknown_input_is_left_out_not_defaulted(self):
        rec = derive({"state": "blocked-human", "kind": "usage-limit", "prompt": "hit your usage limit"},
                     runtime="claude")
        for key in ("reset_at", "model", "account", "waiting_tasks", "restarts_10m", "switch_runtimes"):
            self.assertNotIn(key, rec)
        self.assertEqual(rec["switch_models"], ["opus", "sonnet", "haiku"])
        self.assertNotIn("switch_runtime", rec["actions"], "no other runtime on this host, no switch offered")

    def test_the_switch_list_leaves_out_the_running_and_the_limited_family(self):
        self.assertEqual(derive({"state": "crashed"}, HOLD, model="sonnet[1m]", runtime="claude")["switch_models"],
                         ["haiku"])
        self.assertNotIn("switch_models", derive({"state": "logged-out"}, model="opus", runtime="claude"))

    def test_since_and_incident_stand_while_the_state_does(self):
        first = derive({"state": "blocked-human", "kind": "selection"}, now=NOW)
        later = derive({"state": "blocked-human", "kind": "selection"}, now=NOW + 300, prev=first)
        self.assertEqual((later["since"], later["incident"]), (first["since"], first["incident"]))
        moved = derive({"state": "running"}, now=NOW + 400, prev=later)
        self.assertEqual(moved["since"], int(NOW + 400))
        self.assertNotEqual(moved["incident"], first["incident"])

    def test_a_pause_is_one_incident_across_monitor_restarts(self):
        a = derive({"state": "crashed"}, HOLD, now=NOW)
        b = derive({"state": "blocked-human", "kind": "usage-limit", "prompt": "hit your usage limit"},
                   HOLD, now=NOW + 900)
        self.assertEqual(a["incident"], b["incident"])

    def test_no_pane_text_or_account_identity_leaves_in_the_record(self):
        secret = "sk-ant-api03-SECRET /Users/someone/private.txt hit your usage limit"
        hold = dict(HOLD, account="3f9a2b0c1d2e4f56", reason="core paused: usage limit on opus")
        body = json.dumps(derive({"state": "blocked-human", "kind": "usage-limit", "prompt": secret,
                                  "detail": secret}, hold, runtime="claude", model="opus"))
        for leak in ("SECRET", "/Users", "private", "3f9a2b0c1d2e4f56", "core paused"):
            self.assertNotIn(leak, body)

    def test_coerce_keeps_every_field_inside_its_bounds(self):
        rec = crs.coerce({"v": 1, "state": "paused_limit", "incident": "i" * 500, "since": -5,
                          "reset_at": True, "model": "x" * 99, "waiting_tasks": 3.0,
                          "actions": ["retry_now", "rm -rf /", "restart_core"] + ["retry_now"] * 20,
                          "switch_models": ["sonnet", "gpt-9", "haiku"], "prompt": "pane text"})
        self.assertEqual(len(rec["incident"]), crs.MAX_STR)
        self.assertEqual(len(rec["model"]), crs.MAX_STR)
        self.assertNotIn("since", rec)
        self.assertNotIn("reset_at", rec)
        self.assertNotIn("prompt", rec)
        self.assertEqual(rec["waiting_tasks"], 3)
        self.assertEqual(rec["actions"][:2], ["retry_now", "restart_core"])
        self.assertEqual(len(rec["actions"]), crs.MAX_ACTIONS)
        self.assertEqual(rec["switch_models"], ["sonnet", "haiku"])
        for bad in (None, [], {"v": 2, "state": "running"}, {"v": 1, "state": "offline"}):
            self.assertIsNone(crs.coerce(bad), bad)


class Writer(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.path = self.ws / "state" / "core-recovery.json"

    def test_publish_writes_the_record_and_read_returns_it(self):
        rec = derive({"state": "crashed"}, HOLD)
        self.assertTrue(crs.publish(self.ws, rec, now=time.time()))
        self.assertEqual(crs.read(self.ws), rec)
        self.assertEqual(json.loads(self.path.read_text()), rec)

    def test_an_unchanged_record_is_rewritten_only_when_due_a_refresh(self):
        rec = derive({"state": "running"})
        now = time.time()
        self.assertTrue(crs.publish(self.ws, rec, now=now))
        self.assertFalse(crs.publish(self.ws, rec, now=now + 1), "nothing changed, nothing written")
        self.assertTrue(crs.publish(self.ws, dict(rec, state="idle"), now=now + 2), "a change is written at once")
        self.assertTrue(crs.publish(self.ws, dict(rec, state="idle"), now=now + crs.REFRESH_S + 5),
                        "an unchanged record is re-stamped so its age says the monitor is alive")

    def test_publish_refuses_a_record_outside_the_contract(self):
        with self.assertRaises(ValueError):
            crs.publish(self.ws, {"state": "running"})
        self.assertFalse(self.path.exists())

    def test_a_reader_never_sees_a_torn_record(self):
        recs = [derive({"state": s}) for s in ("running", "idle-ready", "logged-out", "crashed")]
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    raw = self.path.read_text()
                except FileNotFoundError:
                    continue
                try:
                    if crs.coerce(json.loads(raw)) is None:
                        bad.append(raw)
                except ValueError:
                    bad.append(raw)
        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(400):
                crs.publish(self.ws, recs[i % len(recs)], now=time.time())
        finally:
            stop.set()
            t.join()
        self.assertEqual(bad, [])

    def test_read_refuses_an_absent_oversized_malformed_or_stale_record(self):
        self.assertIsNone(crs.read(self.ws))
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"v": 1, "state": "running", "pad": "x" * crs.MAX_BYTES}))
        self.assertIsNone(crs.read(self.ws))
        self.path.write_text("{not json")
        self.assertIsNone(crs.read(self.ws))
        self.path.write_text(json.dumps({"v": 1, "state": "running"}))
        self.assertEqual(crs.read(self.ws), {"v": 1, "state": "running"})
        old = time.time() - crs.MAX_AGE_S - 10
        os.utime(self.path, (old, old))
        self.assertIsNone(crs.read(self.ws), "a monitor that stopped writing vouches for nothing")
        self.assertIsNotNone(crs.read(self.ws, max_age_s=None))

    def test_restarts_count_this_hosts_launches_in_the_window(self):
        log = self.ws / "state" / "session-starts.log"
        log.parent.mkdir(parents=True)
        lines = [{"host": "me", "session_started_at": NOW - 30}, {"host": "me", "session_started_at": NOW - 300},
                 {"host": "me", "session_started_at": NOW - 900}, {"host": "other", "session_started_at": NOW - 10},
                 {"session_started_at": NOW - 5}, "torn {", {"host": "me", "session_started_at": "x"}]
        log.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines) + "\n")
        self.assertEqual(crs.restarts_in_window(self.ws, NOW, hosts=frozenset({"me"})), 2)
        self.assertEqual(crs.restarts_in_window(self.ws / "nowhere", NOW, hosts=frozenset({"me"})), 0)

    def test_update_never_raises_into_the_monitor(self):
        with mock.patch.object(crs, "gather", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stderr"):
            self.assertFalse(crs.update(self.ws, {"state": "running"}))
        self.assertFalse(self.path.exists())

    def test_update_gathers_the_hold_the_queue_and_the_launches(self):
        state = self.ws / "state"
        state.mkdir()
        (state / "core-limit-hold.json").write_text(json.dumps(dict(HOLD, until=time.time() + 3600)))
        (self.ws / "tasks").mkdir()
        for i in range(2):
            (self.ws / "tasks" / f"task-{i}.txt").write_text("task: x\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.ws / "cfg")}):
            self.assertTrue(crs.update(self.ws, {"state": "crashed"}, runtime="claude"))
        rec = crs.read(self.ws)
        self.assertEqual((rec["state"], rec["waiting_tasks"], rec["restarts_10m"], rec["runtime"]),
                         ("paused_limit", 2, 0, "claude"))
        self.assertEqual(rec["account"], "acct_3f9a2b")


class Monitor(unittest.TestCase):
    """The monitor tick is the caller: the core's monitor writes the record, a worker seat's does not."""

    def _tick(self, *extra):
        spec = importlib.util.spec_from_file_location("ciw_recovery", SRC / "core-input-watch.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ws = Path(tempfile.mkdtemp())
        out = ws / "state" / "core-supervisor.json"

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "needs_login"}
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", str(out), "--once",
                "--no-chat-escalation", "--login-confirm-s", "0", *extra]
        with mock.patch.object(m, "capture_styled", lambda *a: "Please log in to continue\n"), \
                mock.patch.object(m, "_load_runtime_health", lambda: _RH()), \
                mock.patch.object(m, "gateway_alive", lambda *a: True), \
                mock.patch.object(m, "session_runtime", lambda *a: "claude"), \
                mock.patch.object(m, "_ensure_tmux_on_path", lambda: None), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(ws / "cfg")}), \
                mock.patch.object(sys, "argv", argv):
            m.main()
        return ws

    def test_the_core_monitor_tick_writes_the_record(self):
        ws = self._tick()
        rec = crs.read(ws)
        self.assertIsNotNone(rec, "the tick published state/core-recovery.json")
        sup = json.loads((ws / "state" / "core-supervisor.json").read_text())
        self.assertEqual((sup["state"], rec["state"], rec["runtime"]), ("logged-out", "signed_out", "claude"))

    def test_a_worker_seats_monitor_writes_none(self):
        ws = self._tick("--seat", "worker-1")
        self.assertTrue((ws / "state" / "core-supervisor.json").exists())
        self.assertFalse((ws / "state" / "core-recovery.json").exists())


class Contract(unittest.TestCase):
    """contracts/core_recovery.v1/ is copied byte-identical into the backend and clients."""

    def test_the_writer_produces_the_heartbeat_fixtures_recovery_block(self):
        want = fixture("heartbeat.json")["health"]["recovery"]
        got = derive({"state": "crashed"}, HOLD, runtime="claude", model="opus", waiting_tasks=2, restarts_10m=0,
                     runtimes_available=("claude", "codex"))
        self.assertEqual(got, want)
        with tempfile.TemporaryDirectory() as tmp:
            crs.publish(Path(tmp), got, now=time.time())
            self.assertEqual(json.loads((Path(tmp) / "state" / crs.RECORD_FILE).read_text()), want)

    def test_the_writer_produces_the_codex_fixtures_recovery_block(self):
        want = fixture("heartbeat.codex.json")["health"]["recovery"]
        got = derive({"state": "idle-ready"}, HOLD, wedge=cli_wedge.frame_abnormal(CODEX_LIMIT), runtime="codex",
                     model="opus", waiting_tasks=2, restarts_10m=0, runtimes_available=("claude", "codex"))
        self.assertEqual(got, want)

    def test_the_heartbeat_fixtures_name_the_capability_and_fit_the_bounds(self):
        for name in ("heartbeat.json", "heartbeat.codex.json"):
            body = fixture(name)
            self.assertIn(crs.CAPABILITY, body["capabilities"])
            self.assertEqual(crs.coerce(body["health"]["recovery"]), body["health"]["recovery"])
            self.assertLessEqual(len(json.dumps(body["health"]["recovery"])), crs.MAX_BYTES)

    def test_the_action_fixture_is_a_click_the_hitl_wire_parses_and_the_allowlist_admits(self):
        body = fixture("action.json")
        reply = ActionReply.from_wire(body[REPLY_FIELD])
        self.assertEqual(crs.parse_action_id(reply.action_id), ("switch_model", "sonnet"))
        for ok in ("retry_now", "restart_core", "escape_prompt", "switch_model:haiku", "switch_runtime:codex"):
            self.assertIsNotNone(crs.parse_action_id(ok), ok)
        for bad in ("switch_model:fable", "switch_model:", "switch_model:sonnet; rm", "switch_runtime:agy",
                    "restart", "", None, 3):
            self.assertIsNone(crs.parse_action_id(bad), bad)

    def test_the_allowlist_admits_only_what_the_runtime_carries_out(self):
        self.assertEqual(crs.parse_action_id("switch_runtime:codex", "claude"), ("switch_runtime", "codex"))
        self.assertIsNone(crs.parse_action_id("switch_runtime:claude", "claude"), "not a switch to itself")
        for bad in ("switch_model:sonnet", "retry_now"):
            self.assertIsNone(crs.parse_action_id(bad, "codex"), bad)
        self.assertEqual(crs.parse_action_id("switch_runtime:claude", "codex"), ("switch_runtime", "claude"))
        self.assertIsNone(crs.parse_action_id("retry_now", "agy"), "an unknown runtime gets the neutral set")
        self.assertEqual(crs.parse_action_id("restart_core", "agy"), ("restart_core", None))

    def test_the_receipt_fixture_uses_the_shared_vocabulary(self):
        body = fixture("receipt.json")
        self.assertIn(body["outcome"], crs.RECEIPT_OUTCOMES)
        self.assertIsNotNone(crs.parse_action_id(body["action_id"]))
        self.assertIn(body["recovery"]["state"], crs.STATES)
        self.assertEqual(body["incident"], fixture("heartbeat.json")["health"]["recovery"]["incident"])
        self.assertEqual(body["hitl_id"], fixture("action.json")[REPLY_FIELD]["hitl_id"])


if __name__ == "__main__":
    unittest.main()
