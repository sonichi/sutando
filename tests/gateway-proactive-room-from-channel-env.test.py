#!/usr/bin/env python3
"""REMOTE_PROACTIVE_ROOM must come from the channel .env even when the token
does not — the .env is the deployment's config file, not only a token store."""
from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"

ROOM = "!ownerdm:example.org"


@contextlib.contextmanager
def _load(env: dict, name: str):
    """Load the shipped module with `env` applied. PROACTIVE_ROOM is import-time,
    so each case needs its own module instance."""
    old = {k: os.environ.get(k) for k in env}
    # A None value means "unset it for this case" — os.environ rejects None.
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, _SRC)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(name, None)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _channel_env(body: str):
    """A $CLAUDE_CONFIG_DIR whose channels/ag2space/.env holds `body`."""
    with tempfile.TemporaryDirectory() as cfg:
        d = Path(cfg) / "channels" / "ag2space"
        d.mkdir(parents=True)
        (d / ".env").write_text(body, encoding="utf-8")
        yield cfg


class ProactiveRoomFromChannelEnv(unittest.TestCase):
    def test_room_is_read_when_the_token_is_already_in_the_env(self):
        # The live-host case: a launcher exports the token, so the .env is never
        # consulted for the token — and the room went unread with it.
        with _channel_env(
            f"REMOTE_TASK_TOKEN=https://gw|s\nREMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, _load(
            {
                "CLAUDE_CONFIG_DIR": cfg,
                "REMOTE_TASK_TOKEN": "https://gw|from-env",
                "REMOTE_PROACTIVE_ROOM": None,
                "AG2_DEVICE_ENV": None,
            },
            "rgb_proactive_token_in_env",
        ) as mod:
            self.assertEqual(mod.PROACTIVE_ROOM, ROOM)

    def test_room_is_read_when_the_token_comes_from_the_file(self):
        with _channel_env(
            f"REMOTE_TASK_TOKEN=https://gw|s\nREMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, _load(
            {
                "CLAUDE_CONFIG_DIR": cfg,
                "REMOTE_TASK_TOKEN": None,
                "REMOTE_PROACTIVE_ROOM": None,
                "AG2_DEVICE_ENV": None,
            },
            "rgb_proactive_token_from_file",
        ) as mod:
            self.assertEqual(mod.PROACTIVE_ROOM, ROOM)

    def test_a_real_env_var_still_wins_over_the_file(self):
        with _channel_env(
            f"REMOTE_TASK_TOKEN=https://gw|s\nREMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, _load(
            {
                "CLAUDE_CONFIG_DIR": cfg,
                "REMOTE_TASK_TOKEN": "https://gw|from-env",
                "REMOTE_PROACTIVE_ROOM": "!explicit:example.org",
                "AG2_DEVICE_ENV": None,
            },
            "rgb_proactive_env_wins",
        ) as mod:
            self.assertEqual(mod.PROACTIVE_ROOM, "!explicit:example.org")

    def test_a_secondary_gateway_blanking_the_room_keeps_it_blank(self):
        # startup.sh launches named instances with REMOTE_PROACTIVE_ROOM= so
        # nudges stay on the primary. An empty export must not fall through to
        # the file, or every secondary re-acquires the owner's DM room.
        with _channel_env(
            f"REMOTE_TASK_TOKEN=https://gw|s\nREMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, _load(
            {
                "CLAUDE_CONFIG_DIR": cfg,
                "REMOTE_TASK_TOKEN": "https://gw|from-env",
                "REMOTE_PROACTIVE_ROOM": "",
                "AG2_DEVICE_ENV": None,
            },
            "rgb_proactive_blanked",
        ) as mod:
            self.assertEqual(mod.PROACTIVE_ROOM, "")

    def test_no_room_anywhere_stays_unset(self):
        with _channel_env("REMOTE_TASK_TOKEN=https://gw|s\n") as cfg, _load(
            {
                "CLAUDE_CONFIG_DIR": cfg,
                "REMOTE_TASK_TOKEN": "https://gw|from-env",
                "REMOTE_PROACTIVE_ROOM": None,
                "AG2_DEVICE_ENV": None,
            },
            "rgb_proactive_absent",
        ) as mod:
            self.assertEqual(mod.PROACTIVE_ROOM, "")

    def test_a_missing_config_dir_is_not_an_import_error(self):
        # The fill runs at import on every launch, including ones with no
        # channel dir at all; a raise here takes the whole bridge down.
        with tempfile.TemporaryDirectory() as cfg, _load(
            {
                "CLAUDE_CONFIG_DIR": cfg,
                "REMOTE_TASK_TOKEN": "https://gw|from-env",
                "REMOTE_PROACTIVE_ROOM": None,
                "AG2_DEVICE_ENV": None,
            },
            "rgb_proactive_no_dir",
        ) as mod:
            self.assertEqual(mod.PROACTIVE_ROOM, "")

    def test_a_candidate_without_the_room_does_not_shadow_the_next(self):
        # AG2_DEVICE_ENV wins for the TOKEN, and the desktop launcher writes it
        # with only the token in it. If holding the token also claimed the room,
        # the channel .env's room would be unreachable on exactly that launcher.
        with _channel_env(
            f"REMOTE_TASK_TOKEN=https://gw|s\nREMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, tempfile.TemporaryDirectory() as devdir:
            dev = Path(devdir) / ".env"
            dev.write_text("REMOTE_TASK_TOKEN=https://gw|dev\n", encoding="utf-8")
            with _load(
                {
                    "CLAUDE_CONFIG_DIR": cfg,
                    "AG2_DEVICE_ENV": str(dev),
                    "REMOTE_TASK_TOKEN": None,
                    "REMOTE_PROACTIVE_ROOM": None,
                },
                "rgb_proactive_no_shadow",
            ) as mod:
                self.assertEqual(mod.PROACTIVE_ROOM, ROOM)

    def test_a_present_but_empty_value_wins_over_a_later_candidate(self):
        # Symmetric with the env layer: a blank the operator wrote is a decision.
        # Falling through would re-acquire the room the higher file disabled.
        with _channel_env(
            f"REMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, tempfile.TemporaryDirectory() as devdir:
            dev = Path(devdir) / ".env"
            dev.write_text(
                "REMOTE_TASK_TOKEN=https://gw|dev\nREMOTE_PROACTIVE_ROOM=\n",
                encoding="utf-8",
            )
            with _load(
                {
                    "CLAUDE_CONFIG_DIR": cfg,
                    "AG2_DEVICE_ENV": str(dev),
                    "REMOTE_TASK_TOKEN": None,
                    "REMOTE_PROACTIVE_ROOM": None,
                },
                "rgb_proactive_file_blank",
            ) as mod:
                self.assertEqual(mod.PROACTIVE_ROOM, "")

    def test_an_undecodable_candidate_does_not_shadow_the_next(self):
        # Reading every candidate up front widened the set of files that must
        # survive parsing; UnicodeDecodeError is not caught by `except OSError`.
        with _channel_env(
            f"REMOTE_TASK_TOKEN=https://gw|s\nREMOTE_PROACTIVE_ROOM={ROOM}\n"
        ) as cfg, tempfile.TemporaryDirectory() as devdir:
            dev = Path(devdir) / ".env"
            dev.write_bytes(b"REMOTE_TASK_URL=https://x.example\xff\n")
            with _load(
                {
                    "CLAUDE_CONFIG_DIR": cfg,
                    "AG2_DEVICE_ENV": str(dev),
                    "REMOTE_TASK_TOKEN": None,
                    "REMOTE_PROACTIVE_ROOM": None,
                },
                "rgb_proactive_undecodable_first",
            ) as mod:
                self.assertEqual(mod.PROACTIVE_ROOM, ROOM)

    def test_an_undecodable_later_candidate_is_not_an_import_error(self):
        # The bridge that will not import delivers no proactive body at all —
        # this PR's own symptom, reached by a different route.
        with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as devdir:
            d = Path(cfg) / "channels" / "ag2space"
            d.mkdir(parents=True)
            (d / ".env").write_bytes(b"REMOTE_TASK_URL=https://x.example\xff\n")
            dev = Path(devdir) / ".env"
            dev.write_text("REMOTE_TASK_TOKEN=https://gw|dev\n", encoding="utf-8")
            with _load(
                {
                    "CLAUDE_CONFIG_DIR": cfg,
                    "AG2_DEVICE_ENV": str(dev),
                    "REMOTE_TASK_TOKEN": None,
                    "REMOTE_PROACTIVE_ROOM": None,
                },
                "rgb_proactive_undecodable_last",
            ) as mod:
                self.assertEqual(mod.PROACTIVE_ROOM, "")

    def test_the_instance_channel_dir_is_honored(self):
        # A named instance reads its OWN channels/<dir>/.env, so it cannot pick
        # up the primary's room.
        with tempfile.TemporaryDirectory() as cfg:
            for name, room in (("ag2space", ROOM), ("dev-ag2space", "!dev:example.org")):
                d = Path(cfg) / "channels" / name
                d.mkdir(parents=True)
                (d / ".env").write_text(
                    f"REMOTE_PROACTIVE_ROOM={room}\n", encoding="utf-8"
                )
            with _load(
                {
                    "CLAUDE_CONFIG_DIR": cfg,
                    "REMOTE_TASK_CHANNEL_DIR": "dev-ag2space",
                    "REMOTE_TASK_TOKEN": "https://gw|from-env",
                    "REMOTE_PROACTIVE_ROOM": None,
                    "AG2_DEVICE_ENV": None,
                },
                "rgb_proactive_instance_dir",
            ) as mod:
                self.assertEqual(mod.PROACTIVE_ROOM, "!dev:example.org")


if __name__ == "__main__":
    unittest.main(verbosity=2)
