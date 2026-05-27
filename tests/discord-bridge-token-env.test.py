#!/usr/bin/env python3
"""
Test that DISCORD_BOT_TOKEN env var takes precedence over .env file.
Port of sonichi/sutando#1050.

Each test case launches a subprocess with controlled env to check TOKEN.
Run: python3 tests/discord-bridge-token-env.test.py
"""

from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = str(REPO / "src" / "discord-bridge.py")


def _run_probe(env_token: str = "", dot_env_token: str = ""):
    """Run a sub-process that loads discord-bridge.py and prints TOKEN=<value>."""
    probe = (
        "import sys, types, os, importlib.util\n"
        "from pathlib import Path\n"
        "_ds = types.ModuleType('discord')\n"
        "class _I:\n"
        "    @classmethod\n"
        "    def default(cls):\n"
        "        i = cls(); i.message_content = False; i.members = False; return i\n"
        "class _C:\n"
        "    def __init__(self, *a, **kw): self.user = None\n"
        "    def event(self, fn): return fn\n"
        "    def get_channel(self, _): return None\n"
        "_ds.Intents = _I; _ds.Client = _C\n"
        "_ds.AllowedMentions = type('AM', (), {'__init__': lambda s, **k: None})\n"
        "_ds.MessageReference = type('MR', (), {'__init__': lambda s, **k: None})\n"
        "_ds.MessageType = types.SimpleNamespace(default=0, reply=1)\n"
        "_ds.File = type('File', (), {'__init__': lambda s, fp, filename=None: None})\n"
        "_ds.DMChannel = type('DMC', (), {})\n"
        "sys.modules['discord'] = _ds\n"
        "spec = importlib.util.spec_from_file_location('bridge', r'" + BRIDGE + "')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "try:\n"
        "    spec.loader.exec_module(mod)\n"
        "    print('TOKEN=' + getattr(mod, 'TOKEN', ''))\n"
        "except SystemExit:\n"
        "    print('TOKEN=EXITED')\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_home = Path(tmp)
        channels_dir = tmp_home / ".claude" / "channels" / "discord"
        if dot_env_token:
            channels_dir.mkdir(parents=True, exist_ok=True)
            (channels_dir / ".env").write_text(f"DISCORD_BOT_TOKEN={dot_env_token}\n")

        env = {**os.environ, "HOME": str(tmp_home)}
        if env_token:
            env["DISCORD_BOT_TOKEN"] = env_token
        else:
            env.pop("DISCORD_BOT_TOKEN", None)

        result = subprocess.run(
            [sys.executable, "-c", probe],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=15,
        )
        for line in result.stdout.splitlines():
            if line.startswith("TOKEN="):
                val = line[len("TOKEN="):]
                return None if val == "EXITED" else val
        return None


class TestDiscordBridgeTokenEnv(unittest.TestCase):

    def test_env_var_takes_precedence_over_dot_env(self):
        """When env var is set, it wins over .env file."""
        token = _run_probe(env_token="env-tok-123", dot_env_token="file-tok-456")
        self.assertEqual(token, "env-tok-123")

    def test_dot_env_fallback_when_env_var_absent(self):
        """When env var is absent, .env file token is used."""
        token = _run_probe(env_token="", dot_env_token="file-tok-456")
        self.assertEqual(token, "file-tok-456")

    def test_exit_when_both_absent(self):
        """When neither source has a token, bridge exits."""
        token = _run_probe(env_token="", dot_env_token="")
        self.assertIsNone(token)

    def test_env_var_alone_no_dot_env_file(self):
        """Env var works even without a .env file."""
        token = _run_probe(env_token="only-env-tok", dot_env_token="")
        self.assertEqual(token, "only-env-tok")


if __name__ == "__main__":
    unittest.main()
