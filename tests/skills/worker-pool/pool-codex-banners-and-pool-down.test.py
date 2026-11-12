#!/usr/bin/env python3
"""A Codex seat stopped by its own error cell, and a whole pool stopped by one cause.

Codex prints a refused turn as "■ <message>" above an idle composer, so pane_gate reads the seat
idle-ready while it is limited or signed out. What is pinned here:
- the Codex CLI's real wording only (codex-cli 0.160.1, from its binary's strings) classifies, and
  only as the last content above the composer: a cell under later turns is history;
- supervision maps a limit to PANE_LIMIT and signed-out to PANE_LOGGED_OUT; a refused request
  leaves the seat usable (PANE_IDLE); a Claude seat's reading is unchanged;
- the reset in "try again at 3:05 PM" / "Nov 13th, 2026 9:12 AM" is parsed;
- an account is shown by its label (display name, plan; for Codex "<method> login"), never an email;
- pool_down() gives one entry per runtime whose every live, unpaused seat is stopped by the same
  cause, with the data 2b's panel card reads; raise_pool_down() keeps one card per down pool and
  resolves it when the pool works again or its seats change.

Run: python3 tests/skills/worker-pool/pool-codex-banners-and-pool-down.test.py
"""
from __future__ import annotations
# ruff: noqa: E402 (the imports follow the sys.path set-up)

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
sup, ps, wi, wc = rem.sup, rem.ps, rem.wi, rem.wc
pl = _load("pool_limit")
import core_limit_hold as clh
from delivery import codex_banners as cb
from hitl.manager import HitlManager, HitlStore

# The Codex 0.157 idle composer and footer, as captured (tests/pane-gate-contract.test.py).
FOOT = "  GPT-6-Sol ultra · ~/Library/Application Support/sp…  ⚠ 1 warning · f2 to view"
IDLE = f"\x1b[1m»\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n{FOOT}\n"
# The CLI's own messages, verbatim from its binary (the apostrophe is U+2019).
LIMITS = {
    "pro": "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
           "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 3:05 PM.",
    "plus": "You’ve hit your usage limit. Upgrade to Plus to continue using Codex "
            "(https://chatgpt.com/explore/plus), or try again at Nov 13th, 2026 9:12 AM.",
    "credits": "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits",
    "admin": "You’ve hit your usage limit. To get more access now, send a request to your admin",
    "model": "You’ve hit your usage limit for GPT-6-Sol. Switch to another model now,",
}
SIGNED_OUT = [
    "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.",
    "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
    "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again.",
    "Your access token could not be refreshed because you have since logged out or signed in to another account. "
    "Please sign in again.",
    "Your access token could not be refreshed. Please log out and sign in again.",
]
REFUSAL = ("This content can’t be shown\n  We take extra care with some cybersecurity requests. If you’re doing "
           "authorized security work, apply for Daybreak to get broader access.")


def pane(cell, composer=IDLE, before="› do the task\n\n"):
    return f"{before}■ {cell}\n\n{composer}"


