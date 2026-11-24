"""install --replace-install: an updated app's new environment takes a running agent's job over.

The session an agent runs was started from the environment it was connected with. Every install from
another copy of the package was refused ("... of another install"), so an app update could never move a
connected agent onto the ag2-sparrow it ships: all eight local agents of a test machine were refused. Run:
    python3 packages/ag2-sparrow/tests/test_install_replaces_other_install.py
"""
import argparse
import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import connect_common as cm  # noqa: E402

claude, codex, agy, openclaw = (importlib.import_module("ag2_sparrow.adapters." + name) for name in (
    "claude_code.cli", "codex.connect_cli", "antigravity.cli", "openclaw.cli"))

ROOT = "/agents/helper"
OLD = "ag2-sparrow@/app/sparrow/venvs/0.11.0-aaaa/lib/python3/site-packages/ag2_sparrow"
NEW = "ag2-sparrow@/app/sparrow/venvs/0.12.0-bbbb/lib/python3/site-packages/ag2_sparrow"


class ReplaceInstall(unittest.TestCase):
    def setUp(self):
        self.addCleanup(cm.take_over, argparse.Namespace())

    def held(self, shape, root=ROOT, adapter=None, install=OLD, live=True, recorded=ROOT):
        """_managed_here's word on `root`, held by `adapter`'s session of `install` while the job records `recorded`."""
        rec = {"live": live, "adapter": adapter or shape.name, "pid": 4242, "install_id": install}
        with mock.patch.object(cm, "owned_config", return_value=({"ROOT": recorded}, "")), \
                mock.patch.object(cm, "lock_holder", return_value=rec), \
                mock.patch.object(cm, "install_id", return_value=NEW):
            return cm._managed_here(shape, root)

    def test_a_session_of_another_install_is_refused_unless_the_install_says_replace(self):
        shape = claude.LOCAL
        self.assertIn("of another install", self.held(shape))
        cm.take_over(argparse.Namespace(replace_install=True))
        self.assertEqual(self.held(shape), "", "the job's own session, started from the environment before")
        # The next invocation without the option refuses again.
        cm.take_over(argparse.Namespace())
        self.assertIn("of another install", self.held(shape))

    def test_replace_takes_over_only_this_adapters_live_session_on_the_jobs_own_root(self):
        shape = claude.LOCAL
        cm.take_over(argparse.Namespace(replace_install=True))
        self.assertIn("is held by codex-local pid 4242", self.held(shape, adapter="codex-local"))
        self.assertIn("a holder with no live record", self.held(shape, live=False))
        self.assertIn("is active for /agents/other", self.held(shape, recorded="/agents/other"))
        self.assertEqual(self.held(shape, install=NEW), "", "its own install's session, as before")

    def test_every_local_adapters_install_takes_the_option_and_reads_it(self):
        for mod in (claude, codex, agy, openclaw):
            with self.subTest(adapter=mod.COMMAND):
                # Parsed (argparse would exit on an unknown option) and handed to the shared reader first.
                with mock.patch.object(cm, "take_over", side_effect=RuntimeError("read")) as read:
                    with self.assertRaises(RuntimeError):
                        mod.main(["install", "--agent", "@helper:s.example", "--replace-install"])
                self.assertIs(read.call_args.args[0].replace_install, True)


if __name__ == "__main__":
    unittest.main()
