#!/usr/bin/env python3
"""src/agent_display_name.py: every self-introduction names the agent by its s.space display name.

A test agent whose display name differs from its internal persona (Stand) name must introduce
itself by the display name on every greeting path; with no display name the documented fallback
order applies (connection record, configured name, mxid localpart, "Sutando")."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

isolate_os_environ()
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import agent_display_name as adn  # noqa: E402

MXID = "@blue-fox.agent:example.invalid"
DISPLAY = "Display Name X"
PERSONA = "Persona Codename"


def _load_getting_started():
    spec = importlib.util.spec_from_file_location(
        "getting_started_adn", REPO / "skills" / "tutorial" / "scripts" / "getting_started.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def profile(name):
    """A homeserver whose profile endpoint answers `name` (None: unreachable)."""
    def fetch(url):
        if url.endswith("/.well-known/matrix/client") or name is None:
            raise OSError("offline")
        return json.dumps({"displayname": name}).encode()
    return fetch


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adn-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        (self.workspace / "state").mkdir(parents=True)
        # The persona the bug leaked: present, and never the answer.
        (self.workspace / "stand-identity.json").write_text(json.dumps({"name": PERSONA}))
        self.env = {"HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
                    "SUTANDO_WORKSPACE": str(self.workspace), "PATH": os.environ.get("PATH", "")}
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def rows(self, *rows):
        return mock.patch("connections.read_rows", return_value=list(rows))


class ResolveTest(Fixture):
    def test_the_live_display_name_wins_over_every_other_name(self):
        row = {"agent_mxid": MXID, "agent_display_name": "Older Name"}
        with self.rows(row), mock.patch.dict(os.environ, {"SUTANDO_DISPLAY_NAME": PERSONA}):
            self.assertEqual(adn.resolve(MXID, fetch=profile(DISPLAY)), (DISPLAY, "profile"))

    def test_offline_the_connection_record_names_this_agent_only(self):
        other = {"agent_mxid": "@other.agent:example.invalid", "agent_display_name": "Someone Else"}
        mine = {"agent_mxid": MXID, "agent_display_name": f"  {DISPLAY} "}
        with self.rows(other, mine):
            self.assertEqual(adn.resolve(MXID, fetch=profile(None)), (DISPLAY, "connection"))
        with self.rows(other):
            self.assertNotEqual(adn.resolve(MXID, fetch=profile(None))[0], "Someone Else")

    def test_then_a_configured_name_in_the_documented_order(self):
        with self.rows(), mock.patch.dict(os.environ, {"AGENT_DISPLAY_NAME": "Second",
                                                       "SUTANDO_DISPLAY_NAME": "First"}):
            self.assertEqual(adn.resolve(MXID, fetch=profile(None)), ("First", "configured"))
        with self.rows(), mock.patch.dict(os.environ, {"AGENT_DISPLAY_NAME": "Second"}):
            self.assertEqual(adn.resolve(MXID, fetch=profile(None)), ("Second", "configured"))

    def test_then_the_localpart_then_sutando_and_never_the_persona(self):
        with self.rows():
            self.assertEqual(adn.resolve(MXID, fetch=profile(None)), ("Blue Fox", "localpart"))
            self.assertEqual(adn.resolve(MXID, fetch=profile("   ")), ("Blue Fox", "localpart"))
            self.assertEqual(adn.resolve("", fetch=profile(None)), ("Sutando", "fallback"))
            self.assertEqual(adn.resolve("not-an-mxid", fetch=profile(DISPLAY)), ("Sutando", "fallback"))

    def test_an_unreadable_connection_record_falls_through(self):
        with mock.patch("connections.read_rows", side_effect=RuntimeError("boom")):
            self.assertEqual(adn.resolve(MXID, fetch=profile(None))[1], "localpart")


class EntryTest(Fixture):
    """resolve() with no mxid finds this agent's own, and the CLI routes --room / --agent-mxid."""

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(adn, "profile_name", return_value=""):
            self.assertEqual(adn._main(list(argv)), 0)
        return out.getvalue()

    def test_resolve_reads_the_agent_mxid_and_survives_none(self):
        with self.rows(), mock.patch.dict(os.environ, {"AGENT_MXID": MXID}):
            self.assertEqual(adn.resolve(fetch=profile(DISPLAY)), (DISPLAY, "profile"))
        with self.rows(), mock.patch.object(adn, "agent_mxid", side_effect=RuntimeError("no identity")):
            self.assertEqual(adn.resolve(fetch=profile(DISPLAY)), ("Sutando", "fallback"))

    def test_cli_uses_the_room_agent_then_the_given_mxid_then_its_own(self):
        with self.rows({"agent_mxid": MXID, "agent_display_name": DISPLAY}):
            with mock.patch("channel_credentials.room_agent_mxid", return_value=MXID):
                got = json.loads(self.main("--room", "!r:example.invalid", "--json"))
            self.assertEqual(got, {"name": DISPLAY, "source": "connection", "agent_mxid": MXID})
            self.assertEqual(self.main("--agent-mxid", MXID).strip(), DISPLAY)
            with mock.patch("channel_credentials.room_agent_mxid", return_value=None), \
                    mock.patch.object(adn, "agent_mxid", return_value=MXID):
                self.assertEqual(self.main("--room", "!r:example.invalid").strip(), DISPLAY)
            with mock.patch.object(adn, "agent_mxid", side_effect=RuntimeError("no identity")):
                self.assertEqual(json.loads(self.main("--json"))["name"], "Sutando")


