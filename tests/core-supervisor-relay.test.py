#!/usr/bin/env python3
"""Tests for src/core-supervisor-relay.py — the COMMUNICATOR (outbound ESCALATE).

Covers the pure decision (which states escalate), the debounce (a persistent
prompt fires once; a new prompt re-fires), message composition, and one full
--dry-run CLI cycle. Signal fixtures match the monitor's core-supervisor.json
schema exactly.

Run: python3 tests/core-supervisor-relay.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import contextlib
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-supervisor-relay.py")
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
_spec = importlib.util.spec_from_file_location("core_supervisor_relay", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
should_escalate = _mod.should_escalate
compose_message = _mod.compose_message
run_cycle = _mod.run_cycle
main = _mod.main
resolve_active_target = _mod.resolve_active_target

import channel_env_containment  # noqa: E402 — the shared module the probe delegates to
import progress_route  # noqa: E402 — the shared route verdict the relay delegates to

_LOGIN = {"state": "blocked-human", "detail": "awaiting user: login",
          "prompt": "Login\nSelect login method:\n  1. Claude account", "kind": "login"}
_LIMIT = {"state": "blocked-human", "detail": "awaiting user: session-limit",
          "prompt": "You've hit your session limit · resets 12:10pm\n"
                    "/usage-credits to finish what you're working on.\n"
                    "Continuing automatically at 12:10pm · esc to cancel",
          "kind": "session-limit"}
_LOGGED_OUT = {"state": "logged-out", "detail": "core not authenticated (needs /login)",
               "prompt": None, "kind": None}
# The monitor's refused-turn signal (#4015): the core sits at its idle prompt and every turn
# it is sent ends in this line. The prompt IS the line, so the remedy is chosen from it.
_REFUSED_LINE = ("You're out of usage credits. Run /usage-credits to keep using Fable 5.1 "
                 "or /model to switch models.")
_REFUSED = {"state": "blocked-human", "detail": "awaiting user: turn-rejected",
            "prompt": _REFUSED_LINE, "kind": "turn-rejected"}
_IDLE = {"state": "idle-ready", "detail": "ready for a task", "prompt": None, "kind": None}
_RUNNING = {"state": "running", "detail": "actively processing", "prompt": None, "kind": None}
_CRASHED = {"state": "crashed", "detail": "core process/session not found", "prompt": None}
_HUNG = {"state": "hung", "detail": "core alive but stalled", "prompt": "…", "kind": "unknown"}
# The monitor answered the Fable weekly-limit dialog itself (Enter = switch model);
# the record rides along in the signal for AUTO_ANSWER_CARRY_S whatever the state.
_FABLE_AUTO = {"state": "blocked-known", "detail": "at known gate: fable-limit",
               "prompt": "You've reached your Fable limit\n❯ Switch to Opus 5 and continue",
               "kind": "fable-limit",
               "auto_answered": {"kind": "fable-limit", "key": "Enter", "at": 1788380000.0}}
_FABLE_AUTO_LATER = dict(_IDLE, auto_answered=_FABLE_AUTO["auto_answered"])
_PRESS_ENTER_AUTO = dict(_RUNNING, auto_answered={"kind": "press-enter", "key": "Enter", "at": 1.0})


@contextlib.contextmanager
def _backend(value):
    """Pin what the core's recorded backend looks like. Stubbed rather than written to
    disk so the assertions do not depend on the live workspace or this host's name."""
    orig = getattr(_mod, "_derive_backend", None)
    _mod._derive_backend = lambda: value
    try:
        yield
    finally:
        if orig is not None:
            _mod._derive_backend = orig


def _no_backend():
    return _backend(None)


def _backend_raises():
    def _boom():
        raise RuntimeError("unreadable")
    return _backend_fn(_boom)


@contextlib.contextmanager
def _backend_fn(fn):
    orig = getattr(_mod, "_derive_backend", None)
    _mod._derive_backend = fn
    try:
        yield
    finally:
        if orig is not None:
            _mod._derive_backend = orig


class TestShouldEscalate(unittest.TestCase):
    def test_login_escalates(self):
        esc, h = should_escalate(_LOGIN, None)
        self.assertTrue(esc)
        self.assertIsNotNone(h)

    def test_logged_out_escalates(self):
        self.assertTrue(should_escalate(_LOGGED_OUT, None)[0])

    def test_idle_and_running_never_escalate(self):
        self.assertFalse(should_escalate(_IDLE, None)[0])
        self.assertFalse(should_escalate(_RUNNING, None)[0])

    def test_crashed_and_hung_go_to_recover_not_user(self):
        # RECOVER (restart) handles these, not user-escalation → no notification.
        self.assertFalse(should_escalate(_CRASHED, None)[0])
        self.assertFalse(should_escalate(_HUNG, None)[0])

    def test_debounce_same_prompt_fires_once(self):
        esc1, h1 = should_escalate(_LOGIN, None)
        self.assertTrue(esc1)
        esc2, h2 = should_escalate(_LOGIN, h1)  # same prompt already escalated
        self.assertFalse(esc2)
        self.assertEqual(h1, h2)

    def test_new_prompt_reescalates(self):
        _, h1 = should_escalate(_LOGIN, None)
        other = {"state": "blocked-human", "detail": "awaiting user: permission",
                 "prompt": "Do you want to proceed?", "kind": "permission"}
        esc, h2 = should_escalate(other, h1)
        self.assertTrue(esc)
        self.assertNotEqual(h1, h2)

    def test_healthy_tick_preserves_last_hash(self):
        # A transient healthy tick between two identical blockers must NOT reset the
        # debounce (else the same login would double-notify).
        _, h1 = should_escalate(_LOGIN, None)
        _, h_mid = should_escalate(_RUNNING, h1)
        self.assertEqual(h_mid, h1)
        self.assertFalse(should_escalate(_LOGIN, h_mid)[0])


    def test_fable_auto_answer_notifies_once_across_states(self):
        esc, h = should_escalate(_FABLE_AUTO, None)
        self.assertTrue(esc)
        # The same record carried on a later idle tick must not fire again.
        esc2, h2 = should_escalate(_FABLE_AUTO_LATER, h)
        self.assertFalse(esc2)
        self.assertEqual(h, h2)

    def test_a_new_fable_answer_notifies_again(self):
        _, h = should_escalate(_FABLE_AUTO, None)
        again = dict(_FABLE_AUTO, auto_answered=dict(_FABLE_AUTO["auto_answered"], at=1788390000.0))
        self.assertTrue(should_escalate(again, h)[0])

    def test_a_plain_known_gate_still_never_escalates(self):
        plain = {k: v for k, v in _FABLE_AUTO.items() if k != "auto_answered"}
        self.assertFalse(should_escalate(plain, None)[0])

    def test_press_enter_auto_answer_is_silent(self):
        self.assertFalse(should_escalate(_PRESS_ENTER_AUTO, None)[0])


