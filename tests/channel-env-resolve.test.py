#!/usr/bin/env python3
"""Contract tests for src/channel_env_resolve.py and its scripts/channel-env.sh
wrapper — which channel env file a caller is told to source.

The caller's contract is `set -a; . "$(bash scripts/channel-env.sh <src>)"; set
+a`, so the returned path is EXECUTED. Two rules therefore gate a candidate,
and each is pinned here at the level that actually failed:

  * containment — the first resolver keyed only on `[ -f "$f" ]`, which follows
    symlinks, so a `channels/<src>/.env` linked outside the tree was returned
    and sourced while channel_env_is_contained() said False for the same path.
  * a non-empty token — the first resolver grepped for `(REMOTE_TASK_TOKEN|
    AG2_REMOTE_TOKEN)=`, key-presence only, so a blank `.env` was selected over
    a sibling holding the real token and the notify still failed.

Both rules are owned elsewhere (channel_env_containment, channel_token); these
cases prove selection DELEGATES rather than approximates, including the
app-support relocation that is the live desktop layout.

Run: python3 tests/channel-env-resolve.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import channel_env_resolve as m  # noqa: E402

TOKEN_LINE = 'REMOTE_TASK_TOKEN="real-token"\n'


_AMBIENT = ("SUTANDO_APP_SUPPORT", "AG2_DEVICE_ENV")


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _AMBIENT}
        for k in _AMBIENT:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _channel(self, source: str = "ag2space") -> tuple[Path, Path]:
        """(channels_dir, channels_dir/<source>), both created."""
        channels = self._tmpdir() / "channels"
        chan = channels / source
        chan.mkdir(parents=True)
        return channels, chan


class TestContainment(_Base):
    def test_symlink_out_of_channels_tree_is_refused(self):
        channels, chan = self._channel()
        outside = self._tmpdir() / "secret.env"
        outside.write_text('REMOTE_TASK_URL="https://evil.example"\n' + TOKEN_LINE)
        (chan / ".env").symlink_to(outside)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))

    def test_plain_file_inside_channels_tree_is_selected(self):
        """Positive control: the refusal above must not be vacuous."""
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_app_support_relocation_is_selected_when_configured(self):
        """The live desktop layout: .env symlinks to $SUTANDO_APP_SUPPORT."""
        channels, chan = self._channel()
        app = self._tmpdir()
        relocated = app / "channels" / "ag2space" / ".env"
        relocated.parent.mkdir(parents=True)
        relocated.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(relocated)
        os.environ["SUTANDO_APP_SUPPORT"] = str(app)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_same_relocation_fails_closed_when_unconfigured(self):
        channels, chan = self._channel()
        app = self._tmpdir()
        relocated = app / "channels" / "ag2space" / ".env"
        relocated.parent.mkdir(parents=True)
        relocated.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(relocated)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))

    def test_link_to_another_channels_file_under_app_root_is_refused(self):
        channels, chan = self._channel()
        app = self._tmpdir()
        other = app / "channels" / "other" / ".env"
        other.parent.mkdir(parents=True)
        other.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(other)
        os.environ["SUTANDO_APP_SUPPORT"] = str(app)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))


class TestNonEmptyToken(_Base):
    def test_blank_env_loses_to_sibling_with_real_token(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("REMOTE_TASK_TOKEN=\n")
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"),
                         chan / "relay-client.env")

    def test_dot_env_still_wins_when_it_holds_a_real_token(self):
        """Precedence is unchanged for a correct layout — only blanks lose."""
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        (chan / "relay-client.env").write_text('REMOTE_TASK_TOKEN="other"\n')
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_quoted_empty_value_does_not_count(self):
        channels, chan = self._channel()
        (chan / ".env").write_text('REMOTE_TASK_TOKEN=""\n')
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"),
                         chan / "relay-client.env")

    def test_legacy_alias_counts(self):
        channels, chan = self._channel()
        (chan / ".env").write_text('AG2_REMOTE_TOKEN="legacy"\n')
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_no_token_anywhere_resolves_to_none(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("MATRIX_USER=x\n")
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))

    def test_missing_channel_dir_resolves_to_none(self):
        channels, _ = self._channel()
        self.assertIsNone(m.resolve_channel_env(channels, "nosuch"))

    def test_sibling_order_is_deterministic(self):
        channels, chan = self._channel()
        (chan / "b-client.env").write_text('REMOTE_TASK_TOKEN="b"\n')
        (chan / "a-client.env").write_text('REMOTE_TASK_TOKEN="a"\n')
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"),
                         chan / "a-client.env")


class TestDeviceEnv(_Base):
    """`$AG2_DEVICE_ENV` is the file the desktop launcher names. It is tried
    first and trusted because the launcher names it, not because it is
    contained; a blank, missing or unset one falls through to the tree."""

    def _device_file(self, body: str = 'REMOTE_TASK_TOKEN="fake-device-token"\n') -> Path:
        f = self._tmpdir() / "space.ag2.app" / "channels" / "ag2space" / ".env"
        f.parent.mkdir(parents=True)
        f.write_text(body)
        return f

    def test_desktop_layout_selects_the_launcher_named_file(self):
        channels = self._tmpdir() / "channels"
        dev = self._device_file()
        os.environ["AG2_DEVICE_ENV"] = str(dev)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), dev)

    def test_launcher_named_file_is_trusted_outside_every_containment_root(self):
        """The trust rule is explicit: the same path fails containment."""
        channels, _ = self._channel()
        dev = self._device_file()
        os.environ["AG2_DEVICE_ENV"] = str(dev)
        self.assertFalse(m.channel_env_is_contained(dev, channels, "ag2space"))
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), dev)

    def test_launcher_named_file_outranks_the_channels_tree(self):
        channels, chan = self._channel()
        (chan / ".env").write_text('REMOTE_TASK_TOKEN="fake-tree-token"\n')
        dev = self._device_file()
        os.environ["AG2_DEVICE_ENV"] = str(dev)
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), dev)

    def test_blank_launcher_named_file_falls_through_to_the_tree(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        os.environ["AG2_DEVICE_ENV"] = str(self._device_file("REMOTE_TASK_TOKEN=\n"))
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_missing_launcher_named_file_falls_through_to_the_tree(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        os.environ["AG2_DEVICE_ENV"] = str(self._tmpdir() / "absent.env")
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_directory_is_not_a_regular_file(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        os.environ["AG2_DEVICE_ENV"] = str(self._tmpdir())
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_blank_variable_falls_through_to_the_tree(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        os.environ["AG2_DEVICE_ENV"] = "  "
        self.assertEqual(m.resolve_channel_env(channels, "ag2space"), chan / ".env")

    def test_other_sources_ignore_the_launcher_named_file(self):
        channels, chan = self._channel("discord")
        (chan / ".env").write_text(TOKEN_LINE)
        os.environ["AG2_DEVICE_ENV"] = str(self._device_file())
        self.assertEqual(m.resolve_channel_env(channels, "discord"), chan / ".env")

    def test_blank_launcher_file_and_no_tree_resolves_to_none(self):
        channels = self._tmpdir() / "channels"
        os.environ["AG2_DEVICE_ENV"] = str(self._device_file("REMOTE_TASK_TOKEN=''\n"))
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))


class TestLaunchKeyEnv(_Base):
    """A host signed in only on a secondary homeserver writes a launch key and no primary token."""

    LAUNCH = "AG2_REMOTE_TOKEN_S_DSPACE='https://relay.example|s'\n"

    def test_a_file_with_only_a_launch_key_is_found_for_launch_keys_not_for_sourcing(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(self.LAUNCH)
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"))
        self.assertEqual(m.resolve_launch_key_env(channels, "ag2space"), chan / ".env")

    def test_the_primary_token_file_keeps_precedence(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(self.LAUNCH)
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        self.assertEqual(m.resolve_launch_key_env(channels, "ag2space"), chan / "relay-client.env")

    def test_a_blank_launch_key_does_not_count(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("AG2_REMOTE_TOKEN_S_DSPACE=''\n")
        self.assertIsNone(m.resolve_launch_key_env(channels, "ag2space"))

    def test_a_launch_key_file_linked_out_of_the_tree_is_refused(self):
        channels, chan = self._channel()
        outside = self._tmpdir() / "secret.env"
        outside.write_text(self.LAUNCH)
        (chan / ".env").symlink_to(outside)
        self.assertIsNone(m.resolve_launch_key_env(channels, "ag2space"))

    def test_the_launcher_named_file_with_only_a_launch_key_is_used(self):
        channels, _ = self._channel()
        named = self._tmpdir() / "device.env"
        named.write_text(self.LAUNCH)
        os.environ["AG2_DEVICE_ENV"] = str(named)
        self.assertEqual(m.resolve_launch_key_env(channels, "ag2space"), named)


class TestActiveLane(_Base):
    """A device file holding only the desktop's active lane's own keys resolves that lane's token,
    by key name in that same file; no other lane's key is read and a primary token still wins."""

    LANE_TOKEN = "https://gw.example/relay|lane-secret"
    NAMES = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")

    def _install(self, device_body: str, lane: str = "s_dspace") -> Path:
        root = self._tmpdir()
        channels = root / "channels"
        (channels / "ag2space").mkdir(parents=True)
        (channels / "active-gateway-lane").write_text(lane + "\n")
        device = channels / "ag2space" / ".env"
        device.write_text(device_body)
        os.environ["SUTANDO_APP_SUPPORT"] = str(root)
        os.environ["AG2_DEVICE_ENV"] = str(device)
        return channels

    def _lane_only(self, lane: str = "s_dspace") -> Path:
        return self._install(f'AG2_REMOTE_TOKEN_S_DSPACE="{self.LANE_TOKEN}"\n'
                             "REMOTE_TASK_CHANNEL_DIR_S_DSPACE=s.space\n", lane)

    def test_a_lane_only_device_file_resolves_the_active_lanes_token(self):
        channels = self._lane_only()
        self.assertIsNone(m.resolve_channel_env(channels, "ag2space"), "the sourced-path contract is unchanged")
        self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {"AG2_REMOTE_TOKEN": self.LANE_TOKEN})
        self.assertEqual(m.channel_values(channels, "ag2space", ("REMOTE_TASK_CHANNEL_DIR",)),
                         {"REMOTE_TASK_CHANNEL_DIR": "s.space"})
        self.assertTrue(m.channel_configured(channels, "ag2space"))

    def test_a_key_of_a_lane_that_is_not_active_is_never_used(self):
        channels = self._install(f'AG2_REMOTE_TOKEN_OTHER_LANE="{self.LANE_TOKEN}"\n')
        self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {})
        self.assertFalse(m.channel_configured(channels, "ag2space"))

    def test_no_active_lane_reads_no_suffixed_key(self):
        channels = self._lane_only()
        os.environ.pop("SUTANDO_APP_SUPPORT")
        self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {})
        for lane in ("all", "primary", "../x"):
            with self.subTest(lane=lane):
                channels = self._lane_only(lane)
                self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {})

    def test_a_primary_token_in_the_device_file_still_wins(self):
        channels = self._install(f'AG2_REMOTE_TOKEN="https://gw.example/relay|primary"\n'
                                 f'AG2_REMOTE_TOKEN_S_DSPACE="{self.LANE_TOKEN}"\n')
        self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES),
                         {"AG2_REMOTE_TOKEN": "https://gw.example/relay|primary"})

    def test_a_primary_token_in_the_channels_tree_still_wins(self):
        channels = self._lane_only()
        (channels / "ag2space" / "relay-client.env").write_text(TOKEN_LINE)
        self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {"REMOTE_TASK_TOKEN": "real-token"})

    def test_the_lane_key_name_follows_the_launch_key_rule(self):
        for lane, key in (("s_dspace", "AG2_REMOTE_TOKEN_S_DSPACE"),
                          ("s_hspace_dfoo", "AG2_REMOTE_TOKEN_S_HSPACE_DFOO"),
                          ("my-lane", "AG2_REMOTE_TOKEN_MY_LANE")):
            with self.subTest(lane=lane):
                self.assertEqual(m.lane_var("AG2_REMOTE_TOKEN", lane), key)
                channels = self._install(f'{key}="{self.LANE_TOKEN}"\n'
                                         'AG2_REMOTE_TOKEN_OTHER="https://gw.example/relay|wrong"\n', lane)
                self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES),
                                 {"AG2_REMOTE_TOKEN": self.LANE_TOKEN})

    def test_an_identity_comes_from_the_lanes_declared_channel_dir(self):
        channels = self._lane_only()
        (channels / "s.space").mkdir()
        (channels / "s.space" / ".env").write_text('AGENT_MXID=@lane.agent:example.test\n'
                                                   'AG2_REMOTE_TOKEN="https://gw.example/relay|dir-token"\n')
        got = m.channel_values(channels, "ag2space", ("AGENT_MXID", "AG2_REMOTE_TOKEN"))
        self.assertEqual(got, {"AGENT_MXID": "@lane.agent:example.test", "AG2_REMOTE_TOKEN": self.LANE_TOKEN},
                         "the token is the device file's lane key alone")

    def test_a_lane_token_pairs_only_with_the_lanes_own_url_key(self):
        names = ("REMOTE_TASK_URL", "AG2_REMOTE_TOKEN")
        channels = self._install(f'AG2_REMOTE_TOKEN_S_DSPACE="{self.LANE_TOKEN}"\n'
                                 "REMOTE_TASK_URL=https://leftover.example/relay\n"
                                 "REMOTE_TASK_CHANNEL_DIR_S_DSPACE=s.space\n")
        (channels / "s.space").mkdir()
        (channels / "s.space" / ".env").write_text("REMOTE_TASK_URL=https://lane-dir.example/relay\n")
        self.assertEqual(m.channel_values(channels, "ag2space", names), {"AG2_REMOTE_TOKEN": self.LANE_TOKEN})
        device = channels / "ag2space" / ".env"
        device.write_text(device.read_text() + "REMOTE_TASK_URL_S_DSPACE=https://gw.example/relay\n")
        self.assertEqual(m.channel_values(channels, "ag2space", names),
                         {"AG2_REMOTE_TOKEN": self.LANE_TOKEN, "REMOTE_TASK_URL": "https://gw.example/relay"})

    def test_a_doubly_quoted_lane_key_reads_as_the_clean_relay_token(self):
        channels = self._install(f'''AG2_REMOTE_TOKEN_S_DSPACE="'{self.LANE_TOKEN}'"\n''')
        self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {"AG2_REMOTE_TOKEN": self.LANE_TOKEN})

    def test_a_lane_identity_key_in_the_device_file_wins(self):
        channels = self._install(f'AG2_REMOTE_TOKEN_S_DSPACE="{self.LANE_TOKEN}"\n'
                                 "AGENT_MXID_S_DSPACE=@key.agent:example.test\n")
        self.assertEqual(m.channel_values(channels, "ag2space", ("AGENT_MXID",)),
                         {"AGENT_MXID": "@key.agent:example.test"})

    def test_a_lane_dir_outside_the_tree_or_naming_ag2space_is_never_read(self):
        for line in ("REMOTE_TASK_CHANNEL_DIR_S_DSPACE=../outside", "REMOTE_TASK_CHANNEL_DIR_S_DSPACE=ag2space"):
            with self.subTest(line=line):
                channels = self._install(f'AG2_REMOTE_TOKEN_S_DSPACE="{self.LANE_TOKEN}"\n'
                                         f'AGENT_MXID=@device.agent:example.test\n{line}\n')
                (channels.parent / "outside").mkdir(exist_ok=True)
                (channels.parent / "outside" / ".env").write_text("AGENT_MXID=@out.agent:example.test\n")
                self.assertEqual(m.channel_values(channels, "ag2space", ("AGENT_MXID",)), {})

    def test_a_signed_out_lane_reads_nothing(self):
        channels = self._install("REMOTE_TASK_CHANNEL_DIR_S_DSPACE=s.space\n")
        (channels / "s.space").mkdir()
        (channels / "s.space" / ".env").write_text("AGENT_MXID=@lane.agent:example.test\n")
        self.assertEqual(m.channel_values(channels, "ag2space", ("AGENT_MXID", *self.NAMES)), {})
        self.assertFalse(m.channel_configured(channels, "ag2space"))

    def test_no_lane_reader_reads_nothing(self):
        channels = self._lane_only()
        with unittest.mock.patch.dict(sys.modules, {"gateway_serving": None}):
            self.assertIsNone(m._active_lane())
            self.assertEqual(m.channel_values(channels, "ag2space", self.NAMES), {})

    def test_the_sourced_path_and_launch_key_file_are_unchanged(self):
        channels = self._lane_only()
        self.assertEqual(m.resolve_launch_key_env(channels, "ag2space"), channels / "ag2space" / ".env")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(["x", str(channels), "ag2space"]), 1)

    def test_room_lookups_never_decode_a_lane(self):
        import connections
        channels = self._lane_only()
        legacy = {"instance": "", "server": "ag2.example.test"}
        with unittest.mock.patch.object(connections, "connection_for_room", return_value=legacy):
            self.assertIsNone(m.resolve_room_channel_env(channels, "ag2space", "!r:ag2.example.test"))
        (channels / "s.space").mkdir()
        (channels / "s.space" / ".env").write_text(TOKEN_LINE)
        row = {"instance": "s_dspace", "server": "s.example.test"}
        with unittest.mock.patch.object(connections, "connection_for_room", return_value=row):
            self.assertEqual(m.resolve_room_channel_env(channels, "ag2space", "!r:s.example.test"),
                             channels / "s.space" / ".env", "the declared dir line, not a decoded name")
        (channels / "ag2space" / ".env").write_text(f'AG2_REMOTE_TOKEN_S_DSPACE="{self.LANE_TOKEN}"\n')
        with unittest.mock.patch.object(connections, "connection_for_room", return_value=row), \
                self.assertRaises(connections.NoConnectionForRoom):
            m.resolve_room_channel_env(channels, "ag2space", "!r:s.example.test")

    def test_other_sources_never_follow_the_lane(self):
        channels = self._lane_only()
        (channels / "discord").mkdir()
        self.assertEqual(m.channel_values(channels, "discord", self.NAMES), {})
        self.assertIsNone(m.active_lane_env(channels, "discord"))


