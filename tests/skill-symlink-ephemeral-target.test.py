"""A skill symlink that resolves INTO a temp dir must warn, not read healthy.

`check_skill_symlinks` modelled four states (linked / unlinked / dangling /
shadowed-by-a-real-dir). A fifth slipped through as healthy: a link that
resolves, but into a scratch worktree. It loads code `git pull` never reaches,
and disappears on the next tmp sweep — at which point it becomes a dangling
link with nothing recording how it got there.

Also pins the inverse: a link to a DIFFERENT DURABLE clone is a supported
layout and must NOT warn. An earlier draft compared against the running
checkout instead, which flagged 57 of 57 links when run from a worktree.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["hc"] = m
    spec.loader.exec_module(m)
    return m


class EphemeralSkillTarget(unittest.TestCase):
    def setUp(self):
        self.hc = _load()

    def _run(self, repo_parent: Path, target_parent: Path):
        """repo lives under repo_parent; the skill link resolves into target_parent."""
        src = repo_parent / "repo" / "skills"
        (src / "alpha").mkdir(parents=True)
        (src / "alpha" / "SKILL.md").write_text("# alpha\n")
        real = target_parent / "elsewhere" / "skills" / "alpha"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("# alpha\n")
        dst = repo_parent / "home" / "skills"
        dst.mkdir(parents=True)
        (dst / "alpha").symlink_to(real)

        self.hc.REPO_DIR = src.parent
        self.hc.claude_home_path = lambda *p: dst.parent.joinpath(*p)
        return self.hc.check_skill_symlinks()

    def test_durable_repo_linking_into_temp_warns(self):
        """The real defect: a durable install whose skill escapes into temp."""
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as durable:
            with tempfile.TemporaryDirectory(dir="/tmp") as ephemeral:
                res = self._run(Path(durable), Path(ephemeral))
                self.assertEqual(res["status"], "warn", res)
                self.assertIn("temp", res["detail"].lower())
                self.assertIn("alpha", res["detail"])

    def test_temp_rooted_install_is_self_consistent(self):
        """A fixture builds its whole install under tempfile. Every correct link
        there is temp-rooted; flagging them made two existing suites fail."""
        with tempfile.TemporaryDirectory(dir="/tmp") as ephemeral:
            e = Path(ephemeral)
            res = self._run(e / "a", e / "b")
            self.assertEqual(res["status"], "ok", res)

    def test_durable_other_clone_is_ok(self):
        """A second durable checkout is a supported layout, not a defect."""
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as durable:
            d = Path(durable)
            res = self._run(d / "a", d / "b")
            self.assertEqual(res["status"], "ok", res)

    def test_ephemeral_roots(self):
        self.assertTrue(self.hc._is_ephemeral("/private/tmp/x/skills/a"))
        self.assertTrue(self.hc._is_ephemeral("/var/folders/ab/cd/T/x"))
        self.assertFalse(self.hc._is_ephemeral(str(Path.home() / "repo" / "skills" / "a")))


if __name__ == "__main__":
    unittest.main()