class CodexBanners(unittest.TestCase):
    def test_every_limit_variant_is_a_usage_limit(self):
        for name, text in LIMITS.items():
            with self.subTest(name):
                self.assertEqual(cb.live_banner(pane(text)).cause, cb.USAGE_LIMIT)

    def test_the_reset_words_come_from_try_again_at(self):
        self.assertEqual(cb.live_banner(pane(LIMITS["pro"])).reset_words, "3:05 PM")
        self.assertEqual(cb.live_banner(pane(LIMITS["plus"])).reset_words, "Nov 13th, 2026 9:12 AM")
        self.assertIsNone(cb.live_banner(pane(LIMITS["admin"])).reset_words)

    def test_every_signed_out_variant_is_signed_out(self):
        for text in SIGNED_OUT:
            with self.subTest(text[:60]):
                self.assertEqual(cb.live_banner(pane(text)).cause, cb.SIGNED_OUT)

    def test_a_refusal_with_its_continuation_line(self):
        got = cb.live_banner(pane(REFUSAL))
        self.assertEqual(got.cause, cb.CONTENT_REFUSAL)
        self.assertIn("We take extra care with some cybersecurity requests", got.line)

    def test_either_apostrophe(self):
        self.assertEqual(cb.live_banner(pane(LIMITS["admin"].replace("’", "'"))).cause, cb.USAGE_LIMIT)

    def test_a_cell_under_a_later_turn_is_history(self):
        later = pane(LIMITS["pro"], composer=f"› the next task\n\n• Done: all three files updated.\n\n{IDLE}")
        self.assertIsNone(cb.live_banner(later))

    def test_another_error_cell_or_no_composer_is_nothing(self):
        self.assertIsNone(cb.live_banner(pane("Unrecognized command '/startup'. Type \"/\" for a list.")))
        self.assertIsNone(cb.live_banner(f"■ {LIMITS['pro']}\n"))
        self.assertIsNone(cb.live_banner(None))
        self.assertIsNone(cb.live_banner(""))

    def test_only_the_cells_own_frame_counts(self):
        self.assertIsNone(cb.live_banner(f"› do it\n\n  ■ {LIMITS['pro']}\n\n{IDLE}"), "an indented marker")
        self.assertIsNone(cb.live_banner(f"› do it\n\n    ■ {LIMITS['pro']}\n\n{IDLE}"), "a marker indented further")
        # Codex draws an assistant's message under its "• " row, its later lines indented: a line of it
        # that starts with the marker is never at column 0.
        for assistant in (f"› do it\n\n• Here is what the log says:\n  ■ {LIMITS['pro']}\n\n{IDLE}",
                          f"› do it\n\n• ■ {LIMITS['pro']}\n\n{IDLE}"):
            self.assertIsNone(cb.live_banner(assistant), "an assistant's line starting with the marker")
        pasted = f"› do it\n\n• Ran cat codex.log\n  └ 3 lines\n■ {LIMITS['pro']}\n\n{IDLE}"
        self.assertIsNone(cb.live_banner(pasted), "a log printed right under its tool row")
        self.assertIsNone(cb.live_banner(f"› do it\n■ {LIMITS['pro']}\n\n{IDLE}"), "printed right under a row")
        spinner = f"› do it\n\n■ {LIMITS['pro']}\n  ◦ Working (3s • esc to interrupt) try again at 9:00 PM\n\n{IDLE}"
        self.assertIsNone(cb.live_banner(spinner), "a turn running under the cell")
        apart = f"› do it\n\n■ {LIMITS['pro']}\n\n  ◦ Working (3s • esc to interrupt)\n\n{IDLE}"
        self.assertIsNone(cb.live_banner(apart), "a spinner set off by a blank line is a running turn, not a hint")
        self.assertIsNone(cb.live_banner(f"› do it\n\n ■ {LIMITS['pro']}\n  and more\n\n{IDLE}"),
                          "one space in: not column 0")
        tool = f"› do it\n\n■ {LIMITS['pro']}\n\n• Ran ls\n  └ a b\n\n{IDLE}"
        self.assertIsNone(cb.live_banner(tool), "a tool row after the cell: the turn went on")
        self.assertIsNone(cb.live_banner(f"■ {LIMITS['pro']}\n\n\n\n{IDLE}"), "three blank lines: not the frame")
        self.assertIsNone(cb.live_banner(f"■ stream error: {LIMITS['pro']}\n\n{IDLE}"),
                          "the message is the cell's own opening words, never quoted inside it")

    def test_the_shapes_a_real_worker_cell_takes(self):
        """Each shape a limited or signed-out worker shows (the review's seven), read as stopped."""
        shapes = {
            "mid-turn after tool output": f"› do it\n\n• Ran ls\n  └ a b\n\n■ {LIMITS['pro']}\n\n{IDLE}",
            "mid-turn after an assistant line": f"› do it\n\n• I'll check the logs.\n\n■ {LIMITS['pro']}\n\n{IDLE}",
            "resumed, no prompt visible": f"• Resumed the earlier session.\n\n■ {LIMITS['pro']}\n\n{IDLE}",
            "a retry cell above it": f"› do it\n\n■ stream error: retrying 1/5 in 2s\n\n■ {LIMITS['pro']}\n\n{IDLE}",
            "a hint row before the composer": f"› do it\n\n■ {LIMITS['pro']}\n\n  Tip: /status shows your usage\n{IDLE}",
            "no blank line": f"› do it\n\n■ {LIMITS['pro']}\n{IDLE}",
            "two blank lines": f"› do it\n\n■ {LIMITS['pro']}\n\n\n{IDLE}",
        }
        for name, capture in shapes.items():
            with self.subTest(name):
                self.assertEqual(cb.live_banner(capture).cause, cb.USAGE_LIMIT)
        wrapped = ("› do it\n\n■ You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit\n"
                   "    https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 3:05 PM.\n\n"
                   f"{IDLE}")
        got = cb.live_banner(wrapped)
        self.assertEqual((got.cause, got.reset_words), (cb.USAGE_LIMIT, "3:05 PM"), "a wrap indented deeper is its own")
        after_tool = f"› do it\n\n• Ran git push\n  └ done\n\n■ {SIGNED_OUT[0]}\n\n{IDLE}"
        self.assertEqual(cb.live_banner(after_tool).cause, cb.SIGNED_OUT, "a sign-out after tool output")
        retry_last = f"› do it\n\n■ {LIMITS['pro']}\n\n■ stream error: retrying 1/5 in 2s\n\n{IDLE}"
        self.assertIsNone(cb.live_banner(retry_last), "the last cell is the retry: not stopped on the limit")

    def test_a_draft_in_the_composer_does_not_hide_the_limit(self):
        draft = f"\x1b[1m»\x1b[0m half typed\n\n{FOOT}\n"
        self.assertEqual(cb.live_banner(pane(LIMITS["pro"], composer=draft)).cause, cb.USAGE_LIMIT)


