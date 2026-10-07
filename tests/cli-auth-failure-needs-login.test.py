#!/usr/bin/env python3
"""A Claude CLI auth failure must read as needs-login in every engine reader.

Five banner forms the CLI prints when its credential is rejected, each placed on a
realistic core pane, must make: cli_wedge's banner grammar name `needs-login`,
runtime-health's `needs_login` and worker_auth_state's `login_expired` fire,
core-input-watch's `compose_state` return `logged-out`, and the relay escalate
one owner message naming /login. Negative controls: an idle pane, and panes whose
composer draft or agent prose mentions "PR 401" / "authentication_error".

Run: python3 tests/cli-auth-failure-needs-login.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC_DIR)


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SRC_DIR, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import cli_wedge  # noqa: E402
import worker_auth_state  # noqa: E402

rh = _load("runtime_health_auth401", "runtime-health.py")
ciw = _load("core_input_watch_auth401", "core-input-watch.py")
relay = _load("core_supervisor_relay_auth401", "core-supervisor-relay.py")

_FOOTER = ("────────────────\n❯ \n────────────────\n"
           "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")

# The forms the CLI renders on a rejected credential, as captured (one per row).
AUTH_FAILURE_LINES = {
    "oauth-401-retry": ("✗ 401 OAuth access token has expired. Re-authenticate to continue."
                        " · Retrying in 8s · attempt 3/10"),
    "oauth-401": "✗ 401 OAuth token has expired. Re-authenticate to continue.",
    "api-error-401": ('API Error: 401 {"type":"error","error":{"type":"authentication_error",'
                      '"message":"OAuth token has expired. Please obtain a new token or refresh'
                      ' your existing token."},"request_id":"req_011CTx3mJ4v9Qe7XqU5aBcDe"}'
                      ' · Please run /login'),
    "invalid-api-key": "Invalid API key · Please run /login",
    "login-expired": "Login expired · Please run /login",
}


def _refused_pane(line):
    """The turn the CLI refused: the prompt, its one ⎿ line, a 0s completed turn, idle."""
    return f"❯ check my inbox\n  ⎿  {line}\n✻ Worked for 0s\n{_FOOTER}"


def _retrying_pane(line):
    """The turn still in flight: the banner under the prompt, the live spinner."""
    return (f"❯ check my inbox\n  ⎿  {line}\n"
            f"✶ Thinking… (14s · ↓ 0 tokens · esc to interrupt)\n{_FOOTER}")


def _panes(line):
    yield "refused", _refused_pane(line)
    if "Retrying" in line:
        yield "retrying", _retrying_pane(line)


NEGATIVE_PANES = {
    "idle": f"● Done — the report is in your DM.\n✻ Worked for 2m 3s\n{_FOOTER}",
    "draft-pr-401": ("● Done.\n✻ Worked for 1m 2s\n────────────────\n"
                     "❯ can you look at PR 401 and the 401 retry handling in\n"
                     "authentication_error paths, then Please run /login docs\n"
                     "────────────────\n"
                     "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"),
    "prose-mentions": ("❯ summarise the auth PR\n"
                       "● PR 401 maps authentication_error to a retry; the CLI then prints"
                       " Invalid API key · Please run /login when the key is bad.\n"
                       "✻ Worked for 41s\n" + _FOOTER),
}


class AuthFailureIsNeedsLogin(unittest.TestCase):
    def test_banner_grammar_names_needs_login(self):
        for form, line in AUTH_FAILURE_LINES.items():
            with self.subTest(form=form):
                names = [n for fam, n, _ in cli_wedge.live_banner_lines(line) if fam == "parked"]
                self.assertIn("needs-login", names)

    def test_runtime_health_needs_login(self):
        for form, line in AUTH_FAILURE_LINES.items():
            for shape, pane in _panes(line):
                with self.subTest(form=form, shape=shape):
                    self.assertTrue(rh.needs_login(pane))

    def test_worker_auth_state_login_expired(self):
        for form, line in AUTH_FAILURE_LINES.items():
            for shape, pane in _panes(line):
                with self.subTest(form=form, shape=shape):
                    self.assertIsNotNone(worker_auth_state.login_expired(pane))

    def test_compose_state_is_logged_out_and_relay_escalates(self):
        for form, line in AUTH_FAILURE_LINES.items():
            for shape, pane in _panes(line):
                for base in ("idle", "working", "unknown"):
                    with self.subTest(form=form, shape=shape, base=base):
                        state, detail, prompt, kind = ciw.compose_state(pane, base, True)
                        self.assertEqual(state, "logged-out")
                        signal = {"state": state, "detail": detail, "prompt": prompt, "kind": kind}
                        fire, _h = relay.should_escalate(signal, None)
                        self.assertTrue(fire)
                        msg = relay.compose_message(signal)
                        self.assertIn("run /login", msg)
                        # Debounced: the same standing state does not re-fire.
                        self.assertFalse(relay.should_escalate(signal, _h)[0])

    def test_login_dm_names_where_to_run_login(self):
        state, detail, prompt, kind = ciw.compose_state(AUTH_FAILURE_LINES["oauth-401"], "idle", True)
        signal = {"state": state, "detail": detail, "prompt": prompt, "kind": kind}

        def boom():
            raise RuntimeError("no backend file")

        cases = (
            ("attach", lambda: {"socket": "/tmp/s.sock", "session": "core"},
             "`tmux -S /tmp/s.sock attach -t core`"),
            ("unreadable", boom, "the Runtime panel"),
        )
        orig = relay._derive_backend
        try:
            for name, fn, where in cases:
                with self.subTest(backend=name):
                    relay._derive_backend = fn
                    msg = relay.compose_message(signal)
                    self.assertIn(f"run /login in the core terminal ({where})", msg)
                    self.assertNotIn("restart.sh", msg)
        finally:
            relay._derive_backend = orig


class NegativeControls(unittest.TestCase):
    def test_no_reader_flags_login(self):
        for name, pane in NEGATIVE_PANES.items():
            with self.subTest(pane=name):
                self.assertFalse(rh.needs_login(pane))
                self.assertIsNone(worker_auth_state.login_expired(pane))
                self.assertFalse(any(n == "needs-login"
                                     for _f, n, _l in cli_wedge.live_banner_lines(pane)))
                self.assertNotIn("needs-login", cli_wedge.matched_abnormal([pane]))
                state = ciw.compose_state(pane, "idle", True)[0]
                self.assertNotEqual(state, "logged-out")

    def test_bare_401_lines_are_not_login(self):
        for line in ("401", "PR 401 is ready", "#401 merged", "API Error: 500 overloaded",
                     "HTTP 401 Unauthorized from the webhook test server"):
            with self.subTest(line=line):
                self.assertFalse(cli_wedge.needs_login_line(line))
                self.assertIsNone(worker_auth_state.login_expired(line))


if __name__ == "__main__":
    unittest.main(verbosity=1)
