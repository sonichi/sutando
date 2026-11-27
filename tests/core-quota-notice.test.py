#!/usr/bin/env python3
"""The core's quota notice (src/core_quota_notice.py) and the Claude Code core monitor's use of it.

- One account card per limit episode, kept in state/core-quota-card.json across restarts; a card
  that was not posted is tried again no sooner than RETRY_S; a cleared limit, or a reset time that
  has passed, makes the next limit a new card.
- The card is sent by the injected account card command (`post --owner-dm --lead`), owner DM only.
- The monitor (src/core-input-watch.py limit_reply_step) sends it for the core's usage-limit
  screen, with the reset time from the banner's own line, and clears it once the pane shows the core
  working again; a worker seat and a Codex pane send none.

Run: python3 tests/core-quota-notice.test.py
"""
from __future__ import annotations

import importlib.util
import json
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
import core_quota_notice as cqn  # noqa: E402

_spec = importlib.util.spec_from_file_location("ciw_quota_notice", str(REPO / "src" / "core-input-watch.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

FOOTER = "────────\n❯ \n────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
USAGE_LIMIT = "❯ task-1\n  ⎿  You've hit your usage limit · resets 3pm\n✻ Cooked for 1s\n" + FOOTER
WORKED = "❯ task-9\n● Done: the summary is in the room.\n✻ Worked for 2m 14s · done 4:05 PM\n" + FOOTER
SETTLED = ("blocked-human", "turn-rejected", "You've hit your usage limit · resets 3pm")
RUNNING = ("running", None, None)


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ws = Path(td.name)
        for d in ("state", "tasks", "results"):
            (self.ws / d).mkdir()
        self.now = [1_000_000.0]
        self.cards, self.posted = [], True

    def send(self, text):
        self.cards.append(text)
        return self.posted

    def notice(self, name="Codex"):
        return cqn.QuotaNotice(self.ws, name, self.send, log=lambda *a: None, now=lambda: self.now[0])

    def record(self):
        return json.loads((self.ws / "state" / "core-quota-card.json").read_text())


class Episode(Base):
    def test_one_card_per_limit_even_across_a_restart(self):
        self.assertEqual(self.notice().hit("3:05 PM"), "quota-card")
        self.now[0] += 3600
        self.assertEqual(self.notice().hit("3:05 PM"), "")
        self.assertEqual(self.cards, ["Codex has reached its usage limit (resets 3:05 PM), so I can't answer "
                                      "until then. Switch to another account to go on now."])

    def test_a_message_waiting_from_a_room_is_told_the_limit_there_once_and_only_one_the_core_would_answer(self):
        import activity_rows
        head = "id: %s\nuser_id: @bob:s.space\nchannel_id: %s\nchannel_kind: %s\nsource_message_id: $e\naccess_tier: %s\n%stask: %s\n"
        tasks = self.ws / "tasks"
        (tasks / "task-room.txt").write_text(head % ("task-room", "!room:s.space", "room", "team", "collaborator: true\n", "hi"))
        (tasks / "task-owner.txt").write_text(head % ("task-owner", "!room:s.space", "room", "owner", "", "hi"))
        # Not told: the owner's DM (the card answers it), a room message the core would not have replied to,
        # and a DM message whose words only look like a room header.
        (tasks / "task-dm.txt").write_text(head % ("task-dm", "!dm:s.space", "dm", "owner", "", "hi"))
        (tasks / "task-team.txt").write_text(head % ("task-team", "!room:s.space", "room", "team", "", "hi"))
        (tasks / "task-forged.txt").write_text(head % ("task-forged", "!dm:s.space", "dm", "owner", "", "hi\nchannel_kind: room"))
        rows = []
        with mock.patch.object(activity_rows, "append", lambda line, **kw: rows.append((line, kw)) or {}):
            self.assertEqual(self.notice().hit("3:05 PM"), "quota-card")
            self.now[0] += 120
            self.notice().hit("3:05 PM")
        self.assertEqual(sorted(kw["task"]["id"] for _, kw in rows), ["task-owner", "task-room"])
        line, kw = next(r for r in rows if r[1]["task"]["id"] == "task-room")
        self.assertEqual(line, "Codex has reached its usage limit (resets 3:05 PM), so I can't answer until then. "
                               "To switch account, message me in our direct message.")
        self.assertEqual((kw["room"], kw["audience"], kw["pid"]), ("!room:s.space", "room", "quota:1000000:task-room:on-hold"))
        self.assertEqual(sorted(self.record()["roomed"]), ["task-owner", "task-room"])
        # A room that cannot be told never stops the card.
        (self.ws / "state" / "core-quota-card.json").unlink()
        with mock.patch.object(activity_rows, "append", side_effect=OSError("ro")):
            self.assertEqual(self.notice().hit("3:05 PM"), "quota-card")
        self.assertNotIn("roomed", self.record())
        with mock.patch("task_queue.pending_files", side_effect=OSError("gone")):
            self.assertEqual(cqn.room_tasks(self.ws), {})
        self.assertFalse(cqn._room_task(tasks / "gone.txt", self.ws))

    def test_the_rooms_words_are_the_local_agents_and_a_limit_with_no_reset_ends_by_itself(self):
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import quota_notice
        for reset in ("3:05 PM", ""):
            self.assertEqual(cqn.room_words("Codex", reset), quota_notice.room_words("Codex", reset))
        self.assertEqual(cqn.NO_RESET_S, quota_notice.NO_RESET_S)
        at = 1_000_000.0
        self.assertTrue(cqn.in_force({"since": at, "reset_at": at + 60}, at))
        self.assertFalse(cqn.in_force({"since": at, "reset_at": at + 60}, at + 61))
        self.assertTrue(cqn.in_force({"since": at - 10 ** 6, "seen": at}, at + cqn.NO_RESET_S))
        self.assertFalse(cqn.in_force({"since": at}, at + cqn.NO_RESET_S + 1))
        for rec in ({}, [], {"since": at, "why": cqn.SIGNED_OUT}):
            self.assertFalse(cqn.in_force(rec, at))
        self.notice().hit("")
        self.assertEqual(self.record()["seen"], self.now[0])

    def test_no_reset_known_says_so(self):
        self.notice().hit()
        self.assertIn("(it does not say when that resets)", self.cards[0])

    def test_a_card_not_posted_is_tried_again_only_after_the_retry_interval(self):
        self.posted = False
        q = self.notice()
        self.assertEqual(q.hit(), "quota-not-posted")
        self.now[0] += cqn.RETRY_S - 1
        self.assertEqual(q.hit(), "")
        self.now[0] += 2
        self.posted = True
        self.assertEqual(q.hit(), "quota-card")
        self.assertEqual(len(self.cards), 2)
        self.assertTrue(self.record()["told"])

    def test_a_send_that_raises_is_not_posted(self):
        def boom(text):
            raise RuntimeError("no lane")
        q = cqn.QuotaNotice(self.ws, "Codex", boom, log=lambda *a: None, now=lambda: self.now[0])
        self.assertEqual(q.hit(), "quota-not-posted")

    def test_cleared_then_a_new_limit_is_a_new_card(self):
        q = self.notice()
        q.hit()
        self.assertEqual(q.cleared(), "quota-cleared")
        self.assertEqual(q.cleared(), "")
        self.assertEqual(q.hit(), "quota-card")
        self.assertEqual(len(self.cards), 2)

    def test_a_reset_time_that_passed_makes_the_next_limit_a_new_card(self):
        q = self.notice()
        q.hit("4:00 PM", self.now[0] + 600)
        self.now[0] += 300
        self.assertEqual(q.hit(), "")
        self.now[0] += 400
        self.assertEqual(q.hit(), "quota-card")
        self.assertEqual(len(self.cards), 2)


class Words(unittest.TestCase):
    def test_reset_from_text_keeps_only_the_time(self):
        self.assertEqual(cqn.reset_from_text("You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM."),
                         "3:05 PM")
        self.assertEqual(cqn.reset_from_text("try again in 2 days 3 hours or upgrade"), "2 days 3 hours")
        self.assertEqual(cqn.reset_from_text("stream disconnected"), "")

    def test_reset_from_epoch_today_and_another_day(self):
        now = time.mktime((2026, 11, 23, 10, 0, 0, 0, 0, -1))
        self.assertEqual(cqn.reset_from_epoch(time.mktime((2026, 11, 23, 15, 5, 0, 0, 0, -1)), now), "3:05 PM")
        self.assertEqual(cqn.reset_from_epoch(time.mktime((2026, 11, 24, 9, 0, 0, 0, 0, -1)), now), "Nov 24, 9:00 AM")
        self.assertEqual(cqn.reset_from_epoch(None, now), "")


class Sender(Base):
    def test_the_injected_command_posts_to_the_owner_dm_with_the_words_leading(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(stdout='{"status": "posted", "event_id": "$e"}\n', returncode=0)
        send = cqn.card_sender(["py", "card.py"], self.ws, log=lambda *a: None, run=run)
        self.assertTrue(send("Codex has reached its usage limit"))
        self.assertEqual(calls, [["py", "card.py", "--workspace", str(self.ws), "post", "--owner-dm", "--retry", "--lead",
                                  "Codex has reached its usage limit"]])

    def test_a_failed_or_hung_command_is_not_posted(self):
        for run in (lambda cmd, **kw: mock.Mock(stdout='{"status": "failed", "why": "no DM"}', returncode=1),
                    lambda cmd, **kw: mock.Mock(stdout="", returncode=1)):
            self.assertFalse(cqn.card_sender(["x"], self.ws, log=lambda *a: None, run=run)("t"))

        def hung(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertFalse(cqn.card_sender(["x"], self.ws, log=lambda *a: None, run=hung)("t"))


class ClaudeCoreMonitor(Base):
    def setUp(self):
        super().setUp()
        for target, name, value in ((clh, "launch_identity", lambda ws, config_dir=None: ("claude-opus-5-5", None)),
                                    (clh, "_spawn_relaunch", lambda *a, **k: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def step(self, settled, pane, runtime="claude", seat=None, quota=None):
        # Ten in the morning today, so the banner's "resets 3pm" is today's 3 PM.
        at = time.mktime(time.localtime()[:3] + (10, 0, 0, 0, 0, -1))
        return M.limit_reply_step(self.ws, settled, settled, pane, runtime, now=at, seat=seat,
                                  retry_cmd=["true"] if seat else None, quota=quota or self.q)

    def test_the_limit_screen_sends_one_card_with_the_banners_reset_then_clears(self):
        self.q = self.notice("Claude Code")
        self.step(SETTLED, USAGE_LIMIT)
        self.step(SETTLED, USAGE_LIMIT)
        self.assertEqual(len(self.cards), 1)
        self.assertTrue(self.cards[0].startswith("Claude Code has reached its usage limit (resets 3:00 PM)"))
        self.step(RUNNING, WORKED)
        self.assertEqual(self.record(), {})
        self.step(SETTLED, USAGE_LIMIT)
        self.assertEqual(len(self.cards), 2)

    def test_a_worker_seat_or_a_codex_pane_sends_none(self):
        self.q = self.notice("Claude Code")
        self.step(SETTLED, USAGE_LIMIT, seat="sutando-worker-x")
        self.step(SETTLED, USAGE_LIMIT, runtime="codex")
        self.assertEqual(self.cards, [])

    def test_the_reset_is_read_from_the_banner_line_alone(self):
        words, at = M.limit_reset("resets 9am elsewhere\nYou've hit your usage limit · resets 3pm\n❯ ", 1_000_000.0)
        self.assertIsNotNone(at)
        self.assertEqual(words, cqn.reset_from_epoch(at, 1_000_000.0))
        self.assertEqual(M.limit_reset("You've hit your usage limit\nresets 3pm", 1_000_000.0), ("", None))

    def test_the_launcher_injects_the_account_card_into_the_core_monitor(self):
        text = (REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh").read_text()
        self.assertIn('--quota-card-script "$REPO/skills/account-switch/scripts/account_card.py"', text)
        self.assertTrue((REPO / "skills" / "account-switch" / "scripts" / "account_card.py").is_file())


class LimitMenuReset(Base):
    def test_the_menus_continue_at_time_is_the_reset_when_the_banner_has_none(self):
        pane = ("You've hit your weekly limit\n  1. Wait here, then continue automatically at Nov 28 at 1pm\n"
                "  2. Switch to usage credits\n")
        words, at = M.limit_reset(pane, 1_000_000.0)
        self.assertIsNotNone(at)
        self.assertEqual(words, cqn.reset_from_epoch(at, 1_000_000.0))
        self.assertIsNotNone(clh.parse_reset("Wait here, then continue automatically at Nov 28 at 1pm", 1_000_000.0))


class UsageCredits(Base):
    """VM 2026-11-24: account-b@example.com hit the plan's weekly limit but had usage credits; the card read as
    "no quota". The card now says how to go on with credits; it never chooses them (they cost money)."""

    PANE = ("You've hit your weekly limit · resets Nov 28 at 1pm (UTC)\n"
            "/rate-limit-options\n"
            "> 1. Stop and wait for limit to reset\n"
            "  2. Wait here, then continue automatically at Nov 28 at 1pm\n"
            "  3. Switch to usage credits\n")

    def test_the_limit_menu_offering_credits_is_seen(self):
        self.assertTrue(cqn.credits_offered(self.PANE))
        self.assertFalse(cqn.credits_offered("You've hit your weekly limit\n  1. Stop and wait for limit to reset\n"))

    def test_the_providers_upgrade_paths_say_overage(self):
        state = self.ws / "state" / "quota-state.json"
        self.assertFalse(cqn.credits_offered("", self.ws), "no record")
        state.write_text(json.dumps({"headers": {"anthropic-ratelimit-unified-upgrade-paths": "overage"}}))
        self.assertTrue(cqn.credits_offered("", self.ws))
        state.write_text(json.dumps({"headers": {"anthropic-ratelimit-unified-upgrade-paths": "plan"}}))
        self.assertFalse(cqn.credits_offered("", self.ws))
        state.write_text("not json")
        self.assertFalse(cqn.credits_offered("", self.ws))

    def test_the_card_says_how_to_use_them_and_only_then(self):
        q = self.notice("Claude Code")
        self.assertEqual(q.hit(*M.limit_reset(self.PANE, self.now[0]), credits=cqn.credits_offered(self.PANE)),
                         "quota-card")
        self.assertIn(cqn.CREDITS_LINE, self.cards[-1])
        self.assertIn("has reached its usage limit", self.cards[-1])
        plain = cqn.words("Codex", "Nov 28, 1:00 PM")
        self.assertNotIn("usage credits", plain)
        self.assertIn(cqn.CREDITS_LINE, cqn.words("Codex", "", credits=True))
        self.assertNotIn("usage credits", cqn.words("Codex", "", why=cqn.SIGNED_OUT, credits=True))


class SignedOut(Base):
    """A signed-out Claude core: the account card (sign-in, no Retry), once, then once per owner message."""

    def send(self, text, signed_out=False):
        self.cards.append((text, signed_out))
        return self.posted

    def owner_task(self, name):
        (self.ws / "tasks" / f"{name}.txt").write_text(f"id: {name}\ntask: hi\naccess_tier: owner\n")

    def test_signed_out_cards_once_then_each_owner_message(self):
        q = self.notice("Claude Code")
        self.assertEqual(M.signed_out_step(q, "blocked-human", "login", "claude"), "quota-card")
        self.assertEqual(M.signed_out_step(q, "blocked-human", "login", "claude"), "")
        self.owner_task("task-hi")
        self.assertEqual(M.signed_out_step(q, "logged-out", None, "claude"), "quota-card-again")
        self.assertEqual(M.signed_out_step(q, "logged-out", None, "claude"), "")
        self.assertEqual(len(self.cards), 2)
        text, signed_out = self.cards[0]
        self.assertTrue(signed_out)
        self.assertTrue(text.startswith("Claude Code is signed out"))
        self.assertTrue(q.carded(cqn.SIGNED_OUT))
        self.assertFalse(q.carded())

    def test_not_signed_out_or_not_claude_sends_none(self):
        q = self.notice("Claude Code")
        self.assertEqual(M.signed_out_step(q, "running", None, "claude"), "")
        self.assertEqual(M.signed_out_step(q, "blocked-human", "usage-limit", "claude"), "")
        self.assertEqual(M.signed_out_step(q, "logged-out", None, "codex"), "")
        self.assertEqual(self.cards, [])

    def test_a_limit_after_signing_out_is_a_new_card(self):
        q = self.notice("Claude Code")
        M.signed_out_step(q, "logged-out", None, "claude")
        q.hit("3 PM")
        self.assertEqual([c[1] for c in self.cards], [True, False])

    def test_the_sender_asks_for_the_signed_out_card(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(stdout='{"status": "posted"}\n', returncode=0)
        cqn.card_sender(["py", "card.py"], self.ws, log=lambda *a: None, run=run)("t", signed_out=True)
        self.assertIn("--signed-out", calls[0])
        self.assertNotIn("--retry", calls[0])


CARD_SCRIPT = str(REPO / "skills" / "account-switch" / "scripts" / "account_card.py")


class _RH:
    TMUX_SOCKET = SESSION = None

    def derive(self):
        return {"health": "idle", "signals": {"process": True}}


class PausedNotice(Base):
    """On a Claude Code core a usage-limit pause is told by the account card alone; any other pause,
    or a card that cannot be posted, keeps the paused notice."""

    def setUp(self):
        super().setUp()
        self.out = str(self.ws / "state" / "core-supervisor.json")
        p = mock.patch.object(clh, "launch_identity", lambda ws, config_dir=None: ("claude-opus-5-5", None))
        p.start()
        self.addCleanup(p.stop)

    def tick(self, pane, escalate=False):
        """One real monitor pass (main --once) as the core's own seat, the card's post faked."""
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", "sutando-core", "--out", self.out,
                "--once", "--stable", "1", "--quota-card-script", CARD_SCRIPT]
        if not escalate:
            argv.append("--no-chat-escalation")
        with mock.patch.object(M, "capture_styled", lambda s, sess: pane), \
                mock.patch.object(M, "_load_runtime_health", lambda: _RH()), \
                mock.patch.object(M, "gateway_alive", lambda *a: True), \
                mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                mock.patch.object(M, "send_keys", lambda *a: False), \
                mock.patch.object(cqn, "card_sender", lambda argv, ws, log=None, run=None: self.send), \
                mock.patch.object(sys, "argv", argv):
            M.main()

    def paused_notices(self):
        return [p for p in (self.ws / "results").glob("*core-limit-hold-*") if "paused" in p.read_text()]

    def limit_cards(self):
        return [r for r in M._hitl_manager(self.out).active() if (r.subject or {}).get("session") == "sutando-core"]

    def test_a_usage_limit_pause_is_the_card_alone(self):
        self.tick(USAGE_LIMIT)
        self.tick(USAGE_LIMIT)
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.paused_notices(), [])
        hold = clh.active_hold(self.ws)
        self.assertEqual(hold["notice_surface"], "account-card", "the hold that takes the owner's replies")

    def test_with_chat_escalation_the_monitors_limit_card_is_not_raised_either(self):
        self.tick(USAGE_LIMIT, escalate=True)
        self.tick(USAGE_LIMIT, escalate=True)
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.limit_cards(), [])
        self.assertEqual(self.paused_notices(), [])

    def test_a_card_that_cannot_be_posted_falls_back_to_the_paused_notice_once(self):
        self.posted = False
        self.tick(USAGE_LIMIT)
        self.assertEqual(len(self.paused_notices()), 1)
        rec = self.record()
        (self.ws / "state" / "core-quota-card.json").write_text(json.dumps(dict(rec, tried=0)))  # long ago
        self.tick(USAGE_LIMIT)
        self.assertEqual(len(self.cards), 1, "a card after the notice would be a second message")
        self.assertEqual(len(self.paused_notices()), 1)

    def test_with_chat_escalation_a_failed_card_leaves_the_monitors_limit_card(self):
        self.posted = False
        self.tick(USAGE_LIMIT, escalate=True)
        self.assertEqual(len(self.limit_cards()), 1)

    def test_a_rate_limit_pause_keeps_its_paused_notice_and_sends_no_card(self):
        q = self.notice("Claude Code")
        settled = ("blocked-human", "usage-limit", "Rate limited · try again in 5m")
        M.limit_reply_step(self.ws, settled, settled, "Rate limited · try again in 5m\n" + FOOTER, "claude",
                           quota=q)
        self.assertEqual(self.cards, [])
        self.assertEqual(len(self.paused_notices()), 1)

    def test_a_pause_at_launch_keeps_its_paused_notice(self):
        limit = clh.Limit("usage-limit", time.time(), None, "claude-opus-5-5", "cli-transcript")
        clh.decide(self.ws, evidence=(limit,), launch_model="claude-opus-5-5")
        self.assertEqual(len(self.paused_notices()), 1)


class EveryMessage(Base):
    """Owner rule (VM 2026-11-23): while out of quota, every owner message gets the card right away;
    none waits silently behind the limit, and none is carded twice."""

    def test_each_owner_message_during_the_limit_gets_the_card_once(self):
        q = self.notice()
        self.assertEqual(q.hit("3:05 PM", tasks=["t1"]), "quota-card")
        self.assertEqual(q.hit("3:05 PM", tasks=["t1"]), "", "the message the first card answered")
        self.assertEqual(q.hit("3:05 PM", tasks=["t1", "t2"]), "quota-card-again")
        self.assertEqual(q.hit("3:05 PM", tasks=["t1", "t2"]), "")
        self.assertEqual(q.hit("3:05 PM", tasks=["t3"]), "quota-card-again")
        self.assertEqual(len(self.cards), 3)
        self.assertTrue(all("usage limit" in c for c in self.cards))
        q.cleared()
        self.assertEqual(q.hit("", tasks=["t3"]), "quota-card", "a new limit is a new episode")

    def test_a_failed_follow_up_is_tried_again_after_its_backoff_then_not_resent(self):
        q = self.notice()
        self.assertEqual(q.hit("", tasks=["t1"]), "quota-card")
        self.posted = False
        self.assertEqual(q.hit("", tasks=["t1", "t2"]), "quota-not-posted")
        self.posted = True
        self.now[0] += cqn.RETRY_S - 1
        self.assertEqual(q.hit("", tasks=["t1", "t2"]), "", "not before the backoff")
        self.now[0] += 1
        self.assertEqual(q.hit("", tasks=["t1", "t2"]), "quota-card-again")
        self.assertEqual(q.hit("", tasks=["t1", "t2"]), "")
        self.assertEqual(len(self.cards), 3)
        self.assertNotIn("again_failed", self.record())

    def test_a_follow_up_that_keeps_failing_is_tried_again_ever_later_and_sent_once_it_can_be(self):
        q = self.notice()
        q.hit("", tasks=["t1"])
        self.posted = False
        for _ in range(cqn.AGAIN_TRIES + 2):
            self.assertEqual(q.hit("", tasks=["t1", "t2"]), "quota-not-posted")
            self.assertEqual(q.hit("", tasks=["t1", "t2"]), "", "not before its next try")
            self.now[0] += cqn.RETRY_S * 2 ** (cqn.AGAIN_TRIES - 1)
        # The message was never marked told: once a card can be posted, it gets one.
        self.posted = True
        self.assertEqual(q.hit("", tasks=["t1", "t2"]), "quota-card-again")
        self.assertEqual(self.record()["carded"], ["t1", "t2"])

    def test_after_the_paused_notice_told_it_a_later_message_still_gets_the_card(self):
        self.posted = False
        q = self.notice()
        q.hit("", tasks=["t1"])
        q.yield_to_notice()
        self.posted = True
        self.assertEqual(q.hit("", tasks=["t2"]), "quota-card-again")
        self.assertEqual(len(self.cards), 2)
        self.assertEqual(q.hit("", tasks=["t2"]), "", "once per message")

    def test_only_the_owners_waiting_messages_count(self):
        (self.ws / "tasks" / "task-a.txt").write_text("id: task-a\naccess_tier: owner\ntask: hi\n")
        (self.ws / "tasks" / "task-b.txt").write_text("id: task-b\naccess_tier: team\ntask: hi\n")
        (self.ws / "tasks" / "task-c.txt").write_text("id: task-c\ntask: no tier\n")
        self.assertEqual(cqn.owner_waiting(self.ws), ["task-a"])


class RuntimeSwitch(Base):
    """VM 2026-11-23 21:14-21:57 PT: the core switched Codex -> Claude Code while the Codex limit's card
    was on record (told, weekly reset days away), so the Claude Code core's own limit was never carded."""

    def test_another_runtimes_told_limit_does_not_silence_this_ones_first_card(self):
        self.assertEqual(self.notice("Codex").hit("Nov 28, 1:00 PM", 4102444800), "quota-card")
        self.assertEqual(self.notice("Codex").hit("Nov 28, 1:00 PM", 4102444800), "")
        self.assertEqual(self.notice("Claude Code").hit("Nov 28 at 1pm"), "quota-card")
        self.assertEqual(len(self.cards), 2)
        self.assertIn("Claude Code has reached its usage limit", self.cards[1])

    def test_the_monitor_defaults_to_this_checkouts_card_script(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "core-input-watch.py").read_text()
        self.assertIn('"--quota-card-script", default=str(_REPO / "skills" / "account-switch"', src)



class Edges(Base):
    def test_no_reset_moment_reads_as_none(self):
        self.assertEqual((cqn.reset_from_epoch(0), cqn.reset_from_epoch(-5), cqn.reset_from_epoch("x")), ("", "", ""))

    def test_the_health_record_names_the_account_and_never_stops_the_notice(self):
        import core_account_health
        from unittest import mock
        q = cqn.QuotaNotice(self.ws, "Codex", self.send, log=lambda *a: None, now=lambda: self.now[0],
                            runtime="codex", account=lambda: "a@example.com")
        with mock.patch.object(core_account_health, "mark") as mark:
            q.health("quota", 5.0)
        self.assertEqual(mark.call_args.args[1:5], ("quota", "codex", "a@example.com", 5.0))
        logged = []
        q.log = lambda *a: logged.append(a)
        with mock.patch.object(core_account_health, "mark", side_effect=RuntimeError):
            q.health("quota")
        self.assertEqual(logged, [("account health not kept:", "RuntimeError")])

    def test_a_record_that_cannot_be_kept_is_not_posted(self):
        import local_record
        from unittest import mock
        logged = []
        q = self.notice()
        q.log = lambda *a: logged.append(a)
        with mock.patch.object(local_record, "write_whole", side_effect=OSError):
            self.assertEqual(q.hit("3pm"), "quota-not-posted")
        self.assertEqual(self.cards, [])
        self.assertIn(("quota record not kept:", "OSError"), logged)

    def test_a_follow_up_card_that_raises_is_a_failure_tried_again(self):
        q = self.notice()
        self.assertEqual(q.hit("3pm", tasks=["task-1"]), "quota-card")

        def boom(text, signed_out=False):
            raise RuntimeError("down")
        q.send = boom
        self.assertEqual(q.hit("3pm", tasks=["task-2"]), "quota-not-posted")
        self.assertEqual(self.record()["again_failed"]["n"], 1)

    def test_owner_waiting_survives_an_unreadable_queue_and_task(self):
        from unittest import mock
        with mock.patch("task_queue.pending_files", side_effect=OSError):
            self.assertEqual(cqn.owner_waiting(self.ws), [])
        self.assertFalse(cqn.is_owner_task(self.ws / "tasks" / "missing.txt"))


class MonitorSteps(Base):
    def test_no_notice_and_a_raising_one_never_stop_the_monitor(self):
        import contextlib
        import io
        from unittest import mock
        self.assertEqual(M.quota_step(None, True, "x"), "")
        boom = mock.Mock(workspace=self.ws, hit=mock.Mock(side_effect=RuntimeError), cleared=mock.Mock())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(M.quota_step(boom, True, "You've hit your usage limit"), "quota-failed (RuntimeError)")
            self.assertEqual(M.signed_out_step(boom, "logged-out", None, "claude"),
                             "signed-out-card-failed (RuntimeError)")


if __name__ == "__main__":
    unittest.main()