class SupervisionReading(unittest.TestCase):
    def test_codex_kinds(self):
        self.assertEqual(sup.classify_pane_text(pane(LIMITS["pro"]), "codex"), ps.PANE_LIMIT)
        self.assertEqual(sup.classify_pane_text(pane(SIGNED_OUT[0]), "codex"), ps.PANE_LOGGED_OUT)
        self.assertEqual(sup.classify_pane_text(pane(REFUSAL), "codex"), ps.PANE_IDLE, "a refused request can work")
        self.assertEqual(sup.classify_pane_text(IDLE, "codex"), ps.PANE_IDLE)

    def test_a_claude_seat_reads_as_before(self):
        self.assertIsNone(sup.codex_banner(pane(LIMITS["pro"]), "claude"))

    def test_the_codex_reset_is_parsed(self):
        now = datetime(2026, 11, 12, 10, 0).timestamp()
        self.assertEqual(pl.reset_at(LIMITS["pro"], now), datetime(2026, 11, 12, 15, 5).timestamp())
        self.assertEqual(pl.reset_at(LIMITS["plus"], now), datetime(2026, 11, 13, 9, 12).timestamp())
        self.assertEqual(pl.reset("resets 9pm", now).banner, "9pm", "Claude's form is unchanged")
        self.assertEqual(pl.reset_at("Rate limit reached; try again in 20s.", now), now + 20)
        self.assertEqual(pl.reset_at("try again in 1.5 seconds", now), now + 1.5)
        self.assertEqual(pl.reset_at("try again in 250ms", now), now + 0.25)

    def test_the_real_observation_turns_the_limit_wait_off_for_codex(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "state").mkdir()
            rows = {"a" * 32: {"state": "live", "label": "Mars", "runtime": "claude"},
                    "c" * 32: {"state": "live", "label": "Reviewer", "runtime": "codex"}}
            (ws / "state" / "roster.json").write_text(json.dumps({"workers": rows}))
            wi.record_session(ws, "a" * 32, "s1", runtime="claude", relation=wi.RELATION_NEW)
            wi.start_incarnation(ws, "a" * 32, "s1", tmux_socket="/tmp/x.sock", tmux_session=wi.tmux_session_name("a" * 32))
            wi.create_worker(ws, runtime="codex", worker_id="c" * 32, tmux_socket="/tmp/x.sock")
            screens = {wi.tmux_session_name("a" * 32): "  ⎿  You've hit your session limit · resets 9pm\n❯ \n",
                       wi.tmux_session_name("c" * 32): pane(LIMITS["pro"])}

            def tmux(argv, **kw):
                name = argv[argv.index("-t") + 1].lstrip("=").split(":")[0] if "-t" in argv else ""
                if "capture-pane" in argv:
                    return subprocess.CompletedProcess(argv, 0, screens.get(name, ""), "")
                if "inbox-holders" in argv:
                    return subprocess.CompletedProcess(argv, 0, "4242 session\n", "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            obs = sup.observe(ws, 1_000_000.0, runner=tmux)
        self.assertEqual((obs["c" * 32].pane, obs["c" * 32].limit_waits), (ps.PANE_LIMIT, False))
        self.assertTrue(obs["a" * 32].limit_waits, "a Claude seat's limit wait is unchanged")

    def test_a_codex_limit_keeps_the_watcher_and_delivery_checks(self):
        def run(limit_waits):
            st, t, seen = ps.SupervisionState(), 1000.0, []
            for _ in range(8):
                o = ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=ps.ABSENT, watcher_held=False, work_outstanding=True,
                                   pane=ps.PANE_LIMIT, pane_id="same", limit_waits=limit_waits)
                st, d = ps.evaluate(st, {"w": o}, t)
                seen.append(d["w"])
                t += 300.0
            return seen, st.workers["w"]
        claude, claude_ev = run(True)
        codex, codex_ev = run(False)
        self.assertIsNone(claude_ev.watcher_first_detected_at, "a Claude seat waits on its limit, checks held")
        self.assertNotIn(ps.CARD_HUMAN, claude)
        self.assertIsNotNone(codex_ev.watcher_first_detected_at, "a Codex seat's dead watcher is still seen")
        self.assertIn(ps.CARD_HUMAN, codex, "and the seat that owes work is carded")