class TestComposeMessage(unittest.TestCase):
    def test_fable_auto_answer_says_what_was_pressed_not_needs_you(self):
        msg = compose_message(_FABLE_AUTO)
        self.assertIn("Fable weekly limit", msg)
        self.assertIn("pressed Enter", msg)
        self.assertIn("fallback model", msg)
        self.assertNotIn("Agent needs you", msg)
        self.assertNotIn("/login", msg)
        # Carried onto a later idle tick, the notice reads the same.
        self.assertEqual(compose_message(_FABLE_AUTO_LATER), msg)

    def test_fable_limit_with_the_caret_elsewhere_escalates_with_the_reason(self):
        unfocused = {"state": "blocked-human", "detail": "awaiting user: fable-limit-unfocused",
                     "prompt": "You've reached your Fable limit\n❯ Continue with Fable 5.1",
                     "kind": "fable-limit-unfocused"}
        self.assertTrue(should_escalate(unfocused, None)[0])
        msg = compose_message(unfocused)
        self.assertIn("Agent needs you", msg)
        self.assertIn("will not press Enter", msg)
        self.assertNotIn("/login", msg)

    def test_includes_detail_and_prompt_excerpt(self):
        m = compose_message(_LOGIN)
        self.assertIn("awaiting user: login", m)
        self.assertIn("Login", m)  # first prompt line
        self.assertIn("resolve", m)

    def test_login_pane_excerpt_is_the_prompt_not_the_chrome(self):
        # The pane's first non-empty line is a box rule and the next an OAuth URL fragment; the
        # one-line notice must quote the prompt (same filter as the escalation card).
        pane = ("╭──────────────── sutando-core ─╮\n"
                "https://claude.ai/oauth/authorize?code=true&code_challenge=ncifI5jOgzI138TpX&state=uv\n"
                "Browser didn't open? Use the url below to sign in:\n"
                "Paste code here if prompted >\n")
        m = compose_message(dict(_LOGIN, prompt=pane))
        self.assertIn("Use the url below to sign in", m)
        for noise in ("╭", "───", "https://", "code_challenge"):
            self.assertNotIn(noise, m)

    def test_the_excerpt_falls_back_to_the_first_line_when_the_filter_cannot_load(self):
        # The notice is the owner's only channel: a missing filter module must not drop it.
        pane = "╭─ rule ─╮\nBrowser didn't open? Use the url below to sign in:\n"
        with patch.dict(sys.modules, {"prompt_excerpt": None}):
            m = compose_message(dict(_LOGIN, prompt=pane))
        self.assertIn("╭─ rule ─╮", m)  # the old first-non-empty line, degraded but delivered

    def test_the_filter_is_found_from_the_relays_own_directory(self):
        # Run as a script, sys.path[0] is not src/: the relay must add its own directory.
        src_dir = os.path.dirname(_SRC)
        saved = list(sys.path)
        try:
            sys.path[:] = [p for p in sys.path if os.path.abspath(p) != os.path.abspath(src_dir)]
            sys.modules.pop("prompt_excerpt", None)
            m = compose_message(dict(_LOGIN, prompt="╭─ rule ─╮\nUse the url below to sign in:\n"))
        finally:
            sys.path[:] = saved
        self.assertIn("Use the url below to sign in", m)

    def test_handles_no_prompt(self):
        m = compose_message(_LOGGED_OUT)
        self.assertIn("not authenticated", m)
        self.assertTrue(m.endswith("resolve this."))

    # Login-class states (sonichi#2397): the remedy is a GUI /login on the host —
    # "reply here or open the app" prescribes actions that cannot clear them.
    def test_logged_out_names_gui_login_remedy(self):
        m = compose_message(_LOGGED_OUT)
        self.assertIn("GUI /login", m)
        self.assertNotIn("reply here or open the app", m)

    def test_login_prompt_names_gui_login_remedy(self):
        m = compose_message(_LOGIN)
        self.assertIn("GUI /login", m)
        self.assertNotIn("reply here or open the app", m)

    def test_session_limit_escalates(self):
        self.assertTrue(should_escalate(_LIMIT, None)[0])

    def test_session_limit_names_the_reset_time_not_login(self):
        m = compose_message(_LIMIT)
        self.assertIn("resumes on its own at 12:10pm", m)
        self.assertIn("/usage-credits", m)
        # The owner named this third route (2026-09-02): the limit is per
        # subscription, so signing in under another one is often the fastest.
        self.assertIn("different subscription", m)
        self.assertNotIn("/login", m)
        self.assertNotIn("restart.sh", m)

    def test_session_limit_without_a_reset_time_still_avoids_login(self):
        sig = dict(_LIMIT, prompt="You've hit your session limit")
        m = compose_message(sig)
        self.assertIn("when the limit window resets", m)
        self.assertNotIn("/login", m)

    def test_turn_rejected_escalates(self):
        self.assertTrue(should_escalate(_REFUSED, None)[0])

    def test_turn_rejected_says_every_turn_is_refused_and_names_the_line(self):
        m = compose_message(_REFUSED)
        self.assertIn("refuses every turn", m)
        self.assertIn("out of usage credits", m)
        # A credit line gets the limit remedy, never the login one.
        self.assertIn("/usage-credits", m)
        self.assertIn("not a login problem", m)
        self.assertNotIn("GUI /login", m)
        self.assertNotIn("restart.sh", m)

    def test_turn_rejected_login_line_names_gui_login_remedy(self):
        sig = dict(_REFUSED, prompt="OAuth access token has expired · Please run /login")
        m = compose_message(sig)
        self.assertIn("refuses every turn", m)
        self.assertIn("OAuth access token has expired", m)
        self.assertIn("GUI /login", m)
        self.assertNotIn("usage limit", m)

    def test_turn_rejected_unknown_line_falls_back_to_the_terminal(self):
        sig = dict(_REFUSED, prompt="Something else the CLI refused with")
        with _no_backend():
            m = compose_message(sig)
        self.assertIn("refuses every turn", m)
        self.assertIn("where the core is running", m)

    _PICKER = "Select model\n  1. Opus\n❯ 2. Sonnet\n  3. Haiku\n  Enter to confirm · Esc to cancel"
    _YESNO = "Allow Bash(rm -rf build)?\n❯ 1. Yes\n  2. No\n  Esc to cancel"

    def test_a_selection_or_permission_gate_names_the_choice_card_first_on_ag2space(self):
        """A numbered picker or a permission dialog is projected to the owner's AG2 Space DM
        as a HITL card whose buttons type the answer; the notice says so before the
        terminal. A typed reply still cannot answer it."""
        for kind, prompt in (("selection", self._PICKER), ("permission", self._YESNO)):
            sig = {"state": "blocked-human", "detail": f"awaiting user: {kind}", "prompt": prompt, "kind": kind}
            with _no_backend():
                m = compose_message(sig, surface="ag2space")
            self.assertIn("choice card in our DM", m, kind)
            self.assertLess(m.index("choice card"), m.index("where the core is running"), kind)
            self.assertIn("A typed chat reply can't answer it", m)
        sig = {"state": "blocked-human", "detail": "awaiting user: unknown", "prompt": "??", "kind": "unknown"}
        with _no_backend():
            self.assertNotIn("choice card", compose_message(sig, surface="ag2space"))

    def test_the_choice_card_is_named_only_where_a_card_with_buttons_exists(self):
        """No card on discord/slack/telegram or macOS-only; a trust/bypass/limit dialog and an
        unparseable prompt get the button-less card, so the notice names the terminal instead."""
        base = {"state": "blocked-human", "detail": "awaiting user: selection", "kind": "selection"}
        for surface in ("discord", "slack", "telegram", ""):
            with _no_backend():
                m = compose_message({**base, "prompt": self._PICKER}, surface=surface)
            self.assertNotIn("choice card", m, surface or "macos-only")
            self.assertIn("answer it where the core is running", m, surface or "macos-only")
        never = ("Do you trust the files in this folder?\n❯ 1. Yes, proceed\n  2. No, exit",
                 "Bypass Permissions mode\n❯ 1. Yes, I accept\n  2. No",
                 "You've hit your weekly limit\n❯ 1. Switch to Opus and continue\n  2. Stop")
        for prompt in never:
            with _no_backend():
                m = compose_message({**base, "prompt": prompt}, surface="ag2space")
            self.assertNotIn("choice card", m, prompt.splitlines()[0])
            self.assertIn("answer it where the core is running", m)
        with _no_backend():
            m = compose_message({**base, "prompt": "pick one"}, surface="ag2space")
        self.assertNotIn("choice card", m, "no numbered options: no buttons")
        # A permission gate keyed on a Yes/No pair is a card on a configured homeserver bridge too.
        with _no_backend():
            m = compose_message({**base, "kind": "permission", "prompt": self._YESNO}, surface="dev-ag2space")
        self.assertIn("choice card", m)

    def test_a_card_lookup_failure_falls_back_to_the_terminal_wording(self):
        sig = {"state": "blocked-human", "detail": "awaiting user: selection", "prompt": self._PICKER,
               "kind": "selection"}
        with _no_backend(), patch.dict(sys.modules, {"hitl": None, "hitl.tui_gate": None}):
            m = compose_message(sig, surface="ag2space")
        self.assertNotIn("choice card", m)
        self.assertIn("answer it where the core is running", m)
        # Run as a script, src/ is not on sys.path yet: the lookup puts it there itself.
        src = os.path.dirname(os.path.abspath(_SRC))
        with patch.object(sys, "path", [p for p in sys.path if os.path.abspath(p) != src]):
            self.assertTrue(_mod._card_projected(sig, "ag2space"))

    def test_run_cycle_hands_the_delivery_surface_to_the_composer(self):
        sig = {"state": "blocked-human", "detail": "awaiting user: selection", "prompt": self._PICKER,
               "kind": "selection"}
        with _no_backend():
            on_space = run_cycle(sig, "", macos=False, source="ag2space", channel="!r:x", dry_run=True)
            on_discord = run_cycle(sig, "", macos=False, source="discord", channel="1", dry_run=True)
        self.assertIn("choice card", on_space)
        self.assertNotIn("choice card", on_discord)

    def test_non_login_blocker_names_the_cli_terminal(self):
        """A `blocked-human` prompt waits on the core's stdin. Neither a chat reply
        nor the app can answer it, so the remedy must name the terminal."""
        sig = {"state": "blocked-human", "detail": "awaiting user: selection",
               "prompt": "pick one", "kind": "selection"}
        with _no_backend():
            m = compose_message(sig)
        # FALLBACK shape: with no derivable backend (embedded core, unreadable .alive)
        # the remedy must still not invent a transport name.
        self.assertIn("where the core is running", m)
        self.assertNotIn("tmux", m)
        self.assertNotIn("sutando-core", m)
        self.assertNotIn("reply here", m)
        self.assertNotIn("open the app to resolve", m)
        self.assertNotIn("GUI /login", m)

    def test_blocked_human_names_the_DERIVED_terminal(self):
        """When the running core recorded its socket, name it — a generic 'where the
        core is running' does not tell someone who does not already know."""
        sig = {"state": "blocked-human", "detail": "awaiting user: selection",
               "prompt": "pick one", "kind": "selection"}
        with _backend({"socket": "/tmp/probe-tmux.sock"}):
            m = compose_message(sig)
        self.assertIn("/tmp/probe-tmux.sock", m)
        self.assertIn("terminal", m)
        self.assertNotIn("where the core is running", m,
                         "the derived form must REPLACE the vague one, not append to it")

    def test_derivation_failure_is_fail_open_not_fatal(self):
        """A helper that raises must degrade to the generic remedy, never crash the
        escalation — the message is the only channel the owner has here."""
        sig = {"state": "blocked-human", "detail": "awaiting user: selection",
               "prompt": "pick one", "kind": "selection"}
        with _backend_raises():
            m = compose_message(sig)
        self.assertIn("where the core is running", m)

    def test_truncates_long_prompt(self):
        """The prompt echo is bounded, the remedy is not, and the bound must hold
        for BOTH remedy branches."""
        for state, kind in (("blocked-human", "unknown"), ("logged-out", "login")):
            big = {"state": state, "detail": "awaiting user: unknown",
                   "prompt": "x" * 500, "kind": kind}
            # BOTH remedy shapes: the derived form is longer, so the bound must hold
            # for it too — that is the branch a host-name-sensitive bound would miss.
            for ctx, label in ((_no_backend(), "generic"),
                               (_backend({"socket": "/tmp/probe-tmux.sock"}), "derived")):
                with ctx:
                    m = compose_message(big)
                self.assertNotIn("x" * 130, m, f"{state}/{label}: prompt echo not truncated")
                self.assertLess(len(m), 400, f"{state}/{label}: message too long")

    def test_kind_appended_when_not_in_detail(self):
        sig = {"state": "blocked-human", "detail": "the core is waiting",
               "prompt": "pick one", "kind": "selection"}
        self.assertIn("(selection)", compose_message(sig))


