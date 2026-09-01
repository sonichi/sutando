#!/usr/bin/env python3
"""Regression test: a DANGLING skill symlink must not count as linked (#2213).

The original condition was:

    if not dst.exists() and not dst.is_symlink():

`exists()` follows the link, so it is False for a dangling one — but
`is_symlink()` is True, making the whole condition False. A dangling entry was
therefore reported as linked. Claude Code cannot load such a skill, so it was
simultaneously invisible and green: the exact "reports healthy while doing
nothing" shape.

Measured on a live host before the fix: 14 dangling links out of 67 entries,
health-check reporting "all 51 skills linked".

Second layer: even once detected, `--fix` could not repair it. The dangling
link occupies the name, so `symlink_to()` raises FileExistsError. Verified
empirically before writing this.

Run: python3 tests/health-check-dangling-symlinks.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "health_check_symlink_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestDanglingSkillSymlinks(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self._tmp = tempfile.TemporaryDirectory()
        # SPACES ARE DELIBERATE, and belong in the shared fixture rather than in
        # one bespoke test. The remedy this suite executes is a SHELL command
        # built from these paths, so an unquoted placeholder word-splits and the
        # repair silently does nothing (qingyun-wu + bassilkhilo-ag2, #2660).
        # A space-free fixture passes against the broken command, which is
        # exactly how the defect survived a test that already ran the remedy.
        # Widening the axis here means every case below carries the property.
        root = Path(self._tmp.name) / "sp ace root"
        root.mkdir()
        self.repo = root / "repo"
        self.src = self.repo / "skills"
        self.dst = root / "home" / ".claude" / "skills"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.hc.REPO_DIR = self.repo
        # check_skill_symlinks resolves its destination via claude_home_path()
        # (CLAUDE_CONFIG_DIR-aware); point it at the sandbox claude-home.
        self._home = root / "home"
        self.hc.claude_home_path = (
            lambda *sub: self._home.joinpath(".claude", *sub)
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _skill(self, name: str) -> Path:
        d = self.src / name
        d.mkdir()
        (d / "SKILL.md").write_text("# x\n")
        return d

    def test_dangling_link_is_not_reported_as_linked(self):
        """The bug. Before the fix this returned ok/'all 1 skills linked'."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "gone-elsewhere")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("dangling", r["detail"])
        self.assertEqual(r["_broken"], ["alpha"])

    def test_real_directory_is_not_reported_as_linked(self):
        """The fourth state. A real dir where a symlink belongs fell through
        BOTH branches into "healthy": `is_symlink()` False, `exists()` True.

        It is not a link -- it is a copy, so `git pull` never reaches it and the
        running skill diverges silently. Observed live 2026-08-05: `x-twitter`
        had been a real dir since Jul 17, 11 days behind the repo, while the
        probe reported "all 60 skills linked".
        """
        self._skill("alpha")
        real = self.dst / "alpha"
        real.mkdir()
        (real / "SKILL.md").write_text("# stale copy\n")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertEqual(r["_shadowed"], ["alpha"])
        self.assertIn("real dir", r["detail"])

    def test_the_advertised_remedy_ACTUALLY_REPAIRS_the_state(self):
        """Run the remedy the warning prints. Not a string match — the command
        is extracted from the detail and EXECUTED.

        The first version of this warning said "`ln -sfn` to re-track", which
        does not repair it: with the real directory still present, macOS `ln`
        treats the destination as a target DIRECTORY and creates a nested
        `<dst>/<name>/<name>` symlink while the real dir stays. The operator
        follows the advice, sees no error, and is still broken
        (john-the-dev, #2660). A wording-only assertion would not have caught
        that, so this one runs it.
        """
        import re
        import subprocess

        self._skill("alpha")
        real = self.dst / "alpha"
        real.mkdir()
        (real / "SKILL.md").write_text("# local edits\n")

        detail = self.hc.check_skill_symlinks()["detail"]

        # The remedy is the backticked command in the warning. Extract it rather
        # than hardcoding, so a future edit to the text is what gets tested.
        cmds = re.findall(r"`([^`]+)`", detail)
        remedy = next((c for c in cmds if "ln -s" in c), None)
        self.assertIsNotNone(remedy, f"no remedy command in the warning: {detail}")
        self.assertNotIn("ln -sfn", remedy,
                         "the warning advertises `ln -sfn`, which does NOT repair a real dir")

        concrete = (remedy.replace("<dst>", str(self.dst))
                          .replace("<src>", str(self.src))
                          .replace("<name>", "alpha"))
        subprocess.run(["bash", "-c", concrete], check=True)

        self.assertTrue((self.dst / "alpha").is_symlink(),
                        "the advertised remedy did not produce a symlink")
        self.assertFalse((self.dst / "alpha" / "alpha").exists(),
                         "the remedy created a NESTED link — the `ln -sfn` failure mode")
        self.assertTrue((self.dst.parent / "alpha.skill-backup" / "SKILL.md").exists(),
                        "the remedy did not preserve the local edits it moved aside")
        self.assertFalse((self.dst / "alpha.skill-backup").exists(),
                         "the backup landed INSIDE <dst> — the skill loader registers "
                         "every directory there, so it loads as a phantom duplicate skill")

    def test_every_path_in_the_remedy_is_QUOTED(self):
        """Pin the property, not just the fixture.

        The suite's paths now contain spaces, so an unquoted remedy fails when
        executed. But a later refactor could quietly de-space the fixture and
        this class would go green against a broken command again. This asserts
        the shape directly: every complete path in the emitted command is
        wrapped in double quotes.

        The failure it guards is silent, which is what makes it worth a second
        test: unquoted, `mv` exits 1 with "<tail>.skill-backup is not a
        directory", the real directory stays, and NEITHER the symlink nor the
        backup is created -- the operator's only recovery path reports success
        while doing nothing.
        """
        import re

        self._skill("alpha")
        (self.dst / "alpha").mkdir()

        detail = self.hc.check_skill_symlinks()["detail"]
        remedy = next(c for c in re.findall(r"`([^`]+)`", detail) if "ln -s" in c)

        bare = re.findall(r'(?<!")<(?:src|dst)>/<name>[^\s"]*', remedy)
        self.assertEqual(
            bare, [],
            f"unquoted path(s) {bare} in the remedy -- a path with a space "
            f"word-splits and the repair silently no-ops: {remedy}",
        )
        for placeholder in ("<dst>/<name>", "<src>/<name>", "<dst>/../<name>.skill-backup"):
            self.assertIn(f'"{placeholder}"', remedy,
                          f"{placeholder} must be quoted in: {remedy}")

    def test_ln_sfn_alone_does_NOT_repair_it(self):
        """The control that justifies the remedy above. If this ever starts
        passing, macOS `ln` changed and the warning can be simplified."""
        import subprocess
        self._skill("alpha")
        real = self.dst / "alpha"
        real.mkdir()
        (real / "SKILL.md").write_text("# local\n")
        # Quoted for the same reason the remedy is: the fixture paths contain
        # spaces, and this control must fail on `ln -sfn` SEMANTICS (the nested
        # link), not on word-splitting -- otherwise it would "pass" for the
        # wrong reason and stop justifying the longer remedy.
        subprocess.run(["bash", "-c",
                        f'ln -sfn "{self.src}/alpha" "{self.dst}/alpha"'], check=True)
        self.assertFalse((self.dst / "alpha").is_symlink(),
                         "ln -sfn repaired it — the warning's caveat is now obsolete")
        self.assertTrue((self.dst / "alpha" / "alpha").is_symlink(),
                        "expected the nested-link failure mode")

    def test_shadowed_is_reported_but_NOT_auto_fixed(self):
        """--fix must not clobber it: a real dir may be an intentional local
        install someone is editing. Same reason refresh-skill.sh refuses."""
        self._skill("alpha")
        real = self.dst / "alpha"
        real.mkdir()
        (real / "SKILL.md").write_text("# local edits\n")
        (real / "LOCAL_ONLY.txt").write_text("do not lose me\n")
        r = self.hc.check_skill_symlinks()
        self.hc.fix_skill_symlinks(r)
        self.assertFalse((self.dst / "alpha").is_symlink(),
                         "--fix replaced a real dir with a symlink")
        self.assertTrue((real / "LOCAL_ONLY.txt").exists(),
                        "--fix destroyed local-only content")

    def test_a_healthy_symlink_is_NOT_flagged_as_shadowed(self):
        """Control: without this, a fix that flags everything would pass above."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "alpha")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertEqual(r.get("_shadowed", []), [])

    def test_healthy_link_still_ok(self):
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "alpha")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_missing_and_dangling_are_reported_separately(self):
        """They need different remediation — one needs unlinking first — so
        collapsing them into one bucket would make --fix wrong for half."""
        self._skill("alpha")
        self._skill("beta")
        (self.dst / "alpha").symlink_to(self.src / "gone")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["_broken"], ["alpha"])
        self.assertEqual(r["_unlinked"], ["beta"])

    def test_dangling_link_for_a_skill_not_in_this_repo_is_still_flagged(self):
        """The repo-driven loop only walks repo skills, so a dead entry from
        another checkout would be missed entirely."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "alpha")
        (self.dst / "from-another-clone").symlink_to(self.src / "nowhere")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "warn")
        self.assertEqual(r["_orphaned"], ["from-another-clone"])

    def test_fix_repairs_a_dangling_link(self):
        """--fix must unlink first; symlink_to() alone raises FileExistsError,
        so before this the fix could not repair the case it existed for."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "gone")
        check = self.hc.check_skill_symlinks()
        res = self.hc.fix_skill_symlinks(check)
        self.assertEqual(res["status"], "ok", res["detail"])
        self.assertTrue((self.dst / "alpha").exists())
        self.assertEqual((self.dst / "alpha").resolve(), (self.src / "alpha").resolve())

    def test_fix_never_removes_a_real_directory(self):
        """The unlink guard must be is_symlink()-gated — a health check must
        not delete real content under any circumstances."""
        self._skill("alpha")
        real = self.dst / "alpha"
        real.mkdir()
        (real / "keep.txt").write_text("important")
        check = self.hc.check_skill_symlinks()
        self.hc.fix_skill_symlinks(check)
        self.assertTrue((real / "keep.txt").exists(), "real dir must survive --fix")


    def test_unreadable_destination_dir_does_not_crash_the_tick(self):
        """The destination sweep walks a directory that can disappear or become
        unreadable between the earlier exists() check and iteration (races,
        permissions, an unmounted volume). A health check must degrade to
        "no orphans found" rather than raise — one bad tick would take down
        every check after it, which is strictly worse than under-reporting."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "alpha")
        real_iterdir = Path.iterdir

        def boom(self_path):
            # Only the destination sweep should blow up; the repo scan above it
            # must still run, otherwise this proves nothing about the guard.
            if self_path == self.dst:
                raise OSError("permission denied")
            return real_iterdir(self_path)

        with mock.patch.object(Path, "iterdir", boom):
            r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertEqual(r.get("_orphaned", []), [])

    def test_skillmd_less_dir_is_not_reported_unlinked(self):
        """Manifest-loaded / scripts-only skills have no SKILL.md and are
        correctly never symlinked by skills/install.sh — the probe must apply
        the installer's own filter instead of warning about them."""
        d = self.src / "manifest-only"
        d.mkdir()
        (d / "manifest.json").write_text("{}\n")
        self._skill("real-skill")  # has SKILL.md, genuinely unlinked
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("real-skill", r["detail"])
        self.assertNotIn("manifest-only", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