class AccountLabels(unittest.TestCase):
    def test_claude_label_is_fixed_wording_never_a_name_or_an_email(self):
        with tempfile.TemporaryDirectory() as d:
            def label(**acct):
                (Path(d) / ".claude.json").write_text(json.dumps({"oauthAccount": acct}))
                return clh.account_label(d)
            self.assertEqual(label(displayName="Ana a@b.co", planDisplayName="Claude Max", emailAddress="a@b.co",
                                   accountUuid="u"), "Claude Max")
            self.assertEqual(label(planDisplayName="Claude Max for a@b.co", accountUuid="u"), "Claude account")
            self.assertEqual(label(planDisplayName="sk-ant-***", accountUuid="u"), "Claude account")
            self.assertEqual(label(accountUuid="u"), "Claude account")
            self.assertEqual(label(planDisplayName="Claude Ana Smith Jones", accountUuid="u"), "Claude account",
                             "only a known plan name")
            self.assertIsNone(label(emailAddress="a@b.co"), "signed in to nothing")
        self.assertIsNone(clh.account_label("/no/such/dir"))
        self.assertIsNone(clh.account_label(None))

    def test_codex_label_is_fixed_wording_never_the_tools_text(self):
        def status(out):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, "")
        # `codex login status`'s own lines (codex-cli 0.160.1 binary strings), and a masked key after one.
        for line, want in (("Logged in using ChatGPT", "ChatGPT account"),
                           ("Logged in using an API key - sk-proj-***ABCDE", "API key"),
                           ("Logged in using Amazon Bedrock API key", "Amazon Bedrock"),
                           ("Logged in using Amazon Bedrock AWS access keys", "Amazon Bedrock"),
                           ("Logged in using personal access token", "access token"),
                           ("Logged in using access token", "access token"),
                           ("Logged in using workload identity", "workload identity"),
                           ("Logged in using something new ***SECRET", "unknown")):
            with self.subTest(line):
                got = wc.codex_account_label(status(line + "\n"))
                self.assertEqual(got, want)
                self.assertNotIn("***", got)
        for fuzz in ("Logged in using an API key - ***ABCDE", "Logged in using ***ABCDE", "Logged in using an API key ABCDE"):
            self.assertIn(wc.codex_account_label(status(fuzz)), ("API key", "unknown"))
        self.assertIsNone(wc.codex_account_label(status("Not logged in\n")))

        def gone(argv, **kw):
            raise OSError("no codex")
        self.assertIsNone(wc.codex_account_label(gone))


