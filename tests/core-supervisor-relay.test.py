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
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-supervisor-relay.py")
_spec = importlib.util.spec_from_file_location("core_supervisor_relay", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
should_escalate = _mod.should_escalate
compose_message = _mod.compose_message
run_cycle = _mod.run_cycle
main = _mod.main
resolve_active_target = _mod.resolve_active_target

_LOGIN = {"state": "blocked-human", "detail": "awaiting user: login",
          "prompt": "Login\nSelect login method:\n  1. Claude account", "kind": "login"}
_LOGGED_OUT = {"state": "logged-out", "detail": "core not authenticated (needs /login)",
               "prompt": None, "kind": None}
_IDLE = {"state": "idle-ready", "detail": "ready for a task", "prompt": None, "kind": None}
_RUNNING = {"state": "running", "detail": "actively processing", "prompt": None, "kind": None}
_CRASHED = {"state": "crashed", "detail": "core process/session not found", "prompt": None}
_HUNG = {"state": "hung", "detail": "core alive but stalled", "prompt": "…", "kind": "unknown"}


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


class TestComposeMessage(unittest.TestCase):
    def test_includes_detail_and_prompt_excerpt(self):
        m = compose_message(_LOGIN)
        self.assertIn("awaiting user: login", m)
        self.assertIn("Login", m)  # first prompt line
        self.assertIn("resolve", m)

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

    def test_non_login_blocker_names_the_cli_terminal(self):
        """A `blocked-human` prompt waits on the core's stdin. Neither a chat reply
        nor the app can answer it, so the remedy must name the terminal."""
        sig = {"state": "blocked-human", "detail": "awaiting user: selection",
               "prompt": "pick one", "kind": "selection"}
        m = compose_message(sig)
        self.assertIn("CLI terminal", m)
        self.assertIn("sutando-core", m)
        self.assertNotIn("reply here", m)
        self.assertNotIn("open the app to resolve", m)
        self.assertNotIn("GUI /login", m)

    def test_truncates_long_prompt(self):
        """The prompt ECHO is bounded; the remedy is not. A 500-char prompt must
        not reach the message, and the bound holds for BOTH remedy branches —
        the login branch was previously only tested with a short prompt, so its
        long-prompt case exceeded this bound unnoticed."""
        for state, kind in (("blocked-human", "unknown"), ("logged-out", "login")):
            big = {"state": state, "detail": "awaiting user: unknown",
                   "prompt": "x" * 500, "kind": kind}
            m = compose_message(big)
            self.assertNotIn("x" * 130, m, f"{state}: prompt echo not truncated")
            self.assertLess(len(m), 400, f"{state}: message too long")

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
            first = run_cycle(_LOGIN, sf, macos=False)  # no channel → macOS suppressed, still decides
            self.assertIsNotNone(first)
            self.assertTrue(os.path.exists(sf))
            second = run_cycle(_LOGIN, sf, macos=False)  # same prompt → suppressed
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
                first = run_cycle(_LOGIN, "relay.state", macos=False)
                self.assertIsNotNone(first)
                self.assertTrue(os.path.exists("relay.state"))  # persisted, not swallowed
                second = run_cycle(_LOGIN, "relay.state", macos=False)  # same prompt → suppressed
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

    def test_cli_non_dict_signal_degrades(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump([1, 2, 3], f)  # valid JSON, wrong shape
            self.assertEqual(main(["--signal", sig, "--no-macos"]), 0)

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


if __name__ == "__main__":
    unittest.main()
