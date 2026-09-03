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
# Claude Code's Fable weekly-limit dialog, reconstructed from the CLI bundle (v2.1.258),
# not captured live: "Switch to <fallback> and continue" is focused by default.
_FABLE_LIMIT = ("  You've reached your Fable limit\n"
                "  You've used your included Fable usage for this week. Continuing on Fable 5.1 uses\n"
                "  usage credits — you have $0.00 in credits.\n"
                "  ❯ Switch to Opus 5 and continue\n"
                "    Continue with Fable 5.1\n"
                "  Esc to cancel")
# The same dialog under a select-list footer, in case the generic one is not rendered.
_FABLE_LIMIT_SELECT_FOOTER = _FABLE_LIMIT.rsplit("\n", 1)[0] + "\n  ↑/↓ to navigate · Enter to select"
# The caret on the PAYING option (rui, #3739): Enter here spends credits, so this
# must be a human gate however the text around it reads.
_FABLE_LIMIT_UNFOCUSED = _FABLE_LIMIT.replace(
    "  ❯ Switch to Opus 5 and continue\n    Continue with Fable 5.1",
    "    Switch to Opus 5 and continue\n  ❯ Continue with Fable 5.1")
assert _FABLE_LIMIT_UNFOCUSED != _FABLE_LIMIT


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

    def test_fable_limit_flags_as_its_own_kind(self):
        self.assertEqual(classify(_FABLE_LIMIT)[0], "fable-limit")
        self.assertEqual(classify(_FABLE_LIMIT_SELECT_FOOTER)[0], "fable-limit")

    def test_fable_limit_is_a_known_gate_answered_with_enter(self):
        # Enter takes the default-focused "Switch to <fallback> and continue": it
        # spends nothing and keeps the core working (owner 2026-09-02).
        st, _d, _p, kind = compose_state(_FABLE_LIMIT, "working", True)
        self.assertEqual((st, kind), ("blocked-known", "fable-limit"))
        self.assertEqual(auto_answer("fable-limit"), "Enter")

    def test_fable_limit_with_the_caret_elsewhere_is_a_human_gate(self):
        # Same dialog, caret on "Continue with Fable": Enter would spend credits.
        kind, _ = classify(_FABLE_LIMIT_UNFOCUSED)
        self.assertEqual(kind, "fable-limit-unfocused")
        st, _d, _p, k = compose_state(_FABLE_LIMIT_UNFOCUSED, "working", True)
        self.assertEqual((st, k), ("blocked-human", "fable-limit-unfocused"))
        self.assertIsNone(auto_answer("fable-limit-unfocused"))
        self.assertIsNone(_mod.answer_step("blocked-known", "fable-limit-unfocused", "p", None))

    def test_switch_text_without_the_caret_is_not_the_answerable_kind(self):
        # The switch phrase alone (scrollback, an unfocused row) must not read as focus.
        pane = "  Switch to Opus 5 and continue\n  Esc to cancel"
        self.assertNotEqual((classify(pane) or (None,))[0], "fable-limit")

    def test_a_focused_switch_line_on_some_other_dialog_is_never_typed_at(self):
        # sonichi (#3739): a dialog whose own text says it discards work carried the
        # same "Switch to … and continue" row; without the Fable text it is unknown.
        pane = ("  This will discard local changes\n"
                "  ❯ Switch to origin/main and continue\n    Cancel\n  Esc to cancel")
        kind, _ = classify(pane)
        self.assertNotIn(kind, ("fable-limit", "fable-limit-unfocused"))
        self.assertIsNone(auto_answer(kind))

    def test_a_resolved_fable_dialog_above_another_dialog_does_not_vouch_for_it(self):
        # sonichi's residual: the Fable text still inside the tail, then a NEW dialog
        # with its own focused switch row. Co-presence is not adjacency.
        pane = (_FABLE_LIMIT.replace("  ❯ Switch to Opus 5 and continue", "    Switch to Opus 5 and continue")
                .rsplit("\n", 1)[0]
                + "\n  [resolved]\n  This will discard local changes\n"
                  "  ❯ Switch to origin/main and continue\n    Cancel\n  Esc to cancel")
        kind, _ = classify(pane)
        self.assertEqual(kind, "unknown")
        self.assertIsNone(auto_answer(kind))
        # ...while the real dialog, whose body wraps onto a second line, still qualifies.
        wrapped = _FABLE_LIMIT.replace("uses\n  usage credits", "uses\n  usage\n  credits")
        self.assertEqual(classify(wrapped)[0], "fable-limit")

    def test_fable_limit_and_session_limit_stay_distinct(self):
        # One is a switch the monitor may take; the other is a wait/spend decision.
        self.assertEqual(classify(_FABLE_LIMIT)[0], "fable-limit")
        self.assertEqual(classify(_SESSION_LIMIT)[0], "session-limit")
        self.assertIsNone(auto_answer("session-limit"))

    def test_unforeseen_prompt_surfaces_as_unknown(self):
        # No matching signature, but an input affordance is present and it is NOT
        # the idle prompt → must surface as "unknown" (owner's no-dead-end rule),
        # never fall through silently.
        novel = "  Overwrite the existing config file?\n  (Enter to confirm · Esc to cancel)"
        hit = classify(novel)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "unknown")