class PoolDown(unittest.TestCase):
    SEATS = {"a" * 32: ("Mars", "claude"), "b" * 32: ("Baxter", "claude"), "c" * 32: ("Reviewer", "codex")}

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {
            w: {"state": "live", "label": n, "runtime": r} for w, (n, r) in self.SEATS.items()}}))
        self.label = patch.object(wc, "_account_label", side_effect=lambda runtime, runner:
                                  "Claude Max" if runtime == "claude" else "ChatGPT account")
        self.label.start()
        self.addCleanup(self.label.stop)

    def obs(self, **over):
        base = {w: {"session_alive": True, "paused": False, "pane": ps.PANE_IDLE} for w in self.SEATS}
        for w, o in over.items():
            base[w].update(o)
        return base

    def test_every_claude_seat_parked_is_one_pool_down(self):
        a, b, _ = list(self.SEATS)
        got = wc.pool_down(self.ws, self.obs(**{a: {"pane": ps.PANE_LIMIT, "limit_reset_at": 2000.0,
                                                    "limit_banner": "9pm"},
                                                b: {"pane": ps.PANE_GATE, "limit_parked": True,
                                                    "limit_reset_at": 1500.0}}), host="mac")
        self.assertEqual(len(got), 1)
        g = got[0]
        self.assertEqual((g["id"], g["cause"], g["runtime"]), ("pool-down:mac:claude-usage-limit", "usage-limit", "claude"))
        self.assertEqual(g["workers"], ["Baxter", "Mars"])
        self.assertEqual(g["reset_at"], 1500.0, "the earliest reset")
        self.assertEqual(g["account_label"], "Claude Max")
        self.assertIn("/login", g["remedy"])
        self.assertNotIn("@", json.dumps(g))

    def test_one_working_seat_means_the_pool_is_not_down(self):
        a, b, _ = list(self.SEATS)
        self.assertEqual(wc.pool_down(self.ws, self.obs(**{a: {"pane": ps.PANE_LIMIT}}), host="mac"), [])
        self.assertEqual(wc.pool_down(self.ws, self.obs(**{a: {"pane": ps.PANE_LIMIT},
                                                           b: {"pane": ps.PANE_LOGGED_OUT}}), host="mac"), [],
                         "two causes are not one pool down")

    def test_paused_and_dead_seats_are_not_the_working_pool(self):
        a, b, _ = list(self.SEATS)
        got = wc.pool_down(self.ws, self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"paused": True}}), host="mac")
        self.assertEqual(got, [], "the working pool is one seat: its own card covers it")
        got = wc.pool_down(self.ws, self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"session_alive": False}}), host="mac")
        self.assertEqual(got, [])
        self.SEATS = dict(self.SEATS, **{"e" * 32: ("Rams", "claude")})
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {
            w: {"state": "live", "label": n, "runtime": r} for w, (n, r) in self.SEATS.items()}}))
        got = wc.pool_down(self.ws, self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"paused": True},
                                                "e" * 32: {"pane": ps.PANE_LIMIT}}), host="mac")
        self.assertEqual([g["workers"] for g in got], [["Mars", "Rams"]], "the paused seat is not in it")

    def test_a_signed_out_codex_pool(self):
        _, _, c = list(self.SEATS)
        self.assertEqual(wc.pool_down(self.ws, self.obs(**{c: {"pane": ps.PANE_LOGGED_OUT}}), host="mac"), [],
                         "one Codex seat: its login card is the card")
        f = "f" * 32
        self.SEATS = dict(self.SEATS, **{f: ("Pylon", "codex")})
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {
            w: {"state": "live", "label": n, "runtime": r} for w, (n, r) in self.SEATS.items()}}))
        got = wc.pool_down(self.ws, self.obs(**{c: {"pane": ps.PANE_LOGGED_OUT}, f: {"pane": ps.PANE_LOGGED_OUT}}),
                           host="mac")
        self.assertEqual([(g["id"], g["account_label"]) for g in got],
                         [("pool-down:mac:codex-signed-out", "ChatGPT account")])
        self.assertIn("codex login", got[0]["remedy"])
        self.assertIsNone(got[0]["reset_at"])

    def test_the_card_waits_two_sweeps_and_a_flapping_seat_moves_nothing(self):
        a, b, _ = list(self.SEATS)
        m = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        down = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"pane": ps.PANE_LIMIT}})
        once = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, down, host="mac"), manager=m)
        self.assertEqual(once["raised"], [], "one sweep down is not yet a down pool")
        for state in (down, self.obs(), down, self.obs(), down, self.obs(), down, self.obs(), down, self.obs(), down):
            wc.raise_pool_down(self.ws, wc.pool_down(self.ws, state, host="mac"), manager=m)
        pool_cards = [r for r in m.store.all() if (r.subject or {}).get("wedge") == wc.POOL_DOWN]
        self.assertLessEqual(len(pool_cards), 1, "eleven flapping sweeps give at most one card")

    def test_one_card_per_down_pool_resolved_when_it_works_again(self):
        a, b, _ = list(self.SEATS)
        m = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        down = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"pane": ps.PANE_LIMIT}})
        wc.raise_pool_down(self.ws, wc.pool_down(self.ws, down, host="mac"), manager=m)
        first = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, down, host="mac"), manager=m)
        self.assertEqual(len(first["raised"]), 1, "raised on the second sweep down")
        card = m.get(first["raised"][0])
        self.assertIn("All 2 Claude workers on mac are stopped", card.message)
        self.assertEqual(card.subject["pool_down"]["id"], "pool-down:mac:claude-usage-limit")
        again = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, down, host="mac"), manager=m)
        self.assertEqual(again, {"raised": [], "updated": [], "closed": []}, "the same pool down changes nothing")
        e = "e" * 32
        self.SEATS = dict(self.SEATS, **{e: ("Rams", "claude")})
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {
            w: {"state": "live", "label": n, "runtime": r} for w, (n, r) in self.SEATS.items()}}))
        grown = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"pane": ps.PANE_LIMIT}, e: {"pane": ps.PANE_LIMIT}})
        revision = m.get(first["raised"][0]).revision
        moved = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, grown, host="mac"), manager=m)
        self.assertEqual((moved["raised"], moved["updated"], moved["closed"]), ([], first["raised"], []),
                         "a new set of seats updates the same card")
        card = m.get(first["raised"][0])
        self.assertIn("All 3 Claude workers", card.message)
        self.assertGreater(card.revision, revision, "re-projected to the owner")
        settled = card.revision
        later = self.obs(**{a: {"pane": ps.PANE_LIMIT, "limit_reset_at": 9999.0}, b: {"pane": ps.PANE_LIMIT},
                            e: {"pane": ps.PANE_LIMIT}})
        quiet = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, later, host="mac"), manager=m)
        self.assertEqual((quiet["updated"], m.get(first["raised"][0]).revision), ([], settled),
                         "the same seats with a new reset: kept current, never re-notified")
        self.assertIn("It resets at", m.get(first["raised"][0]).message)
        one = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"paused": True}, e: {"paused": True}})
        alone = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, one, host="mac"), manager=m)
        self.assertEqual(alone["closed"], [], "one sweep up closes nothing yet")
        alone = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, one, host="mac"), manager=m)
        self.assertEqual(alone["closed"], first["raised"], "two sweeps without a down pool close it")
        back = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, self.obs(), host="mac"), manager=m)
        self.assertEqual(back, {"raised": [], "updated": [], "closed": []})
        self.assertEqual([r for r in m.active() if (r.subject or {}).get("wedge") == wc.POOL_DOWN], [])


