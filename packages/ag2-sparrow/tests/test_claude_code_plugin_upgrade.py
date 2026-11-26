"""An upgrade moves claude-local's session onto the plugin the new ag2-sparrow ships.

2026-11-25: after the app moved every agent onto 0.12.4, two claude-local sessions still loaded the
ag2space-inbox plugin from the 0.8.3 environment: install carried the recorded PLUGIN_DIR, and that older
environment was still on disk.

Run:
    python3 packages/ag2-sparrow/tests/test_claude_code_plugin_upgrade.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow.adapters.claude_code import cli, hold, shape  # noqa: E402


def plugin_at(path: Path) -> Path:
    (path / ".claude-plugin").mkdir(parents=True)
    (path / ".claude-plugin" / "plugin.json").write_text("{}")
    return path


class PluginFollowsTheUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-upgrade-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        site = self.tmp / "venvs" / "0.8.3-bcc3e498e49a" / "lib" / "python3.12" / "site-packages"
        self.old = plugin_at(site / "ag2_sparrow" / "adapters" / "claude_code" / "ag2space-inbox")
        self.own = plugin_at(self.tmp / "my-plugin")
        self.carried = dict((key, conv) for key, _, conv in cli.RECORDED)["PLUGIN_DIR"]

    def test_an_older_packages_plugin_is_replaced_by_this_ones(self):
        self.assertTrue(shape.shipped_plugin(self.old))
        self.assertTrue(shape.shipped_plugin(shape.PLUGIN))
        self.assertFalse(shape.shipped_plugin(self.own))
        self.assertEqual(shape.resolve_plugin_dir(str(self.old)), (shape.PLUGIN, False), "no 'missing' warning")
        self.assertEqual(self.carried(str(self.old)), "", "a repair install records this package's plugin")
        argv = hold.claude_argv({"CLAUDE": "claude", "PLUGIN_DIR": str(self.old)}, "sid")
        self.assertEqual(argv[1:3], ["--plugin-dir", str(shape.PLUGIN)], "the session starts on the new plugin")

    def test_a_plugin_dir_the_owner_chose_is_kept_and_a_missing_one_falls_back(self):
        self.assertEqual(shape.resolve_plugin_dir(str(self.own)), (self.own, False))
        self.assertEqual(self.carried(str(self.own)), str(self.own))
        self.assertEqual(shape.resolve_plugin_dir(str(self.tmp / "gone")), (shape.PLUGIN, True))
        self.assertEqual(self.carried(str(self.tmp / "gone")), "")
        self.assertEqual(self.carried(""), "")


if __name__ == "__main__":
    unittest.main()
