#!/usr/bin/env python3
"""An operator's process override must beat a stale env stanza in the config.

The existing Telegram suite passes a pre-resolved string into
`_resolve_proactive_owner_id`, so it cannot see which helper the caller used.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from sutando_config import config_get, config_get_env_first  # noqa: E402

BRIDGE = REPO / "src" / "telegram-bridge.py"


class ProactiveOwnerEnvFirst(unittest.TestCase):
    def test_process_override_beats_a_stale_config_stanza(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sutando.config.local.json").write_text(
                json.dumps({"env": {"SUTANDO_DM_OWNER_ID": "stale-from-config"}}),
                encoding="utf-8")
            prev = os.environ.get("SUTANDO_DM_OWNER_ID")
            os.environ["SUTANDO_DM_OWNER_ID"] = "corrective-from-process"
            try:
                self.assertEqual(
                    config_get_env_first("SUTANDO_DM_OWNER_ID", "", repo_root=root),
                    "corrective-from-process",
                    "env-first must return the operator's process override")
                self.assertEqual(
                    config_get("SUTANDO_DM_OWNER_ID", "", repo_root=root),
                    "stale-from-config",
                    "config-first returns the stale value — this is the wrong helper here")
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_DM_OWNER_ID", None)
                else:
                    os.environ["SUTANDO_DM_OWNER_ID"] = prev

    def test_the_bridge_is_wired_to_the_env_first_helper(self):
        """Pins the caller, which is what the pre-resolved-string suite cannot see."""
        src = BRIDGE.read_text(encoding="utf-8")
        m = re.search(r'(\w+)\("SUTANDO_DM_OWNER_ID"', src)
        self.assertIsNotNone(m, "the proactive-owner lookup vanished from telegram-bridge")
        self.assertEqual(m.group(1), "config_get_env_first",
                         f"telegram-bridge resolves the override with {m.group(1)}(), "
                         "so a stale config stanza would win over the operator")


if __name__ == "__main__":
    unittest.main(verbosity=0)
