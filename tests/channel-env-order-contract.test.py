#!/usr/bin/env python3
"""The two readers of the ag2space channel credential agree on which file wins.

`scripts/channel-env.sh` (via src/channel_env_resolve.py) tells every task
which file to source; the gateway bridge's `_channel_env_candidates` picks the
file it connects with. The bridge is package-canonical and not vendored from
src/, so it cannot delegate yet; this test fails if the two orders diverge.

Scope is the layouts both readers support: `$AG2_DEVICE_ENV`, then
`$CLAUDE_CONFIG_DIR/channels/ag2space/.env`. Sibling `*.env` files and the
containment rule are the resolver's alone.

Run: python3 tests/channel-env-order-contract.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

_ISOLATE = ("AG2_DEVICE_ENV", "CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "SUTANDO_APP_SUPPORT",
            "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_TASK_URL", "AG2_REMOTE_URL",
            "REMOTE_TASK_CHANNEL_DIR", "REMOTE_MEDIA_MARKER")
_SAVED = {k: os.environ.get(k) for k in _ISOLATE}
for _k in _ISOLATE:
    os.environ.pop(_k, None)
_SCRATCH = Path(tempfile.mkdtemp(prefix="channel-env-contract-"))
os.environ["CLAUDE_CONFIG_DIR"] = str(_SCRATCH / "import-ccd")
for _kind in ("TASK", "RESULT", "STATE"):
    os.environ[f"AGENT_CONNECT_{_kind}_DIR"] = str(_SCRATCH / _kind.lower())

import channel_env_resolve as resolver  # noqa: E402
from ag2_sparrow import remote_gateway_bridge as bridge  # noqa: E402

DEVICE_TOKEN = "fake-device-token"
TREE_TOKEN = "fake-tree-token"


class OrderContract(unittest.TestCase):
    def setUp(self):
        for k in ("AG2_DEVICE_ENV", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(k, None)
        self.root = Path(tempfile.mkdtemp(dir=_SCRATCH))
        self.cfg = self.root / "ccd"
        self.tree_env = self.cfg / "channels" / "ag2space" / ".env"
        self.device_env = self.root / "space.ag2.app" / "channels" / "ag2space" / ".env"

    def _write(self, path: Path, token: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'REMOTE_TASK_TOKEN="{token}"\n')

    def _picks(self) -> tuple[str, str]:
        """(file channel-env.sh prints, file the bridge loads its token from)."""
        env = dict(os.environ)
        r = subprocess.run(["bash", str(REPO / "scripts" / "channel-env.sh"), "ag2space"],
                           capture_output=True, text=True, env=env)
        _tok, _url, bridge_path = bridge._token_from_ag2space_env()
        return r.stdout.strip(), bridge_path or ""

    def _agree(self, expected: Path | None) -> None:
        shell, gw = self._picks()
        want = str(expected) if expected else ""
        self.assertEqual(shell, want, "channel-env.sh pick")
        self.assertEqual(gw, want, "bridge pick")

    def test_desktop_layout_only_the_launcher_file_has_the_token(self):
        self._write(self.device_env, DEVICE_TOKEN)
        os.environ["AG2_DEVICE_ENV"] = str(self.device_env)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self._agree(self.device_env)

    def test_launcher_file_outranks_the_config_dir(self):
        self._write(self.device_env, DEVICE_TOKEN)
        self._write(self.tree_env, TREE_TOKEN)
        os.environ["AG2_DEVICE_ENV"] = str(self.device_env)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self._agree(self.device_env)

    def test_blank_launcher_file_falls_through_to_the_config_dir(self):
        self.device_env.parent.mkdir(parents=True)
        self.device_env.write_text("REMOTE_TASK_TOKEN=\n")
        self._write(self.tree_env, TREE_TOKEN)
        os.environ["AG2_DEVICE_ENV"] = str(self.device_env)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self._agree(self.tree_env)

    def test_unset_launcher_variable_uses_the_config_dir(self):
        self._write(self.tree_env, TREE_TOKEN)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self._agree(self.tree_env)

    def test_missing_launcher_file_uses_the_config_dir(self):
        self._write(self.tree_env, TREE_TOKEN)
        os.environ["AG2_DEVICE_ENV"] = str(self.root / "absent.env")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self._agree(self.tree_env)

    def test_both_readers_share_the_launcher_variable_name(self):
        self.assertEqual(resolver.DEVICE_ENV_VAR, "AG2_DEVICE_ENV")
        self.assertEqual(bridge.CHANNEL_DIR, resolver.DEVICE_ENV_SOURCE)


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