class TestRunCycleAndCli(unittest.TestCase):
    def test_dry_run_cycle_escalates_login(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "relay.state")
            msg = run_cycle(_LOGIN, sf, macos=False, dry_run=True)
            self.assertIsNotNone(msg)
            # dry-run must NOT persist state (so a real run still fires).
            self.assertFalse(os.path.exists(sf))

    def test_real_cycle_persists_and_debounces(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "state", "relay.state")
            with patch.object(_mod, "_macos_notify", lambda m: True):
                first = run_cycle(_LOGIN, sf, macos=True)  # macOS delivered → debounce
                self.assertIsNotNone(first)
                self.assertTrue(os.path.exists(sf))
                second = run_cycle(_LOGIN, sf, macos=True)  # same prompt → suppressed
                self.assertIsNone(second)

    def test_relative_state_file_still_debounces(self):
        # Regression: a cwd-relative --state-file (e.g. "relay.state") has an empty
        # dirname. Previously os.makedirs("") raised FileNotFoundError, swallowed by
        # the best-effort except → state never persisted → the relay re-escalated
        # every cycle. The dir-create must be skipped when there is no dirname.
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                with patch.object(_mod, "_macos_notify", lambda m: True):
                    first = run_cycle(_LOGIN, "relay.state", macos=True)
                    self.assertIsNotNone(first)
                    self.assertTrue(os.path.exists("relay.state"))  # persisted, not swallowed
                    second = run_cycle(_LOGIN, "relay.state", macos=True)  # same prompt → suppressed
                    self.assertIsNone(second)
            finally:
                os.chdir(cwd)

    def test_cli_dry_run_on_signal_file(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump(_LOGIN, f)
            rc = main(["--signal", sig, "--no-macos", "--dry-run"])
            self.assertEqual(rc, 0)

    def test_cli_missing_signal_degrades_quietly(self):
        rc = main(["--signal", "/nonexistent/core-supervisor.json", "--no-macos"])
        self.assertEqual(rc, 0)

    def test_cli_non_escalating_signal(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump(_IDLE, f)
            self.assertEqual(main(["--signal", sig, "--no-macos"]), 0)

    def test_cli_non_dict_signal_escalates_as_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump([1, 2, 3], f)  # valid JSON, wrong shape
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--signal", sig, "--no-macos", "--dry-run"]), 0)
            self.assertIn("status file is unreadable", out.getvalue())

    def test_cli_corrupt_signal_escalates_once_not_silently(self):
        # 2026-09-11: a short write left core-supervisor.json corrupt from boot; the
        # relay used to treat that exactly like "no signal yet" and escalate nothing.
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            state = os.path.join(td, "relay.state")
            with open(sig, "w") as f:
                f.write('{"state": "blocked-human", "prompt": "Log')  # truncated
            sent = []
            with patch.object(_mod, "_macos_notify", lambda m: sent.append(m) or True):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--signal", sig, "--state-file", state]), 0)
                    self.assertEqual(main(["--signal", sig, "--state-file", state]), 0)
            self.assertEqual(len(sent), 1, "debounced like any blocker")
            self.assertIn("Agent needs you", sent[0])
            self.assertIn("restarting the engine rewrites the file", sent[0])

    def test_cli_signal_that_cannot_be_opened_escalates(self):
        # present but unopenable (a directory here; a permissions fault in the wild)
        # is not "no signal yet" either — only a missing file is
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--signal", td, "--no-macos", "--dry-run"]), 0)
            self.assertIn("status file is unreadable", out.getvalue())

    def test_unreadable_signal_decision_and_message(self):
        esc, h = should_escalate(dict(_mod.UNREADABLE_SIGNAL), None)
        self.assertTrue(esc)
        self.assertFalse(should_escalate(dict(_mod.UNREADABLE_SIGNAL), h)[0])

    def test_cycle_without_state_file_still_emits(self):
        # No --state-file → no debounce persistence, but the escalation still fires.
        msg = run_cycle(_LOGIN, "", macos=False)
        self.assertIsNotNone(msg)

    def test_run_cycle_dispatches_to_both_surfaces(self):
        # Verify the dispatch call-sites (macOS + channel) fire without invoking the
        # real external I/O — the adapters themselves are best-effort side effects.
        calls = []
        orig_m, orig_c = _mod._macos_notify, _mod._channel_notify
        _mod._macos_notify = lambda m: calls.append(("macos", m))
        _mod._channel_notify = lambda m, s, c: calls.append(("chan", s, c))
        try:
            with tempfile.TemporaryDirectory() as td:
                run_cycle(_LOGIN, os.path.join(td, "s.state"),
                          macos=True, source="discord", channel="123")
        finally:
            _mod._macos_notify, _mod._channel_notify = orig_m, orig_c
        kinds = [c[0] for c in calls]
        self.assertIn("macos", kinds)
        self.assertIn("chan", kinds)

    def test_failed_channel_send_does_not_debounce(self):
        # #2101 review (High): when a channel is selected but its send FAILS, the
        # debounce hash must NOT persist — the blocker re-escalates next cycle
        # instead of being silently marked as already-notified.
        orig_m, orig_c = _mod._macos_notify, _mod._channel_notify
        _mod._macos_notify = lambda m: None
        _mod._channel_notify = lambda m, s, c: False   # selected channel send fails
        try:
            with tempfile.TemporaryDirectory() as td:
                sf = os.path.join(td, "s.state")
                first = run_cycle(_LOGIN, sf, macos=True, source="ag2space",
                                  channel="!room:ag2.space")
                self.assertIsNotNone(first)
                self.assertFalse(os.path.exists(sf), "hash must not persist on failed send")
                # Same blocker, next cycle → re-escalates (not suppressed).
                second = run_cycle(_LOGIN, sf, macos=True, source="ag2space",
                                   channel="!room:ag2.space")
                self.assertIsNotNone(second)
        finally:
            _mod._macos_notify, _mod._channel_notify = orig_m, orig_c

    def test_successful_channel_send_debounces(self):
        # Complement: a channel send that LANDS persists the hash → suppressed next cycle.
        orig_m, orig_c = _mod._macos_notify, _mod._channel_notify
        _mod._macos_notify = lambda m: None
        _mod._channel_notify = lambda m, s, c: True    # selected channel send lands
        try:
            with tempfile.TemporaryDirectory() as td:
                sf = os.path.join(td, "s.state")
                first = run_cycle(_LOGIN, sf, macos=False, source="ag2space",
                                  channel="!room:ag2.space")
                self.assertIsNotNone(first)
                self.assertTrue(os.path.exists(sf), "hash must persist on successful send")
                second = run_cycle(_LOGIN, sf, macos=False, source="ag2space",
                                   channel="!room:ag2.space")
                self.assertIsNone(second, "same blocker suppressed after a landed send")
        finally:
            _mod._macos_notify, _mod._channel_notify = orig_m, orig_c

    def test_cli_active_from_routes_to_owner_channel(self):
        # Covers main()'s --active-from branch: with no explicit --notify-*, the
        # owner's active channel is resolved from last-owner-activity.json and the
        # escalation routes there.
        calls = []
        orig_c = _mod._channel_notify
        _mod._channel_notify = lambda m, s, c: calls.append((s, c))
        try:
            with tempfile.TemporaryDirectory() as td:
                sig = os.path.join(td, "core-supervisor.json")
                with open(sig, "w") as f:
                    json.dump(_LOGIN, f)
                act = os.path.join(td, "last-owner-activity.json")
                with open(act, "w") as f:
                    json.dump({"channel": "discord", "channel_id": "42"}, f)
                rc = main(["--signal", sig, "--active-from", act, "--no-macos",
                           "--state-file", os.path.join(td, "s.state")])
                self.assertEqual(rc, 0)
        finally:
            _mod._channel_notify = orig_c
        self.assertEqual(calls, [("discord", "42")])


class TestResolveActiveTarget(unittest.TestCase):
    """--active-from: auto-route to the owner's most-recently-active channel,
    degrading to macOS-only ("", "") whenever we can't route confidently."""

    def _write(self, td, obj):
        p = os.path.join(td, "last-owner-activity.json")
        with open(p, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    def test_deliverable_surface_with_channel_id_routes(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, {"channel": "discord", "channel_id": "12345", "summary": "hi"})
            self.assertEqual(resolve_active_target(p), ("discord", "12345"))

    def test_ag2space_room_routes(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, {"channel": "ag2space", "channel_id": "!room:ag2.space"})
            self.assertEqual(resolve_active_target(p), ("ag2space", "!room:ag2.space"))

    def test_deliverable_but_no_channel_id_is_macos_only(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, {"channel": "slack", "summary": "no id recorded"})
            self.assertEqual(resolve_active_target(p), ("", ""))

    def test_non_deliverable_surface_is_macos_only(self):
        # "voice"/"github-commits" are activity signals, not deliverable channels.
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, {"channel": "voice", "channel_id": "x"})
            self.assertEqual(resolve_active_target(p), ("", ""))

    def test_configured_channel_dir_makes_new_surface_deliverable(self):
        # New-homeserver rule: a source outside the static set is deliverable
        # iff $CLAUDE_CONFIG_DIR/channels/<source>/ holds a *.env (notify.py's
        # own resolution rule) — adding a homeserver is config-only.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cfg:
            os.makedirs(os.path.join(cfg, "channels", "dev-ag2space"))
            with open(os.path.join(cfg, "channels", "dev-ag2space", ".env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            p = self._write(td, {"channel": "dev-ag2space", "channel_id": "!r:dev.ag2.space"})
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = cfg
            try:
                self.assertEqual(resolve_active_target(p), ("dev-ag2space", "!r:dev.ag2.space"))
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_relay_client_env_alone_is_not_deliverable(self):
        # notify.py reads exactly channels/<source>/.env; a lane holding only
        # relay-client.env would fail the actual send — must NOT be selected
        # (the #2701 review P1 failure mode).
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cfg:
            os.makedirs(os.path.join(cfg, "channels", "dev-ag2space"))
            with open(os.path.join(cfg, "channels", "dev-ag2space", "relay-client.env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            p = self._write(td, {"channel": "dev-ag2space", "channel_id": "!r:dev.ag2.space"})
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = cfg
            try:
                self.assertEqual(resolve_active_target(p), ("", ""))
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_unconfigured_new_surface_stays_macos_only(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cfg:
            p = self._write(td, {"channel": "dev-ag2space", "channel_id": "!r:dev.ag2.space"})
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = cfg
            try:
                self.assertEqual(resolve_active_target(p), ("", ""))
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_domain_named_lane_with_env_is_deliverable(self):
        # Dots between alphanumerics are legal (domain-named lanes, in lockstep
        # with notify.py's rule).
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cfg:
            os.makedirs(os.path.join(cfg, "channels", "dev.ag2.space"))
            with open(os.path.join(cfg, "channels", "dev.ag2.space", ".env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            p = self._write(td, {"channel": "dev.ag2.space", "channel_id": "!r:dev.ag2.space"})
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = cfg
            try:
                self.assertEqual(resolve_active_target(p), ("dev.ag2.space", "!r:dev.ag2.space"))
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_symlinked_out_channel_dir_is_not_deliverable(self):
        # notify.py's sender REFUSES a channel entry that resolves outside
        # channels/ (realpath containment); the probe must agree or we recreate
        # selected-then-send-fails via the symlink mismatch (#2701 review P1).
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cfg, \
             tempfile.TemporaryDirectory() as outside:
            os.makedirs(os.path.join(cfg, "channels"), exist_ok=True)
            with open(os.path.join(outside, ".env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            os.symlink(outside, os.path.join(cfg, "channels", "sneaky"))
            p = self._write(td, {"channel": "sneaky", "channel_id": "!r:s"})
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = cfg
            try:
                self.assertEqual(resolve_active_target(p), ("", ""))
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def _with_env(self, **values):
        """Set/unset env vars for one test; None means unset."""
        saved = {k: os.environ.get(k) for k in values}
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)

    def _relocated_layout(self, source="ag2space"):
        """$CLAUDE_CONFIG_DIR/channels/<source>/.env -> $APP/channels/<source>/.env,
        the AG2 Space desktop-app layout (#3150/#3201). Returns (cfg, app)."""
        cfg = tempfile.mkdtemp()
        app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, app, ignore_errors=True)
        real_dir = os.path.join(app, "channels", source)
        os.makedirs(real_dir)
        with open(os.path.join(real_dir, ".env"), "w") as f:
            f.write("REMOTE_TASK_TOKEN=x\n")
        link_dir = os.path.join(cfg, "channels", source)
        os.makedirs(link_dir)
        os.symlink(os.path.join(real_dir, ".env"), os.path.join(link_dir, ".env"))
        return cfg, app

    def test_app_support_relocated_env_is_deliverable(self):
        # notify.py accepts $SUTANDO_APP_SUPPORT/channels/<source>/.env as a
        # second root (#3150/#3201); the probe must agree or the lane never routes.
        with tempfile.TemporaryDirectory() as td:
            cfg, app = self._relocated_layout("dev-ag2space")
            self._with_env(CLAUDE_CONFIG_DIR=cfg, SUTANDO_APP_SUPPORT=app)
            p = self._write(td, {"channel": "dev-ag2space", "channel_id": "!r:d"})
            self.assertEqual(resolve_active_target(p), ("dev-ag2space", "!r:d"))

    def test_same_shape_outside_app_support_is_not_deliverable(self):
        # Containment, not a leaf-shape match: same layout with the var unset or
        # pointed at another root is a symlink the sender refuses — so must the probe.
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            cfg, _app = self._relocated_layout("dev-ag2space")
            p = self._write(td, {"channel": "dev-ag2space", "channel_id": "!r:d"})
            self._with_env(CLAUDE_CONFIG_DIR=cfg, SUTANDO_APP_SUPPORT=None)
            self.assertEqual(resolve_active_target(p), ("", ""))
            self._with_env(SUTANDO_APP_SUPPORT=other)
            self.assertEqual(resolve_active_target(p), ("", ""))

    def test_claude_home_tier_is_honored(self):
        # notify.py resolves CLAUDE_CONFIG_DIR -> CLAUDE_HOME -> ~/.claude; the
        # probe must walk the SAME tiers (a CLAUDE_HOME-only env previously
        # made probe and sender disagree — #2701 review P1).
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "channels", "dev-ag2space"))
            with open(os.path.join(home, "channels", "dev-ag2space", ".env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            p = self._write(td, {"channel": "dev-ag2space", "channel_id": "!r:d"})
            old_cfg = os.environ.pop("CLAUDE_CONFIG_DIR", None)
            old_home = os.environ.get("CLAUDE_HOME")
            os.environ["CLAUDE_HOME"] = home
            try:
                self.assertEqual(resolve_active_target(p), ("dev-ag2space", "!r:d"))
            finally:
                if old_cfg is not None:
                    os.environ["CLAUDE_CONFIG_DIR"] = old_cfg
                if old_home is None:
                    del os.environ["CLAUDE_HOME"]
                else:
                    os.environ["CLAUDE_HOME"] = old_home

    def test_traversal_shaped_source_is_never_probed(self):
        # The slug guard must reject path-shaped sources outright.
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, {"channel": "../discord", "channel_id": "x"})
            self.assertEqual(resolve_active_target(p), ("", ""))

    def test_missing_file_is_macos_only(self):
        self.assertEqual(resolve_active_target("/no/such/activity.json"), ("", ""))

    def test_malformed_and_nondict_are_macos_only(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(resolve_active_target(self._write(td, "{bad json")), ("", ""))
            self.assertEqual(resolve_active_target(self._write(td, [1, 2, 3])), ("", ""))



class TestChannelEnvContainmentDelegation(unittest.TestCase):
    """The probe (_is_deliverable) must call the shared
    src/channel_env_containment.py function, not carry its own copy of the
    containment rule — the exact duplication CLAUDE.md's architecture rules
    call out as the defect."""

    def _reload(self):
        """A fresh module instance, separate from the shared `_mod` every
        other test class depends on, so patching the shared function's
        binding here can't affect them."""
        spec = importlib.util.spec_from_file_location("core_supervisor_relay_fresh", _SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_binds_the_shared_function_by_identity(self):
        self.assertIs(_mod._channel_env_is_contained,
                      channel_env_containment.channel_env_is_contained)

    def test_containment_delegates_to_shared_module_not_a_copy(self):
        """Stub the shared function to always refuse, reload the probe, and
        confirm even an otherwise-deliverable app-support relocation is now
        refused. If _is_deliverable carried its own copy of the rule, this
        module-level stub would have no effect."""
        with tempfile.TemporaryDirectory() as td:
            cfg = tempfile.mkdtemp()
            app = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
            self.addCleanup(shutil.rmtree, app, ignore_errors=True)
            real_dir = os.path.join(app, "channels", "dev-ag2space")
            os.makedirs(real_dir)
            with open(os.path.join(real_dir, ".env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            link_dir = os.path.join(cfg, "channels", "dev-ag2space")
            os.makedirs(link_dir)
            os.symlink(os.path.join(real_dir, ".env"), os.path.join(link_dir, ".env"))

            p = os.path.join(td, "last-owner-activity.json")
            with open(p, "w") as f:
                json.dump({"channel": "dev-ag2space", "channel_id": "!r:d"}, f)

            saved = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "SUTANDO_APP_SUPPORT")}
            os.environ["CLAUDE_CONFIG_DIR"] = cfg
            os.environ["SUTANDO_APP_SUPPORT"] = app
            try:
                with patch.object(channel_env_containment, "channel_env_is_contained",
                                  return_value=False):
                    fresh = self._reload()
                    self.assertEqual(fresh.resolve_active_target(p), ("", ""))
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_load_channel_env_containment_fails_closed_when_import_fails(self):
        """The fallback lambda itself, not just the already-bound result:
        force the shared module's import to fail and confirm the returned
        callable refuses even an otherwise-valid-looking containment case —
        never silently widen the guard just because the import failed."""
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if name == "channel_env_containment":
                raise ImportError("simulated: src/ not importable")
            return real_import(name, *a, **kw)

        fresh = self._reload()
        with patch.object(builtins, "__import__", boom):
            fallback = fresh._load_channel_env_containment()

        with tempfile.TemporaryDirectory() as td:
            channels_dir = os.path.join(td, "channels")
            env_dir = os.path.join(channels_dir, "dev-ag2space")
            os.makedirs(env_dir)
            env_path = os.path.join(env_dir, ".env")
            with open(env_path, "w") as f:
                f.write("REMOTE_TASK_TOKEN=x\n")
            self.assertFalse(fallback(env_path, channels_dir, "dev-ag2space"))


class TestBackendRecordContract(unittest.TestCase):
    """`_derive_backend` reads the file `core_heartbeat` wrote. The label and the
    freshness rule must match the writer's, or it silently resolves to nothing."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self._td.name, "ws")
        os.makedirs(os.path.join(self.ws, "state", "cores"))
        self._saved = os.environ.get("SUTANDO_HOST_LABEL")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUTANDO_HOST_LABEL", None)
        else:
            os.environ["SUTANDO_HOST_LABEL"] = self._saved
        self._td.cleanup()

    @contextlib.contextmanager
    def _workspace(self):
        """Inject the workspace the helper imports, so the real resolver is not
        consulted and the test cannot silently read the live workspace."""
        import sys
        import types
        stub = types.ModuleType("workspace_default")
        # Variadic: callers pass migrate=/repo=, and a zero-arg stub would raise
        # TypeError into the helper's broad except, asserting fail-open by accident.
        stub.resolve_workspace = lambda *a, **kw: self.ws
        prev = sys.modules.get("workspace_default")
        sys.modules["workspace_default"] = stub
        try:
            yield
        finally:
            if prev is None:
                sys.modules.pop("workspace_default", None)
            else:
                sys.modules["workspace_default"] = prev

    def _write_alive(self, label, socket_path="/tmp/sutando-tmux.sock", age_sec=0,
                     session=None):
        f = os.path.join(self.ws, "state", "cores", f"{label}.alive")
        rec = {"host": label, "socket": socket_path}
        if session is not None:
            rec["session"] = session
        with open(f, "w") as fh:
            json.dump(rec, fh)
        if age_sec:
            old = os.path.getmtime(f) - age_sec
            os.utime(f, (old, old))
        return f

    def test_remedy_names_the_session_not_a_bare_attach(self):
        """A bare `attach` on a socket shared with the watcher can land the owner
        in the wrong session, so the remedy must carry -t."""
        os.environ["SUTANDO_HOST_LABEL"] = "SessHost"
        self._write_alive("SessHost")
        with self._workspace():
            msg = compose_message(_HUNG)
        self.assertIn("attach -t sutando-core", msg)
        self.assertNotRegex(msg, r"attach(?! -t)")

    def test_remedy_uses_the_session_the_heartbeat_recorded(self):
        """A sibling/custom session is exactly the case a default would send to
        the wrong prompt, so the recorded value must win over the default."""
        os.environ["SUTANDO_HOST_LABEL"] = "SessHost"
        self._write_alive("SessHost", session="sutando-core-watcher")
        with self._workspace():
            msg = compose_message(_HUNG)
        self.assertIn("attach -t sutando-core-watcher", msg)
        self.assertNotIn("attach -t sutando-core`", msg)

    def test_env_session_overrides_the_default_when_unrecorded(self):
        """Same env/default contract the launchers use, so a custom-session host
        gets an actionable command rather than the shipped default."""
        os.environ["SUTANDO_HOST_LABEL"] = "SessHost"
        os.environ["SUTANDO_TMUX_SESSION"] = "my-core"
        try:
            self._write_alive("SessHost")
            with self._workspace():
                msg = compose_message(_HUNG)
        finally:
            os.environ.pop("SUTANDO_TMUX_SESSION", None)
        self.assertIn("attach -t my-core", msg)

    def test_reads_the_label_the_heartbeat_wrote_under(self):
        """The writer uses util_paths._host_label(); a reader on platform.node()
        misses the file whenever DHCP drifts the hostname away from the label."""
        os.environ["SUTANDO_HOST_LABEL"] = "Label-Not-The-Hostname"
        self._write_alive("Label-Not-The-Hostname")
        with self._workspace():
            be = _mod._derive_backend()
        self.assertIsNotNone(
            be, "read under the process hostname instead of the host-label contract")
        self.assertEqual(be["socket"], "/tmp/sutando-tmux.sock")

    def test_label_falls_back_to_platform_node_when_util_paths_is_unimportable(self):
        """Drives the fallback rather than asserting it exists: util_paths is
        replaced by a module with no _host_label, so the import itself raises."""
        import platform as _pl
        import sys
        import types
        os.environ["SUTANDO_HOST_LABEL"] = "Label-Not-The-Hostname"
        prev = sys.modules.get("util_paths")
        sys.modules["util_paths"] = types.ModuleType("util_paths")
        try:
            got = _mod._core_host_label()
        finally:
            if prev is None:
                sys.modules.pop("util_paths", None)
            else:
                sys.modules["util_paths"] = prev
        self.assertEqual(got, _pl.node().split(".")[0])

    def test_control_the_two_label_branches_return_different_values(self):
        """Without this the fallback test passes for the wrong reason: if the
        working path also returned platform.node(), both branches look alike."""
        import platform as _pl
        os.environ["SUTANDO_HOST_LABEL"] = "Label-Not-The-Hostname"
        self.assertEqual(_mod._core_host_label(), "Label-Not-The-Hostname")
        self.assertNotEqual("Label-Not-The-Hostname", _pl.node().split(".")[0])

    def test_a_stale_record_is_not_a_target(self):
        """Written under BOTH labels on purpose: with only one, a reader that used
        the wrong label would return None and pass this for the wrong reason."""
        import platform as _pl
        os.environ["SUTANDO_HOST_LABEL"] = "StaleHost"
        self._write_alive("StaleHost", age_sec=600)
        self._write_alive(_pl.node().split(".")[0], age_sec=600)
        with self._workspace():
            self.assertIsNone(_mod._derive_backend(),
                              "a record past the staleness bound is not a live target")

    def test_a_fresh_record_still_resolves(self):
        os.environ["SUTANDO_HOST_LABEL"] = "FreshHost"
        self._write_alive("FreshHost", socket_path="/tmp/other.sock")
        with self._workspace():
            be = _mod._derive_backend()
        self.assertEqual(be["socket"], "/tmp/other.sock")

    def test_absent_record_is_none_not_a_crash(self):
        os.environ["SUTANDO_HOST_LABEL"] = "NoSuchHost"
        with self._workspace():
            self.assertIsNone(_mod._derive_backend())


class TestTargetIsRunnable(unittest.TestCase):
    def test_the_message_gives_a_command_not_a_path(self):
        """A bare socket path is not something the owner can act on."""
        with _backend({"socket": "/tmp/sutando-tmux.sock"}):
            msg = compose_message(_HUNG)
        self.assertIn("tmux -S /tmp/sutando-tmux.sock attach", msg)

    def test_no_backend_still_degrades_to_generic_phrasing(self):
        with _no_backend():
            msg = compose_message(_HUNG)
        self.assertIn("where the core is running", msg)
        self.assertNotIn("tmux -S", msg)


class TestProgressRouteDelegation(unittest.TestCase):
    """Routability is src/progress_route.py's verdict, the one notify.py applies;
    a private copy in the relay would drift from it."""

    def _reload(self):
        spec = importlib.util.spec_from_file_location("core_supervisor_relay_route", _SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _activity(self, td, obj):
        p = os.path.join(td, "last-owner-activity.json")
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def test_binds_the_shared_route_by_identity(self):
        self.assertIs(_mod._delivery_route, progress_route.delivery_route)

    def test_verdict_follows_the_shared_function(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._activity(td, {"channel": "discord", "channel_id": "42"})
            with patch.object(progress_route, "delivery_route", return_value=None):
                self.assertEqual(self._reload().resolve_active_target(p), ("", ""))
            self.assertEqual(self._reload().resolve_active_target(p), ("discord", "42"))

    def test_unimportable_route_fails_closed(self):
        with patch.dict(sys.modules, {"progress_route": None}):
            fn = _mod._load_progress_route()
        self.assertIsNone(fn("discord", "42"))

    def test_explicit_no_route_target_degrades_to_macos_only(self):
        calls = []
        orig_c = _mod._channel_notify
        _mod._channel_notify = lambda m, s, c: calls.append((s, c)) or True
        try:
            with tempfile.TemporaryDirectory() as td:
                sig = os.path.join(td, "core-supervisor.json")
                with open(sig, "w") as f:
                    json.dump(_LOGIN, f)
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    rc = main(["--signal", sig, "--notify-source", "voice",
                               "--notify-channel", "local-voice", "--no-macos",
                               "--state-file", os.path.join(td, "s.state")])
        finally:
            _mod._channel_notify = orig_c
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("no delivery path; macOS disabled, nothing to escalate to", err.getvalue())


class TestNoRouteIsNeverDelivered(unittest.TestCase):
    """A configured custom source whose channel is no room id: notify.py sends
    nothing, so the relay must neither select it nor debounce on it."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        os.makedirs(os.path.join(self.cfg, "channels", "dev-ag2space"))
        with open(os.path.join(self.cfg, "channels", "dev-ag2space", ".env"), "w") as f:
            f.write("REMOTE_TASK_URL=http://127.0.0.1:9\nREMOTE_TASK_TOKEN=x\n")
        scripts = os.path.join(self.cfg, "skills", "task-progress", "scripts")
        os.makedirs(scripts)
        os.symlink(os.path.realpath(os.path.join(_HERE, "..", "skills", "task-progress",
                                                 "scripts", "notify.py")),
                   os.path.join(scripts, "notify.py"))
        saved = {k: os.environ.get(k) for k in
                 ("CLAUDE_CONFIG_DIR", "REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")}
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        for k in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
            os.environ.pop(k, None)

        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)

    def test_relay_does_not_select_a_no_route_target(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "last-owner-activity.json")
            with open(p, "w") as f:
                json.dump({"channel": "dev-ag2space", "channel_id": "C0123"}, f)
            self.assertEqual(resolve_active_target(p), ("", ""))

    def test_a_no_route_send_does_not_debounce(self):
        orig_m = _mod._macos_notify
        _mod._macos_notify = lambda m: None
        try:
            with tempfile.TemporaryDirectory() as td:
                sf = os.path.join(td, "s.state")
                self.assertFalse(_mod._channel_notify("x", "dev-ag2space", "C0123"))
                run_cycle(_LOGIN, sf, macos=False, source="dev-ag2space", channel="C0123")
                self.assertFalse(os.path.exists(sf), "a no-route send must not debounce")
        finally:
            _mod._macos_notify = orig_m


class TestDebounceNeedsAConfirmedDelivery(unittest.TestCase):
    """The hash persists only after a delivery landed somewhere; a cycle that
    reached no one must retry on the next tick."""

    def _cycle(self, td, *, no_macos, macos_ok=True, channel_ok=None):
        sig = os.path.join(td, "core-supervisor.json")
        with open(sig, "w") as f:
            json.dump(_LOGIN, f)
        state = os.path.join(td, "s.state")
        argv = ["--signal", sig, "--state-file", state,
                "--notify-source", "voice", "--notify-channel", "local-voice"]
        if no_macos:
            argv.append("--no-macos")
        calls = []
        with patch.object(_mod, "_macos_notify", lambda m: calls.append("macos") or macos_ok), \
                patch.object(_mod, "_channel_notify", lambda m, s, c: calls.append("chan") or channel_ok), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = main(argv)
        return rc, calls, os.path.exists(state), out.getvalue()

    def test_no_route_with_no_macos_persists_nothing_and_retries(self):
        with tempfile.TemporaryDirectory() as td:
            rc, calls, persisted, out = self._cycle(td, no_macos=True)
            self.assertEqual((rc, calls, persisted), (0, [], False))
            self.assertIn("not delivered: no route and macOS disabled (will retry)", out)
            self.assertNotIn("escalated:", out)
            rc, calls, persisted, out = self._cycle(td, no_macos=True)
            self.assertNotIn("no escalation", out, "the next cycle must retry, not be suppressed")
            self.assertFalse(persisted)

    def test_no_route_with_a_failed_macos_notification_persists_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            _, calls, persisted, out = self._cycle(td, no_macos=False, macos_ok=False)
            self.assertEqual((calls, persisted), (["macos"], False))
            self.assertIn("not delivered: no route and macOS notification failed (will retry)", out)

    def test_no_route_with_macos_delivered_persists(self):
        with tempfile.TemporaryDirectory() as td:
            _, calls, persisted, out = self._cycle(td, no_macos=False, macos_ok=True)
            self.assertEqual((calls, persisted), (["macos"], True))
            self.assertIn("escalated:", out)
            _, _, _, out = self._cycle(td, no_macos=False, macos_ok=True)
            self.assertIn("no escalation", out, "a delivered alert is debounced")

    def test_a_failed_channel_is_not_rescued_by_macos(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "s.state")
            with patch.object(_mod, "_macos_notify", lambda m: True), \
                    patch.object(_mod, "_channel_notify", lambda m, s, c: False):
                run_cycle(_LOGIN, sf, macos=True, source="discord", channel="42")
            self.assertFalse(os.path.exists(sf))


class TestMacosNotifyAdapter(unittest.TestCase):
    """The real _macos_notify against a fake `osascript` on PATH: only an exit 0
    is a delivery, and only a delivery may persist the debounce."""

    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.calls = os.path.join(self.bin, "calls")

    def _fake(self, body, mode=0o755):
        p = os.path.join(self.bin, "osascript")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\necho called >> {self.calls}\n{body}\n")
        os.chmod(p, mode)

    def _notify(self):
        with patch.dict(os.environ, {"PATH": self.bin}):
            return _mod._macos_notify("hello")

    def _main_cycle(self, td):
        sig = os.path.join(td, "core-supervisor.json")
        with open(sig, "w") as f:
            json.dump(_LOGIN, f)
        state = os.path.join(td, "s.state")
        with patch.dict(os.environ, {"PATH": self.bin}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--signal", sig, "--state-file", state,
                  "--notify-source", "voice", "--notify-channel", "local-voice"])
        return os.path.exists(state), out.getvalue()

    def _assert_undelivered_and_retries(self):
        with tempfile.TemporaryDirectory() as td:
            persisted, out = self._main_cycle(td)
            self.assertFalse(persisted)
            self.assertIn("not delivered: no route and macOS notification failed", out)
            persisted, out = self._main_cycle(td)
            self.assertFalse(persisted)
            self.assertNotIn("no escalation", out, "the next cycle must retry")

    def test_exit_0_is_delivered_and_debounces(self):
        self._fake("exit 0")
        self.assertIs(self._notify(), True)
        self.assertTrue(os.path.exists(self.calls), "the fake osascript ran")
        with tempfile.TemporaryDirectory() as td:
            persisted, out = self._main_cycle(td)
            self.assertTrue(persisted)
            self.assertIn("escalated:", out)
            self.assertIn("no escalation", self._main_cycle(td)[1])

    def test_exit_1_is_not_delivered(self):
        self._fake("exit 1")
        self.assertIs(self._notify(), False)
        self.assertTrue(os.path.exists(self.calls), "the fake osascript ran")
        self._assert_undelivered_and_retries()
        with open(self.calls) as f:
            self.assertEqual(len(f.readlines()), 3, "both cycles retried the notification")

    def test_missing_binary_is_not_delivered(self):
        self.assertIs(self._notify(), False)
        self._assert_undelivered_and_retries()

    def test_oserror_is_not_delivered(self):
        self._fake("exit 0", mode=0o644)  # present but not executable: PermissionError
        self.assertIs(self._notify(), False)
        self._assert_undelivered_and_retries()

    def test_timeout_is_not_delivered(self):
        import subprocess as _sp
        self._fake("exit 0")
        with patch.object(_mod.subprocess, "run",
                          side_effect=_sp.TimeoutExpired("osascript", 8)):
            self.assertIs(self._notify(), False)
            self._assert_undelivered_and_retries()


class TestUndeliveredLogThrottle(unittest.TestCase):
    """A cycle that reaches no one retries every tick but logs once per signal,
    then at most every UNDELIVERED_LOG_INTERVAL_S; the debounce state stays empty."""

    def _cycle(self, td, signal, now):
        sig = os.path.join(td, "core-supervisor.json")
        with open(sig, "w") as f:
            json.dump(signal, f)
        state = os.path.join(td, "s.state")
        with patch.object(_mod.time, "time", return_value=now), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--signal", sig, "--state-file", state, "--no-macos"])
        return out.getvalue().count("not delivered:"), os.path.exists(state)

    def test_logs_once_then_again_after_the_interval(self):
        iv = _mod.UNDELIVERED_LOG_INTERVAL_S
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._cycle(td, _LOGIN, 1000.0), (1, False))
            self.assertEqual(self._cycle(td, _LOGIN, 1030.0), (0, False))
            self.assertEqual(self._cycle(td, _LOGIN, 1000.0 + iv - 1), (0, False))
            self.assertEqual(self._cycle(td, _LOGIN, 1000.0 + iv + 1), (1, False))
            self.assertEqual(self._cycle(td, _LOGIN, 1000.0 + iv + 31), (0, False))

    def test_a_new_signal_logs_at_once(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._cycle(td, _LOGIN, 1000.0), (1, False))
            self.assertEqual(self._cycle(td, _LIMIT, 1030.0), (1, False))

    def test_the_debounce_state_never_holds_the_throttle(self):
        with tempfile.TemporaryDirectory() as td:
            self._cycle(td, _LOGIN, 1000.0)
            self.assertFalse(os.path.exists(os.path.join(td, "s.state")))
            self.assertTrue(os.path.exists(os.path.join(td, "s.state.undelivered")))


if __name__ == "__main__":
    unittest.main()