class OtherServerRoomTest(Fixture):
    """A room no connection names an identity for never borrows the primary agent's name."""

    def greet(self, **room_agent):
        primary = {"agent_mxid": MXID, "agent_display_name": DISPLAY}
        out = io.StringIO()
        with self.rows(primary), mock.patch.object(adn, "profile_name", return_value=""), \
                mock.patch.object(adn, "agent_mxid", return_value=MXID), \
                mock.patch("channel_credentials.room_agent_mxid", **room_agent), \
                contextlib.redirect_stdout(out):
            self.assertEqual(adn._main(["--room", "!r:other.invalid", "--json"]), 0)
        return json.loads(out.getvalue())

    def test_a_room_whose_connection_names_no_identity(self):
        self.assertEqual(self.greet(return_value=""),
                         {"name": "Sutando", "source": "fallback", "agent_mxid": ""})

    def test_a_room_no_connection_serves(self):
        import connections
        got = self.greet(side_effect=connections.NoConnectionForRoom("no row for other.invalid"))
        self.assertEqual(got, {"name": "Sutando", "source": "fallback", "agent_mxid": ""})

    def test_a_failing_tier_never_fails_the_resolver(self):
        with self.rows(), mock.patch.object(adn, "configured_name", side_effect=ImportError("gone")):
            self.assertEqual(adn.resolve(MXID, fetch=profile(None)), ("Blue Fox", "localpart"))


class GreetingPathsTest(Fixture):
    """Each introduction path takes the name from the one resolver."""

    def test_tutorial_page_name_is_the_display_name(self):
        gs = _load_getting_started()
        with self.rows():
            self.assertEqual(gs.resolve_name("auto", MXID, profile(DISPLAY)), DISPLAY)
            self.assertNotEqual(gs.resolve_name("auto", MXID, profile(None)), PERSONA)

    def test_cli_prints_the_name_an_agent_greets_with(self):
        env = isolated_child_env(**{**self.env, "AGENT_MXID": "@bad", "SUTANDO_DISPLAY_NAME": DISPLAY})
        out = subprocess.run([sys.executable, str(REPO / "src" / "agent_display_name.py"), "--json"],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual((got["name"], got["source"]), (DISPLAY, "configured"))
        plain = subprocess.run([sys.executable, str(REPO / "src" / "agent_display_name.py")],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(plain.stdout.strip(), DISPLAY)

    def test_every_greeting_instruction_names_the_resolver(self):
        for rel in ("CLAUDE.md", "AGENTS.md", "skills/room-onboard/SKILL.md", "skills/tutorial/SKILL.md"):
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertIn("agent_display_name.py", text, rel)
        onboard = (REPO / "skills/room-onboard/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("python3 src/agent_display_name.py --room <channel_id>", onboard)


if __name__ == "__main__":
    unittest.main()