class TestUnobservedProbeIsNotHung(unittest.TestCase):
    """An unobserved process probe (tmux refused the client, binary missing) must
    hold, never become the RECOVER-facing `hung`; only a SEEN session with stale
    status is `hung`, and a server that answered "no session" is `crashed`."""

    def test_unobserved_probe_holds(self):
        st, detail, _p, kind = compose_state("", "unknown", True, process=None)
        self.assertEqual(st, "unobserved")
        self.assertIn("unobserved", detail)
        self.assertNotEqual(st, "hung")

    def test_present_and_stale_is_still_hung(self):
        st, *_ = compose_state("Running step 3...\n(no prompt, no footer)", "unknown", True, process=True)
        self.assertEqual(st, "hung")

    def test_definitive_absence_is_crashed(self):
        st, *_ = compose_state("", "offline", True, process=False)
        self.assertEqual(st, "crashed")

    def test_refused_client_through_derive_and_compose(self):
        """Full path: refused tmux client → runtime_health.derive() → compose_state()."""
        import subprocess as _sp
        import sys as _sys
        import tempfile
        from unittest import mock
        _sys.path.insert(0, os.path.join(_HERE, "..", "src"))
        import tmux_probe  # noqa: E402
        rh_spec = importlib.util.spec_from_file_location(
            "runtime_health_pin", os.path.join(_HERE, "..", "src", "runtime-health.py"))
        rh = importlib.util.module_from_spec(rh_spec); rh_spec.loader.exec_module(rh)
        tmp = tempfile.mkdtemp()
        # Every unrelated probe held healthy so only the process signal varies.
        with mock.patch.object(rh, "_resolve_workspace", lambda repo: tmp), \
             mock.patch.object(rh, "_gateway_running", lambda: True), \
             mock.patch.object(rh, "_ag2space_app_running", lambda: True), \
             mock.patch.object(rh, "_station_cached", lambda ws: True), \
             mock.patch.object(rh, "_heartbeat_fresh", lambda ws: True):
            def refused(*a, **k):
                return _sp.CompletedProcess(["tmux"], 1, "", "server exited unexpectedly\n")
            with mock.patch.object(tmux_probe.subprocess, "run", refused):
                base = rh.derive()
            self.assertEqual(base["health"], "unknown")
            self.assertIsNone(base["signals"]["process"])
            st, *_ = compose_state("", base["health"], True, process=base["signals"]["process"])
            self.assertEqual(st, "unobserved")

            def gone(*a, **k):
                return _sp.CompletedProcess(["tmux"], 1, "", "can't find session: sutando-core\n")
            with mock.patch.object(tmux_probe.subprocess, "run", gone):
                base = rh.derive()
            self.assertEqual(base["health"], "offline")
            self.assertIs(base["signals"]["process"], False)
            st, *_ = compose_state("", base["health"], True, process=base["signals"]["process"])
            self.assertEqual(st, "crashed")


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

    def test_allowlist_is_exactly_press_enter_and_fable_limit(self):
        self.assertEqual(auto_answer("press-enter"), "Enter")
        self.assertEqual(auto_answer("fable-limit"), "Enter")
        self.assertEqual(set(_mod._AUTO_ANSWER), {"press-enter", "fable-limit"})

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


class TestAnswerStep(unittest.TestCase):
    """The actor's pure half: a settled, allowlisted gate gets one key per instance."""

    def test_sends_enter_for_a_settled_fable_limit(self):
        self.assertEqual(_mod.answer_step("blocked-known", "fable-limit", "p1", None), "Enter")

    def test_same_prompt_instance_is_answered_once(self):
        self.assertIsNone(_mod.answer_step("blocked-known", "fable-limit", "p1", "p1"))
        self.assertEqual(_mod.answer_step("blocked-known", "fable-limit", "p2", "p1"), "Enter")

    def test_human_gates_are_never_typed_at(self):
        for kind in ("login", "session-limit", "fable-limit-unfocused", "permission", "selection", "unknown"):
            self.assertIsNone(_mod.answer_step("blocked-known", kind, "p", None), kind)
        self.assertIsNone(_mod.answer_step("blocked-human", "fable-limit", "p", None))

    def test_a_settling_prompt_is_not_answered(self):
        self.assertIsNone(_mod.answer_step("running", "fable-limit", None, None))

    def test_disabled_flag_reports_only(self):
        self.assertIsNone(_mod.answer_step("blocked-known", "fable-limit", "p1", None, enabled=False))