class CodexLoginCard(unittest.TestCase):
    def test_a_signed_out_codex_seat_gets_a_login_card_quoting_its_cell(self):
        with tempfile.TemporaryDirectory() as d:
            ws, wid = Path(d), "d" * 32
            (ws / "state").mkdir()
            (ws / "state" / "roster.json").write_text(json.dumps(
                {"workers": {wid: {"state": "live", "label": "Reviewer", "runtime": "codex"}}}))
            wi.create_worker(ws, runtime="codex", worker_id=wid, tmux_socket="/tmp/codex-card.sock")
            text = pane(SIGNED_OUT[2])

            def tmux(argv, **kw):
                return subprocess.CompletedProcess(argv, 0, text if "capture-pane" in argv else "", "")
            m = HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))
            out = wc.raise_card(ws, wid, ps.CARD_LOGIN, runner=tmux, routed=lambda *a: False, manager=m)
            self.assertEqual(out["outcome"], "carded", out)
            card = m.get(out["hitl_id"])
            self.assertIn(SIGNED_OUT[2], card.message, "the CLI's own words")
            self.assertIn("run codex login", card.message)
            self.assertEqual(card.subject["cause"], [cb.SIGNED_OUT])
            text = IDLE  # signed in again: the same card is not raised for an idle seat
            self.assertEqual(wc.raise_card(ws, wid, ps.CARD_LOGIN, runner=tmux, routed=lambda *a: False,
                                           manager=m)["outcome"], "cleared")


if __name__ == "__main__":
    unittest.main(verbosity=1)
