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


class AccountLabels(unittest.TestCase):
    def test_claude_label_is_name_and_plan_never_the_email(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".claude.json").write_text(json.dumps({"oauthAccount": {
                "displayName": "Ana a@b.co", "planDisplayName": "Claude Max", "emailAddress": "a@b.co",
                "accountUuid": "u"}}))
            self.assertEqual(clh.account_label(d), "Ana · Claude Max")
            (Path(d) / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "a@b.co"}}))
            self.assertIsNone(clh.account_label(d), "an email alone is no label")
        self.assertIsNone(clh.account_label("/no/such/dir"))
        self.assertIsNone(clh.account_label(None))

    def test_codex_label_from_login_status(self):
        def status(out):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, "")
        self.assertEqual(wc.codex_account_label(status("Logged in using ChatGPT\n")), "ChatGPT login")
        self.assertEqual(wc.codex_account_label(status("Logged in using ChatGPT (a@b.co)\n")), "ChatGPT (a@b.co) login"
                         .replace(" (a@b.co)", ""), "an email word is dropped")
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
                                  "Ana · Claude Max" if runtime == "claude" else "ChatGPT login")
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
        self.assertEqual(g["account_label"], "Ana · Claude Max")
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
                         [("pool-down:mac:codex-signed-out", "ChatGPT login")])
        self.assertIn("codex login", got[0]["remedy"])
        self.assertIsNone(got[0]["reset_at"])

    def test_one_card_per_down_pool_resolved_when_it_works_again(self):
        a, b, _ = list(self.SEATS)
        m = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        down = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"pane": ps.PANE_LIMIT}})
        first = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, down, host="mac"), manager=m)
        self.assertEqual(len(first["raised"]), 1)
        card = m.get(first["raised"][0])
        self.assertIn("All 2 Claude workers on mac are stopped", card.message)
        self.assertEqual(card.subject["pool_down"]["id"], "pool-down:mac:claude-usage-limit")
        again = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, down, host="mac"), manager=m)
        self.assertEqual(again, {"raised": [], "closed": []}, "the same pool down raises nothing new")
        e = "e" * 32
        self.SEATS = dict(self.SEATS, **{e: ("Rams", "claude")})
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {
            w: {"state": "live", "label": n, "runtime": r} for w, (n, r) in self.SEATS.items()}}))
        grown = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"pane": ps.PANE_LIMIT}, e: {"pane": ps.PANE_LIMIT}})
        moved = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, grown, host="mac"), manager=m)
        self.assertEqual((len(moved["raised"]), moved["closed"]), (1, first["raised"]), "a new set of seats is a new card")
        one = self.obs(**{a: {"pane": ps.PANE_LIMIT}, b: {"paused": True}, e: {"paused": True}})
        alone = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, one, host="mac"), manager=m)
        self.assertEqual((alone["raised"], alone["closed"]), ([], moved["raised"]), "one seat left: its own card")
        back = wc.raise_pool_down(self.ws, wc.pool_down(self.ws, self.obs(), host="mac"), manager=m)
        self.assertEqual(back, {"raised": [], "closed": []})
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
