#!/usr/bin/env python3
"""One "core is blocked on you" alert per surface.

The monitor raises a HITL card for a blocked core and the s.space bridge projects it into the
owner's DM; the relay used to post its own plain alert there as well, so the owner got two.
The cards here come from the monitor's own `escalate()`, the producer the bridge projects from.

Run: python3 tests/core-supervisor-relay-one-alert.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SRC / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load("core_supervisor_relay_one_alert", "core-supervisor-relay.py")
ciw = _load("ciw_one_alert", "core-input-watch.py")

from hitl import core_card  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402

SESSION = "sutando-core"
UNKNOWN = {"state": "blocked-human", "detail": "awaiting user: unknown", "kind": "unknown",
           "prompt": "Do you want to proceed with the migration?", "session": SESSION}
DM = "!dm:example.org"
# The default socket lives under a path with a space in it.
SOCKET = "/Users/someone/Library/Application Support/app/engine/sutando-tmux.sock"


class _Workspace:
    """A workspace laid out as the monitor writes it: state/core-supervisor.json beside the
    HITL store, which the bridge projects from."""

    def __init__(self, td):
        self.state = os.path.join(td, "state")
        os.makedirs(self.state)
        self.signal = os.path.join(self.state, "core-supervisor.json")
        self.store = relay.card_store_for(self.signal)
        os.makedirs(self.store)
        self.manager = HitlManager(HitlStore(pathlib.Path(self.store)))

    def write_signal(self, sig, age=0.0):
        with open(self.signal, "w") as f:
            json.dump(sig, f)
        if age:
            t = time.time() - age
            os.utime(self.signal, (t, t))

    def raise_card(self, sig, projected=True):
        req = ciw.escalate(self.manager, sig["state"], sig["detail"], sig.get("kind"),
                           sig.get("prompt"), sig.get("session") or SESSION)
        self.last = req
        if projected:
            self.manager.record_projection(req.id, req.revision, "$card")
        return req


@contextlib.contextmanager
def _sends():
    sent, pops = [], []
    with patch.object(relay, "_channel_notify",
                      lambda m, s, c, extra=None: sent.append((s, c, m)) or True), \
            patch.object(relay, "_macos_notify", lambda m: pops.append(m) or True), \
            patch.object(relay, "_derive_backend", lambda: {"socket": SOCKET, "session": SESSION}):
        yield sent, pops


def _cycle(ws, sig, source="ag2space", channel=DM, age=0.0, state_file=None):
    outcome = {}
    msg = relay.run_cycle(sig, state_file or "", macos=True, source=source, channel=channel,
                          card_store=ws.store, signal_age=age, outcome=outcome)
    return msg, outcome


class TestSharedPredicate(unittest.TestCase):
    def test_the_monitor_cards_exactly_the_states_the_relay_checks(self):
        self.assertIs(ciw._CHAT_ESCALATE_STATES, core_card.CARD_STATES)

    def test_status_follows_the_cards_projection(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _Workspace(td)
            self.assertEqual(core_card.card_status(ws.store, SESSION, "blocked-human"), core_card.NONE)
            req = ws.raise_card(UNKNOWN, projected=False)
            self.assertEqual(core_card.card_status(ws.store, SESSION, "blocked-human"), core_card.PENDING)
            ws.manager.record_projection(req.id, req.revision, "$card")
            self.assertEqual(core_card.card_status(ws.store, SESSION, "blocked-human"), core_card.SHOWN)
            # Another session's card, or one for another state, is not this block's.
            self.assertEqual(core_card.card_status(ws.store, "other-core", "blocked-human"), core_card.NONE)
            self.assertEqual(core_card.card_status(ws.store, SESSION, "logged-out"), core_card.NONE)
            ws.manager.resolve(req.id)
            self.assertEqual(core_card.card_status(ws.store, SESSION, "blocked-human"), core_card.NONE)

    def test_an_unreadable_store_reads_none(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _Workspace(td)
            ws.raise_card(UNKNOWN)
            with patch.object(HitlManager, "active", side_effect=OSError("store unreadable")):
                self.assertEqual(core_card.card_status(ws.store, SESSION, "blocked-human"),
                                 core_card.NONE)

    def test_a_missing_store_reads_none(self):
        self.assertEqual(core_card.card_status("/nonexistent/hitl", SESSION, "blocked-human"),
                         core_card.NONE)


class TestOneAlertPerSurface(unittest.TestCase):
    def test_a_projected_card_on_s_space_takes_the_place_of_the_plain_message(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, pops):
            ws = _Workspace(td)
            ws.raise_card(UNKNOWN)
            sf = os.path.join(td, "relay.state")
            msg, outcome = _cycle(ws, UNKNOWN, state_file=sf)
            self.assertIsNone(msg)
            self.assertEqual(sent, [])
            self.assertEqual(pops, [], "the card's own DM message is the notification")
            self.assertEqual(outcome.get("card"), "carried")
            # The block counts as told: a later cycle stays quiet even once the card is gone.
            self.assertTrue(os.path.exists(sf))
            ws.manager.resolve(ws.last.id)
            self.assertIsNone(_cycle(ws, UNKNOWN, state_file=sf, age=600)[0])
            self.assertEqual(sent, [])

    def test_main_reads_the_store_beside_the_signal(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            ws.write_signal(UNKNOWN)
            ws.raise_card(UNKNOWN)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                relay.main(["--signal", ws.signal, "--notify-source", "ag2space",
                            "--notify-channel", DM, "--no-macos", "--no-seats",
                            "--state-file", os.path.join(td, "relay.state")])
            self.assertEqual(sent, [])
            self.assertIn("HITL card", out.getvalue())

    def test_main_hands_the_relay_the_signals_age(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _Workspace(td)
            ws.write_signal(UNKNOWN, age=300)
            seen = {}
            with patch.object(relay, "run_cycle", lambda *a, **k: seen.update(k)), \
                    contextlib.redirect_stdout(io.StringIO()):
                relay.main(["--signal", ws.signal, "--no-macos", "--no-seats"])
            self.assertAlmostEqual(seen["signal_age"], 300, delta=5)
            self.assertEqual(seen["card_store"], ws.store)

    def test_a_card_check_that_cannot_load_sends_the_alert(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            ws.raise_card(UNKNOWN)
            with patch.dict(sys.modules, {"hitl": None, "hitl.core_card": None}):
                self.assertIsNotNone(_cycle(ws, UNKNOWN)[0])
            self.assertEqual(len(sent), 1)

    def test_a_card_on_its_way_holds_the_message_then_gives_up(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            sf = os.path.join(td, "relay.state")
            # The monitor writes the signal a moment before it raises the card.
            msg, outcome = _cycle(ws, UNKNOWN, state_file=sf, age=1)
            self.assertIsNone(msg)
            self.assertEqual(outcome.get("card"), "wait")
            self.assertFalse(os.path.exists(sf), "a held alert is not a told one")
            # No card after one relay pass: the monitor is not raising one.
            self.assertIsNotNone(_cycle(ws, UNKNOWN, age=relay.CARD_RAISE_WAIT_S + 1)[0])
            sent.clear()
            ws.raise_card(UNKNOWN, projected=False)
            self.assertIsNone(_cycle(ws, UNKNOWN, state_file=sf, age=30)[0])
            self.assertEqual(sent, [])
            # A card that never lands does not swallow the alert.
            msg, _ = _cycle(ws, UNKNOWN, state_file=sf, age=relay.CARD_WAIT_S + 1)
            self.assertIsNotNone(msg)
            self.assertEqual(len(sent), 1)

    def test_a_host_that_raises_no_cards_does_not_wait_for_one(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            os.rmdir(ws.store)
            self.assertIsNotNone(_cycle(ws, UNKNOWN, age=1)[0])
            self.assertEqual(len(sent), 1)

    def test_a_card_given_up_on_leaves_the_message_to_the_relay(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            req = ws.raise_card(UNKNOWN, projected=False)
            ws.manager.refuse_projection(req.id, req.revision, "HTTP 400", 1, lambda n: {})
            self.assertTrue(ws.manager.projection_failed(req.id))
            self.assertIsNotNone(_cycle(ws, UNKNOWN, age=1)[0])
            self.assertEqual(len(sent), 1)

    def test_a_channel_with_no_card_still_gets_the_message(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            ws.raise_card(UNKNOWN)
            for source in ("discord", "slack", "telegram"):
                self.assertIsNotNone(_cycle(ws, UNKNOWN, source=source, channel="1")[0], source)
            self.assertEqual([s for s, _, _ in sent], ["discord", "slack", "telegram"])

    def test_a_state_the_monitor_does_not_card_still_sends(self):
        sig = dict(relay.UNREADABLE_SIGNAL)
        self.assertNotIn(sig["state"], core_card.CARD_STATES)
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            ws.raise_card(UNKNOWN)  # a card for another block must not hide this one
            self.assertIsNotNone(_cycle(ws, sig, age=1)[0])
            self.assertEqual(len(sent), 1)

    def test_another_sessions_card_does_not_hide_this_block(self):
        with tempfile.TemporaryDirectory() as td, _sends() as (sent, _):
            ws = _Workspace(td)
            ws.raise_card(dict(UNKNOWN, session="sutando-core-2"))
            self.assertIsNotNone(_cycle(ws, UNKNOWN, age=600)[0])
            self.assertEqual(len(sent), 1)


class TestPlainMessage(unittest.TestCase):
    def _message(self, backend):
        with patch.object(relay, "_derive_backend", lambda: backend):
            return relay.compose_message(UNKNOWN, surface="discord")

    def test_the_command_is_on_its_own_line_and_pastes_into_a_shell(self):
        msg = self._message({"socket": SOCKET, "session": SESSION})
        lines = msg.splitlines()
        self.assertTrue(lines[0].startswith("⚠️ Sutando is waiting on you at its terminal"), lines[0])
        self.assertIn("Do you want to proceed with the migration?", lines[0])
        fence = lines.index("```")
        command = lines[fence + 1]
        self.assertEqual(lines[fence + 2], "```")
        self.assertEqual(shlex.split(command), ["tmux", "-S", SOCKET, "attach", "-t", SESSION])
        self.assertEqual(lines[fence - 1], "Open it with:")
        self.assertEqual(lines[-1], "A chat reply can't answer it.")

    def test_the_s_space_fallback_names_no_card(self):
        """On s.space the relay sends only when no card shows the block, so no text it sends
        there names one, while each keeps its own remedy."""
        limit = {"state": "blocked-human", "detail": "awaiting user: session-limit", "kind": "session-limit",
                 "prompt": "You've hit your session limit · resets 12:10pm", "session": SESSION}
        fable = {"state": "blocked-human", "detail": "awaiting user: fable-limit-unfocused",
                 "kind": "fable-limit-unfocused", "session": SESSION,
                 "prompt": "You've reached your Fable limit\n❯ Continue with Fable 5.1"}
        login = {"state": "logged-out", "detail": "core not authenticated", "prompt": None,
                 "kind": None, "session": SESSION}
        cases = ((UNKNOWN, "A chat reply can't answer it."), (limit, "resumes on its own at 12:10pm"),
                 (fable, "Pick the switch, or restart the core, in the Runtime panel"),
                 (login, "run /login in the core terminal"))
        for backend in ({"socket": SOCKET, "session": SESSION}, None):
            for sig, remedy in cases:
                with self.subTest(kind=sig["kind"], backend=bool(backend)), \
                        patch.object(relay, "_derive_backend", lambda: backend):
                    msg = relay.compose_message(sig, surface="ag2space")
                    self.assertNotIn("card", msg.lower())
                    self.assertNotIn("Restart core", msg)
                    self.assertNotIn("tap ", msg.lower())
                    self.assertIn(remedy, msg)

    def test_no_recorded_terminal_names_the_places_in_plain_words(self):
        msg = self._message(None)
        self.assertNotIn("tmux", msg)
        self.assertIn("Open the core's terminal or the Runtime panel", msg)

    def test_the_login_remedy_quotes_its_command_too(self):
        with patch.object(relay, "_derive_backend", lambda: {"socket": SOCKET, "session": SESSION}):
            msg = relay.compose_message({"state": "logged-out", "detail": "core not authenticated",
                                         "prompt": None, "kind": None})
        command = msg.split("`")[1]
        self.assertEqual(shlex.split(command), ["tmux", "-S", SOCKET, "attach", "-t", SESSION])


if __name__ == "__main__":
    unittest.main()
