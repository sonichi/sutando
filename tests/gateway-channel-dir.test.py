#!/usr/bin/env python3
"""REMOTE_TASK_CHANNEL_DIR contract: a gateway-bridge instance reads its
.env fallback and access.json from $CLAUDE_CONFIG_DIR/channels/<CHANNEL_DIR>/,
default "ag2space". This is what makes a second homeserver instance (e.g.
"dev-ag2space") unable to inherit prod's credentials or tier map.

Loads the SHIPPED loader (src/remote-gateway-bridge.py → canonical ag2-sparrow
module) fresh per case, because CHANNEL_DIR is import-time env.

Run: python3 tests/gateway-channel-dir.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"


import contextlib


@contextlib.contextmanager
def _load(env: dict, name: str):
    """Load the shipped module with `env` applied, keeping it applied for the
    body (CHANNEL_DIR is import-time, but the access path reads env per call)."""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, _SRC)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestChannelDir(unittest.TestCase):
    def test_default_is_ag2space(self):
        with tempfile.TemporaryDirectory() as cfg, _load(
            # AG2_DEVICE_ENV outranks CLAUDE_CONFIG_DIR in _ag2space_access_path,
            # so leaving it ambient points this at the operator's real install.
            {"CLAUDE_CONFIG_DIR": cfg, "REMOTE_TASK_TOKEN": "https://gw|s",
             "AG2_DEVICE_ENV": ""},
            "rgb_chandir_default",
        ) as mod:
            self.assertEqual(mod.CHANNEL_DIR, "ag2space")
            self.assertEqual(
                mod._ag2space_access_path(),
                os.path.join(cfg, "channels", "ag2space", "access.json"),
            )

    def test_override_moves_both_paths(self):
        with tempfile.TemporaryDirectory() as cfg:
            # Lay a token .env ONLY under the override dir; the fallback reader
            # must find it there (and would not under ag2space/).
            d = os.path.join(cfg, "channels", "dev-ag2space")
            os.makedirs(d)
            with open(os.path.join(d, ".env"), "w") as f:
                f.write("REMOTE_TASK_TOKEN=https://dev-gw|devsecret\n")
            with _load(
                {
                    "CLAUDE_CONFIG_DIR": cfg,
                    "REMOTE_TASK_CHANNEL_DIR": "dev-ag2space",
                    "REMOTE_TASK_TOKEN": "",
                    "AG2_DEVICE_ENV": "",
                },
                "rgb_chandir_override",
            ) as mod:
                self.assertEqual(mod.CHANNEL_DIR, "dev-ag2space")
                self.assertEqual(
                    mod._ag2space_access_path(),
                    os.path.join(cfg, "channels", "dev-ag2space", "access.json"),
                )
                raw, _url, _tf = mod._token_from_ag2space_env()
                self.assertIn("devsecret", raw or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