class TestMainAutoAnswerWiring(unittest.TestCase):
    """One --once tick against a Fable-limit pane: the key is typed through send_keys
    and the signal file records it; --no-auto-answer only reports."""

    def _tick(self, extra_args, pane=None):
        import sys
        import tempfile
        from unittest.mock import patch
        sent = []
        pane = _FABLE_LIMIT if pane is None else pane
        out = os.path.join(tempfile.mkdtemp(), "core-supervisor.json")

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "working"}
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", out,
                "--once", "--stable", "1"] + extra_args
        with patch.object(_mod, "capture", lambda s, sess: pane), \
                patch.object(_mod, "_load_runtime_health", lambda: _RH()), \
                patch.object(_mod, "gateway_alive", lambda *a: True), \
                patch.object(_mod, "_ensure_tmux_on_path", lambda: None), \
                patch.object(_mod, "send_keys", lambda s, sess, k: sent.append((s, sess, k)) or True), \
                patch.object(sys, "argv", argv):
            main()
        with open(out) as f:
            return sent, json.load(f)

    def test_fable_limit_is_typed_at_and_recorded(self):
        sent, payload = self._tick([])
        self.assertEqual(sent, [("/tmp/x.sock", "sutando-core", "Enter")])
        self.assertEqual(payload["kind"], "fable-limit")
        self.assertEqual(payload["auto_answered"]["kind"], "fable-limit")
        self.assertEqual(payload["auto_answered"]["key"], "Enter")

    def test_caret_on_the_paying_option_is_escalated_never_typed(self):
        sent, payload = self._tick([], pane=_FABLE_LIMIT_UNFOCUSED)
        self.assertEqual(sent, [])
        self.assertEqual((payload["state"], payload["kind"]), ("blocked-human", "fable-limit-unfocused"))
        self.assertNotIn("auto_answered", payload)

    def test_an_expired_answer_record_is_dropped_from_the_signal(self):
        # The record rides along for AUTO_ANSWER_CARRY_S; past that it is gone.
        from unittest.mock import patch
        with patch.object(_mod, "AUTO_ANSWER_CARRY_S", -1.0):
            sent, payload = self._tick([])
        self.assertEqual(len(sent), 1)
        self.assertNotIn("auto_answered", payload)

    def test_no_auto_answer_flag_reports_only(self):
        sent, payload = self._tick(["--no-auto-answer"])
        self.assertEqual(sent, [])
        self.assertEqual(payload["kind"], "fable-limit")
        self.assertNotIn("auto_answered", payload)


class TestSendKeys(unittest.TestCase):
    """send_keys reports what tmux did: True only on a zero exit, False on a
    non-zero exit or when tmux cannot be run at all — never an exception."""

    def _with_fake_tmux(self, script):
        import stat
        import tempfile
        d = tempfile.mkdtemp()
        log = os.path.join(d, "argv.log")
        p = os.path.join(d, "tmux")
        with open(p, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + json.dumps(log) + "\n" + script + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return d, log

    def test_zero_exit_is_true_and_the_key_reaches_the_session_pane(self):
        from unittest.mock import patch
        d, log = self._with_fake_tmux("exit 0")
        with patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertTrue(_mod.send_keys("/tmp/x.sock", "sutando-core", "Enter"))
        with open(log) as f:
            self.assertEqual(f.read().split("\n")[:6],
                             ["-S", "/tmp/x.sock", "send-keys", "-t", "sutando-core:0", "Enter"])

    def test_non_zero_exit_is_false(self):
        from unittest.mock import patch
        d, _ = self._with_fake_tmux("exit 1")
        with patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertFalse(_mod.send_keys("/tmp/x.sock", "sutando-core", "Enter"))

    def test_an_unrunnable_tmux_is_false_not_an_exception(self):
        from unittest.mock import patch
        with patch.object(_mod.subprocess, "run", side_effect=OSError("no tmux")):
            self.assertFalse(_mod.send_keys("/tmp/x.sock", "sutando-core", "Enter"))


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
