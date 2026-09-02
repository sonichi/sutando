#!/usr/bin/env python3
"""Tests for src/core-input-watch.py — the core supervisor MONITOR (M1).

classify() fixtures are the ACTUAL tmux panes captured from the bundled core on
the Mac mini 2026-07-14 while driving the first-run gates by hand
(bypass-permissions, /login, paste-code, login-success) plus the two states that
must NEVER flag: the idle "ready for a task" prompt and normal agent output.

compose_state() tests exercise the full state machine (crashed / blocked-human /
blocked-known / logged-out / gateway-down / idle-ready / running / hung).

Run: python3 tests/core-input-watch.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-input-watch.py")
_spec = importlib.util.spec_from_file_location("core_input_watch", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
compose_state = _mod.compose_state
auto_answer = _mod.auto_answer
main = _mod.main

_BYPASS = ("  in Bypass Permissions mode.\n  https://code.claude.com/docs/en/security\n"
           "  ❯ 1. No, exit\n    2. Yes, I accept\n  Enter to confirm · Esc to cancel")
_LOGIN_MENU = ("  Login\n  Select login method:\n  ❯ 1. Claude account with subscription\n"
               "    2. Anthropic Console account\n  Esc to cancel")
_PASTE = ("  Login\n  Browser didn't open? Use the url below to sign in\n"
          "https://claude.com/cai/oauth/authorize?code=true\n"
          "  Paste code here if prompted >\n  Esc to cancel")
_PRESS_ENTER = ("  Login\n  Logged in as x@example.com\n  Login successful. Press Enter to continue…")
_IDLE = ("──────── sutando-core ──\n❯ \n────────\n"
         "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
_IDLE_LOGGEDOUT = _IDLE + "\n     Not logged in · Run /login"
_WORKING = ("⏺ Bash(WS=... echo WORKSPACE=...)\n  ⎿  WORKSPACE=/Users/x\n     HOST=mini\n     … +39 lines")
# The usage-limit screen (owner screenshot 2026-09-02, Qingyuns-MacBook-Pro core):
# a wait/spend decision that the relay had been reporting as a LOGIN gate.
_SESSION_LIMIT = ("● Monitor event: \"Streaming task watcher\"\n"
                  "  ⎿  You've hit your session limit · resets 12:10pm\n"
                  "     (America/Los_Angeles)\n"
                  "     /usage-credits to finish what you're working on.\n"
                  "  Continuing automatically at 12:10pm · esc to cancel")
# A real mid-session permission prompt rendered ABOVE the persistent idle footer
# (review repro 2026-07-14). The footer's await-affordance must NOT suppress it.
_PERMISSION_WITH_FOOTER = (
    "  Do you want to proceed?\n  Allow this action\n  Esc to cancel\n"
    "  ────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")


class TestClassify(unittest.TestCase):
    def test_bypass_flags(self):
        self.assertEqual(classify(_BYPASS)[0], "bypass-permissions")

    def test_login_menu_flags(self):
        self.assertEqual(classify(_LOGIN_MENU)[0], "login")

    def test_paste_flags(self):
        self.assertEqual(classify(_PASTE)[0], "login")

    def test_press_enter_flags(self):
        self.assertEqual(classify(_PRESS_ENTER)[0], "press-enter")

    def test_idle_does_not_flag(self):
        self.assertIsNone(classify(_IDLE))

    def test_working_does_not_flag(self):
        self.assertIsNone(classify(_WORKING))

    def test_empty_does_not_flag(self):
        self.assertIsNone(classify(""))

    def test_permission_prompt_above_footer_is_not_suppressed(self):
        # Regression (review 2026-07-14): the idle-footer suppression must not hide a
        # real permission prompt just because the persistent footer is also present.
        hit = classify(_PERMISSION_WITH_FOOTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "permission")

    def test_permission_with_footer_composes_blocked_human(self):
        st, _d, prompt, kind = compose_state(_PERMISSION_WITH_FOOTER, "working", True)
        self.assertEqual(st, "blocked-human")
        self.assertEqual(kind, "permission")
        self.assertIsNotNone(prompt)

    def test_idle_with_await_token_still_does_not_flag(self):
        # The idle prompt can carry an await-hint-like token ("to accept") without
        # being a real gate — the _IDLE guard must still suppress it (never nag the
        # user on the ready-for-a-task prompt).
        idle_hint = ("  ⏵⏵ bypass permissions on (shift+tab to cycle) · "
                     "press tab to accept · ← for agents")
        self.assertIsNone(classify(idle_hint))

    def test_session_limit_flags_as_its_own_kind(self):
        self.assertEqual(classify(_SESSION_LIMIT)[0], "session-limit")

    def test_session_limit_wins_over_stale_login_text_above_it(self):
        # The live misfire: an earlier /login menu still in the pane scrollback
        # made the limit screen read as "login". The limit must win.
        pane = _LOGIN_MENU + "\n" + _SESSION_LIMIT
        self.assertEqual(classify(pane)[0], "session-limit")

    def test_stale_limit_text_does_not_outrank_a_live_login_gate(self):
        # The symmetric twin (rui + sonichi, #3730): a full limit screen left in
        # scrollback above a live login menu; the live gate is the lower one.
        pane = _SESSION_LIMIT + "\n" + _LOGIN_MENU
        self.assertEqual(classify(pane)[0], "login")

    def test_session_limit_is_a_human_gate_never_auto_answered(self):
        st, detail, _p, kind = compose_state(_SESSION_LIMIT, "working", True)
        self.assertEqual((st, kind), ("blocked-human", "session-limit"))
        self.assertIn("session-limit", detail)
        self.assertIsNone(auto_answer("session-limit"))

    def test_unforeseen_prompt_surfaces_as_unknown(self):
        # No matching signature, but an input affordance is present and it is NOT
        # the idle prompt → must surface as "unknown" (owner's no-dead-end rule),
        # never fall through silently.
        novel = "  Overwrite the existing config file?\n  (Enter to confirm · Esc to cancel)"
        hit = classify(novel)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "unknown")


class TestComposeState(unittest.TestCase):
    """compose_state REFINES runtime-health's coarse `base_health` (one shared
    derivation, #2092) into the 8 supervisor states — signature is
    (pane, base_health, gateway_alive). base_health ∈
    {offline, needs_login, working, idle, unknown}."""

    def test_crashed_when_base_offline(self):
        # runtime-health "offline" (no session) dominates everything.
        st, *_ = compose_state(_WORKING, "offline", gateway_alive=True)
        self.assertEqual(st, "crashed")

    def test_blocked_human_on_login(self):
        # An ACTIVE /login menu is finer than the coarse health → carry the prompt.
        st, detail, prompt, kind = compose_state(_LOGIN_MENU, "working", True)
        self.assertEqual(st, "blocked-human")
        self.assertEqual(kind, "login")
        self.assertIsNotNone(prompt)

    def test_blocked_known_on_bypass(self):
        st, _d, _p, kind = compose_state(_BYPASS, "working", True)
        self.assertEqual(st, "blocked-known")
        self.assertEqual(kind, "bypass-permissions")

    def test_logged_out_when_base_needs_login_no_active_gate(self):
        # Passive "Not logged in · Run /login" banner (no active menu) → the
        # base needs_login maps straight to logged-out.
        st, *_ = compose_state(_IDLE_LOGGEDOUT, "needs_login", True)
        self.assertEqual(st, "logged-out")

    def test_gateway_down_when_base_ok_gateway_dead(self):
        st, *_ = compose_state(_IDLE, "idle", gateway_alive=False)
        self.assertEqual(st, "gateway-down")

    def test_idle_ready_when_base_idle(self):
        st, *_ = compose_state(_IDLE, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_running_when_base_working(self):
        st, *_ = compose_state(_WORKING, "working", True)
        self.assertEqual(st, "running")

    def test_hung_when_base_unknown(self):
        # runtime-health "unknown" = live session but stale/absent core-status
        # (wedged loop) → the supervisor's hung, carrying the pane tail. Uses a
        # WORKING (no-affordance) pane: no idle footer → genuinely wedged.
        st, _d, prompt, _k = compose_state(_WORKING, "unknown", True)
        self.assertEqual(st, "hung")
        self.assertIsNotNone(prompt)

    def test_idle_ready_overrides_stale_status_when_pane_is_idle(self):
        # #2112: a healthy core sitting at its idle prompt writes core-status
        # rarely, so runtime-health goes "unknown" (stale status) and WOULD be
        # falsely flagged hung (→ spurious ESCALATE / RECOVER). When the pane
        # POSITIVELY shows the idle-ready footer, trust it: idle, not wedged.
        idle_footer = ("prior output\n\n"
                       "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
        st, _d, _p, _k = compose_state(idle_footer, "unknown", True)
        self.assertEqual(st, "idle-ready")
        # A no-affordance pane (mid-work / frozen) with the same stale status must
        # STILL read hung — the override is positive-idle-only, so genuine wedge
        # detection is preserved.
        st2, *_ = compose_state("Running step 3...\n(no prompt, no footer)", "unknown", True)
        self.assertEqual(st2, "hung")


class TestAutoAnswer(unittest.TestCase):
    """M4 decision safety: only strictly-safe gates auto-answer; all else escalates."""

    def test_press_enter_is_the_only_auto_answer(self):
        self.assertEqual(auto_answer("press-enter"), "Enter")

    def test_login_never_auto_answered(self):
        self.assertIsNone(auto_answer("login"))

    def test_unknown_never_auto_answered(self):
        # The no-dead-end catch-all must escalate, never guess a keystroke.
        self.assertIsNone(auto_answer("unknown"))

    def test_selection_and_permission_never_auto_answered(self):
        self.assertIsNone(auto_answer("selection"))
        self.assertIsNone(auto_answer("permission"))

    def test_trust_and_bypass_escalate_not_auto_accepted(self):
        # Handled by PREVENT seeds; if they surface at runtime we ESCALATE — never
        # auto-accept a trust / dangerous-mode prompt without explicit opt-in.
        self.assertIsNone(auto_answer("folder-trust"))
        self.assertIsNone(auto_answer("bypass-permissions"))


class TestEnsureTmuxOnPath(unittest.TestCase):
    """Mini-verified 2026-07-14: a detached spawn without Homebrew on PATH made bare
    `tmux` fail → healthy core mis-read as crashed. The monitor must self-heal PATH."""

    def test_prepends_tmux_dir_when_not_on_path(self):
        import sys
        orig_path = os.environ.get("PATH", "")
        orig_which = _mod.shutil.which
        orig_exists = _mod.os.path.exists
        try:
            _mod.shutil.which = lambda _n: None            # tmux not on PATH
            _mod.os.path.exists = lambda p: p == "/opt/homebrew/bin/tmux"
            os.environ["PATH"] = "/usr/bin"
            _mod._ensure_tmux_on_path()
            self.assertIn("/opt/homebrew/bin", os.environ["PATH"].split(os.pathsep))
        finally:
            _mod.shutil.which = orig_which
            _mod.os.path.exists = orig_exists
            os.environ["PATH"] = orig_path

    def test_noop_when_tmux_already_resolvable(self):
        orig_path = os.environ.get("PATH", "")
        orig_which = _mod.shutil.which
        try:
            _mod.shutil.which = lambda _n: "/usr/bin/tmux"  # already found
            os.environ["PATH"] = "/usr/bin"
            _mod._ensure_tmux_on_path()
            self.assertEqual(os.environ["PATH"], "/usr/bin")  # untouched
        finally:
            _mod.shutil.which = orig_which
            os.environ["PATH"] = orig_path


class TestMainOnce(unittest.TestCase):
    """End-to-end --once through the SHARED runtime-health derivation: with no
    live sutando-core, runtime_health.derive() → 'offline' → the supervisor
    writes state 'crashed'. Exercises main()/_load_runtime_health()/capture()/
    gateway_alive()/_atomic_write() in-process (not just classify/compose)."""

    def test_once_writes_crashed_with_no_core(self):
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "state", "core-supervisor.json")
            argv = ["core-input-watch.py", "--socket",
                    os.path.join(td, "nope.sock"), "--out", out, "--once"]
            old = sys.argv
            sys.argv = argv
            try:
                main()
            finally:
                sys.argv = old
            with open(out) as f:
                sig = json.load(f)
        self.assertEqual(sig["state"], "crashed")
        self.assertEqual(sig["session"], "sutando-core")

    def test_once_blocked_prompt_debounces_on_first_tick(self):
        """A fresh gate with --stable 2 must NOT escalate on the first tick — the
        debounce holds it as 'running (settling)' until the prompt persists. Drives
        the settling branch + the --app-data gateway probe in-process."""
        import sys
        import tempfile

        class _FakeRH:  # stand in for runtime-health: session alive + working
            TMUX_SOCKET = None
            SESSION = None

            def derive(self):
                return {"health": "working"}

        orig_cap, orig_load = _mod.capture, _mod._load_runtime_health
        _mod.capture = lambda sock, sess: _BYPASS          # a recognized gate
        _mod._load_runtime_health = lambda: _FakeRH()
        try:
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "state", "core-supervisor.json")
                argv = ["core-input-watch.py", "--socket", "/x", "--out", out,
                        "--app-data", td, "--stable", "2", "--once"]
                old = sys.argv
                sys.argv = argv
                try:
                    main()
                finally:
                    sys.argv = old
                with open(out) as f:
                    sig = json.load(f)
        finally:
            _mod.capture, _mod._load_runtime_health = orig_cap, orig_load
        # stable=2, first tick → prompt seen once (< 2) → debounced to running.
        self.assertEqual(sig["state"], "running")


if __name__ == "__main__":
    unittest.main()
