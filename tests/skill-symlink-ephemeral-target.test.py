"""A skill symlink resolving INTO a temp dir must warn; one resolving into a
different DURABLE clone must not — that layout is supported.
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

    def _inject_classifier(self, temp_subtree: Path):
        """Call `temp_subtree` ephemeral and nothing else.

        A real DURABLE path would have to be written outside a temp root."""
        root = str(temp_subtree.resolve())
        self.hc._is_ephemeral = lambda t: os.path.realpath(t) == root or \
            os.path.realpath(t).startswith(root + os.sep)

    def test_durable_repo_linking_into_temp_warns(self):
        """The real defect: a durable install whose skill escapes into temp."""
        with tempfile.TemporaryDirectory() as td:
            durable, ephemeral = Path(td) / "durable", Path(td) / "ephemeral"
            durable.mkdir(); ephemeral.mkdir()
            self._inject_classifier(ephemeral)
            res = self._run(durable, ephemeral)
            self.assertEqual(res["status"], "warn", res)
            self.assertIn("temp", res["detail"].lower())
            self.assertIn("alpha", res["detail"])

    def test_temp_rooted_install_is_self_consistent(self):
        """A fixture builds its whole install under tempfile, so every correct link
        there is temp-rooted."""
        with tempfile.TemporaryDirectory() as td:
            e = Path(td) / "ephemeral"
            e.mkdir()
            self._inject_classifier(e)
            res = self._run(e / "a", e / "b")
            self.assertEqual(res["status"], "ok", res)

    def test_durable_other_clone_is_ok(self):
        """A second durable checkout is a supported layout, not a defect."""
        with tempfile.TemporaryDirectory() as td:
            d, unrelated = Path(td) / "durable", Path(td) / "ephemeral"
            d.mkdir(); unrelated.mkdir()
            self._inject_classifier(unrelated)
            res = self._run(d / "a", d / "b")
            self.assertEqual(res["status"], "ok", res)

    def test_ephemeral_roots(self):
        """Fixtures are DERIVED from the platform's own roots. Naming `/private/tmp`
        passes on macOS and fails on Linux, where realpath('/tmp') is '/tmp'."""
        for root in self.hc._ephemeral_roots():
            self.assertTrue(self.hc._is_ephemeral(f"{root}/x/skills/a"), root)
        self.assertFalse(self.hc._is_ephemeral(str(Path.home() / "repo" / "skills" / "a")))

    def test_a_root_itself_is_ephemeral(self):
        """A link pointing AT the root, not inside it. Every root carried a trailing
        slash, so the prefix test answered False for the shortest possible case."""
        roots = self.hc._ephemeral_roots()
        self.assertTrue(roots, "no ephemeral roots derived — fixture would be vacuous")
        for root in roots:
            self.assertTrue(self.hc._is_ephemeral(root), root)
            self.assertTrue(self.hc._is_ephemeral(root + "/"), root + "/")

    def test_sibling_named_like_a_root_is_not_ephemeral(self):
        """Containment is by path component, not string prefix; only the shallowest
        root can be the sibling, because macOS roots nest."""
        roots = self.hc._ephemeral_roots()
        shallow = [r for r in roots
                   if not any(r != o and r.startswith(o + "/") for o in roots)]
        self.assertTrue(shallow, "no shallowest root — fixture would be vacuous")
        for root in shallow:
            self.assertFalse(self.hc._is_ephemeral(root + "foo"), root + "foo")
            self.assertFalse(self.hc._is_ephemeral(root + "foo/skills/a"), root + "foo")

    def test_roots_are_derived_not_literal(self):
        """The scan forbids a host path literal, so the roots must come from the
        platform — and must still cover what the literal list covered."""
        roots = self.hc._ephemeral_roots()
        self.assertIn(os.path.realpath(tempfile.gettempdir()).rstrip("/"), roots)
        for expected in ("/tmp", os.path.realpath("/tmp").rstrip("/")):
            self.assertIn(expected, roots)

    def _with_tmpdir(self, value, fn):
        """Evaluate `fn()` as if the platform reported `value` as its temp dir.
        Call the predicate INSIDE the window; after it, roots re-derive from the host."""
        orig = tempfile.gettempdir
        tempfile.gettempdir = lambda: value
        try:
            self.hc._ephemeral_roots.cache_clear()
            return fn()
        finally:
            tempfile.gettempdir = orig
            self.hc._ephemeral_roots.cache_clear()

    def _roots_with_tmpdir(self, value):
        return self._with_tmpdir(value, self.hc._ephemeral_roots)

    def test_macos_tmpdir_also_yields_the_shared_folders_base(self):
        """macOS $TMPDIR is <base>/folders/xx/yy/T, so the shared `/folders` base must
        also be a root or ANOTHER session's scratch dir reads durable."""
        def check():
            roots = self.hc._ephemeral_roots()
            self.assertIn("/var/folders", roots)
            self.assertIn("/var/folders/ab/cd/T", roots)
            # Evaluated INSIDE the window, so the assertion is about the derived
            # roots and not about whatever the host's real TMPDIR happens to be.
            self.assertTrue(self.hc._is_ephemeral("/var/folders/zz/other/T/x"),
                            "a peer session's scratch dir must be ephemeral")
        self._with_tmpdir("/var/folders/ab/cd/T", check)

    def test_an_empty_tmpdir_is_skipped_not_added_as_a_root(self):
        """An empty temp dir must not become the root '' — which, as a prefix, would
        make EVERY path ephemeral."""
        def check():
            roots = self.hc._ephemeral_roots()
            self.assertNotIn("", roots)
            self.assertIn("/tmp", roots)
            self.assertFalse(self.hc._is_ephemeral(str(Path.home() / "durable")))
        self._with_tmpdir("", check)


if __name__ == "__main__":
    unittest.main()