class TestCli(_Base):
    """`main()` in-process. The wrapper cases below cover the same paths through
    bash, but only as a subprocess — which no coverage run can see."""

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_wrong_arity_is_usage_error(self):
        rc, out, err = self._main(["channel_env_resolve.py", "only-one"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)
        self.assertEqual(out, "")

    def test_missing_channel_dir_exits_one_and_names_the_path(self):
        channels, _ = self._channel()
        rc, out, err = self._main(["x", str(channels), "nosuch"])
        self.assertEqual(rc, 1)
        self.assertIn("no channel dir", err)
        self.assertEqual(out, "")

    def test_no_qualifying_candidate_exits_one_and_names_both_vars(self):
        channels, chan = self._channel()
        (chan / ".env").write_text("REMOTE_TASK_TOKEN=\n")
        rc, out, err = self._main(["x", str(channels), "ag2space"])
        self.assertEqual(rc, 1)
        self.assertIn("REMOTE_TASK_TOKEN", err)
        self.assertIn("AG2_REMOTE_TOKEN", err)
        self.assertEqual(out, "", "stdout must stay empty — the caller sources it")

    def test_success_prints_only_the_path(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        rc, out, err = self._main(["x", str(channels), "ag2space"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(chan / ".env"))
        self.assertEqual(err, "")


class TestShellWrapper(_Base):
    """The wrapper is what callers actually invoke; pin its stdout + exit codes."""

    def _run(self, config_dir: Path, source: str = "ag2space", **env):
        environ = isolated_child_env()
        environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        for k in _AMBIENT:
            environ.pop(k, None)
        environ.update(env)
        return subprocess.run(
            ["bash", str(REPO / "scripts" / "channel-env.sh"), source],
            capture_output=True, text=True, env=environ)

    def test_prints_resolved_path_and_exits_zero(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / ".env"))

    def test_escaping_symlink_prints_nothing_on_stdout_and_exits_one(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        outside = self._tmpdir() / "secret.env"
        outside.write_text(TOKEN_LINE)
        (chan / ".env").symlink_to(outside)
        r = self._run(cfg)
        self.assertEqual(r.returncode, 1)
        # `. "$(...)"` on empty stdout must not source anything.
        self.assertEqual(r.stdout.strip(), "")

    def test_blank_env_is_skipped_for_the_sibling_holding_the_token(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text("REMOTE_TASK_TOKEN=\n")
        (chan / "relay-client.env").write_text(TOKEN_LINE)
        r = self._run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / "relay-client.env"))

    def _device_file(self, body: str = 'REMOTE_TASK_TOKEN="fake-device-token"\n') -> Path:
        f = self._tmpdir() / "space.ag2.app" / "channels" / "ag2space" / ".env"
        f.parent.mkdir(parents=True)
        f.write_text(body)
        return f

    def test_desktop_layout_with_no_channel_dir_prints_the_launcher_file(self):
        dev = self._device_file()
        r = self._run(self._tmpdir(), AG2_DEVICE_ENV=str(dev))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(dev))

    def test_desktop_layout_with_empty_channel_dir_prints_the_launcher_file(self):
        cfg = self._tmpdir()
        (cfg / "channels" / "ag2space").mkdir(parents=True)
        dev = self._device_file()
        r = self._run(cfg, AG2_DEVICE_ENV=str(dev))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(dev))

    def test_blank_launcher_file_falls_through_to_the_channel_env(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg, AG2_DEVICE_ENV=str(self._device_file("REMOTE_TASK_TOKEN=\n")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / ".env"))

    def test_blank_launcher_file_and_no_channel_dir_prints_nothing(self):
        r = self._run(self._tmpdir(),
                      AG2_DEVICE_ENV=str(self._device_file("REMOTE_TASK_TOKEN=\n")))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("no channel dir", r.stderr)

    def test_invalid_source_is_still_rejected_before_any_resolution(self):
        r = self._run(self._tmpdir(), source="../etc")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid source", r.stderr)

    def _unrunnable_python_on_path(self) -> str:
        """A PATH whose python3 cannot run — the shape a Mac without the Xcode
        CLT presents, which is why the wrapper must not shell a bare python3."""
        d = self._tmpdir() / "bin"
        d.mkdir()
        stub = d / "python3"
        stub.write_text('#!/bin/sh\necho "python3: not runnable" >&2\nexit 127\n')
        stub.chmod(0o755)
        return f"{d}:/usr/bin:/bin"

    def test_configured_interpreter_wins_over_an_unrunnable_path_python(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg, PATH=self._unrunnable_python_on_path(),
                      SUTANDO_PY=sys.executable)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(chan / ".env"))

    def test_no_runnable_interpreter_is_loud_and_prints_nothing_on_stdout(self):
        cfg = self._tmpdir()
        chan = cfg / "channels" / "ag2space"
        chan.mkdir(parents=True)
        (chan / ".env").write_text(TOKEN_LINE)
        r = self._run(cfg, PATH=self._unrunnable_python_on_path(),
                      SUTANDO_PY=str(self._tmpdir() / "absent"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertNotEqual(r.stderr.strip(), "")


class TestRoomRouting(_Base):
    """`--room` delegates the connection choice to `connections.connection_for_room`
    and only maps its row onto a channel dir; the choice itself is pinned in
    tests/connections-room-routing.test.py."""

    def _row(self, instance, server):
        return {"instance": instance, "server": server}

    def _routed(self, answer=None, refuse=None):
        import connections
        asked = []

        def fake(room_id, workspace=None):
            asked.append(room_id)
            if refuse:
                raise connections.NoConnectionForRoom(refuse)
            return answer
        p = unittest.mock.patch.object(connections, "connection_for_room", fake)
        p.start()
        self.addCleanup(p.stop)
        return asked

    def test_named_connection_gets_its_own_channel_dir(self):
        channels, _ = self._channel()
        own = channels / "dev-s-space-ag2space"
        own.mkdir()
        (own / ".env").write_text(TOKEN_LINE)
        (channels / "ag2space" / ".env").write_text(TOKEN_LINE)
        asked = self._routed(self._row("dev-s-space", "dev.s.space"))
        self.assertEqual(m.resolve_room_channel_env(channels, "ag2space", "!r:dev.s.space"), own / ".env")
        self.assertEqual(asked, ["!r:dev.s.space"])

    def test_legacy_connection_gets_the_ag2space_channel(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        self._routed(self._row("", "ag2.space"))
        self.assertEqual(m.resolve_room_channel_env(channels, "s-space-ag2space", "!r:ag2.space"), chan / ".env")

    def test_single_connection_keeps_the_given_source(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        self._routed(None)
        self.assertEqual(m.resolve_room_channel_env(channels, "ag2space", "!r:example.org"), chan / ".env")

    def test_named_connection_without_a_file_is_refused_by_name(self):
        import connections
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        self._routed(self._row("example-org", "example.org"))
        with self.assertRaises(connections.NoConnectionForRoom) as cm:
            m.resolve_room_channel_env(channels, "ag2space", "!r:example.org")
        self.assertIn("example-org", str(cm.exception))

    def test_cli_room_refusal_exits_one_with_nothing_on_stdout(self):
        channels, chan = self._channel()
        (chan / ".env").write_text(TOKEN_LINE)
        self._routed(refuse="no connection for server other.net")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.main(["x", str(channels), "ag2space", "--room", "!r:other.net"])
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertIn("other.net", err.getvalue())

    def test_cli_room_success_prints_the_routed_path(self):
        channels, _ = self._channel()
        own = channels / "s-space-ag2space"
        own.mkdir()
        (own / ".env").write_text(TOKEN_LINE)
        self._routed(self._row("s-space", "s.space"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = m.main(["x", str(channels), "ag2space", "--room", "!r:s.space"])
        self.assertEqual((rc, out.getvalue().strip()), (0, str(own / ".env")))

    def test_cli_bad_room_flag_is_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(m.main(["x", "c", "ag2space", "--nope", "r"]), 2)

    def _stub_python(self) -> Path:
        stub = self._tmpdir() / "py"
        stub.write_text('#!/bin/sh\nshift\necho "$@"\n')
        stub.chmod(0o755)
        return stub

    def _wrapper(self, *args, **env):
        environ = isolated_child_env()
        environ["CLAUDE_CONFIG_DIR"] = str(self._tmpdir())
        environ.update(env)
        return subprocess.run(["bash", str(REPO / "scripts" / "channel-env.sh"), *args],
                              capture_output=True, text=True, env=environ)

    def test_wrapper_hands_the_room_to_the_resolver(self):
        r = self._wrapper("ag2space", "--room", "!r:dev.s.space", SUTANDO_PY=str(self._stub_python()))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().endswith("/channels ag2space --room !r:dev.s.space"), r.stdout)

    def test_wrapper_without_room_passes_no_flag(self):
        r = self._wrapper("ag2space", SUTANDO_PY=str(self._stub_python()))
        self.assertTrue(r.stdout.strip().endswith("/channels ag2space"), r.stdout)

    def test_wrapper_rejects_a_bare_room_flag(self):
        for args in (("ag2space", "--room"), ("ag2space", "--room", ""), ("ag2space", "--x", "y")):
            r = self._wrapper(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(r.stdout, "")


class TestLaneEnv(unittest.TestCase):
    """The pointers a launcher forwards so a child session resolves lanes as the core does."""

    def test_only_the_pointer_variables_are_named(self):
        env = {"AG2_DEVICE_ENV": "/d/device.env", "SUTANDO_APP_SUPPORT": "/app",
               "REMOTE_TASK_CHANNEL_DIR_S_SPACE": "s-space-ag2space", "REMOTE_TASK_TOKEN": "u|t",
               "AG2_REMOTE_TOKEN": "u|t", "AG2_REMOTE_TOKEN_S_SPACE": "u|t", "REMOTE_TASK_URL": "u",
               "REMOTE_TASK_TOKEN_FILE": "/f", "HOME": "/h"}
        self.assertEqual(m.lane_env(env), {"AG2_DEVICE_ENV": "/d/device.env", "SUTANDO_APP_SUPPORT": "/app",
                                           "REMOTE_TASK_CHANNEL_DIR_S_SPACE": "s-space-ag2space"})

    def test_empty_and_control_character_values_are_left_out(self):
        env = {"AG2_DEVICE_ENV": " ", "SUTANDO_APP_SUPPORT": "/app\nREMOTE_TASK_TOKEN=x",
               "REMOTE_TASK_CHANNEL_DIR_X": "d\0"}
        self.assertEqual(m.lane_env(env), {})

    def test_cli_prints_nul_framed_pairs_and_no_token(self):
        env = isolated_child_env(AG2_DEVICE_ENV="/d/device.env", SUTANDO_APP_SUPPORT="/app",
                                 REMOTE_TASK_TOKEN="u|secret", AG2_REMOTE_TOKEN_S_SPACE="u|secret")
        r = subprocess.run([sys.executable, str(REPO / "src" / "channel_env_resolve.py"), "--lane-env"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "AG2_DEVICE_ENV=/d/device.env\0SUTANDO_APP_SUPPORT=/app\0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
