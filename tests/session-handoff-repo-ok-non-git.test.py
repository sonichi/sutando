#!/usr/bin/env python3
"""`_repo_ok` must accept an app-bundled checkout, which ships without .git.

Run: python3 tests/session-handoff-repo-ok-non-git.test.py

The validator is the gate every candidate path passes through, so when it
rejects a whole class of install nothing downstream runs and there is no error
to notice — the snapshot simply stops updating. That is how #2756 stayed live:
`session-state.md` froze for a week on an app-bundled host and the only symptom
was a stale file.

The negative cases are the point. Widening a validator is only safe if it still
refuses what it refused before, so `no .git AND no src/` and a bare directory are
asserted to stay rejected rather than assumed to.
"""
from __future__ import annotations
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "session-handoff.sh"


def _repo_ok_source() -> str:
    """The live `_repo_ok` definition, read from the script rather than copied.

    A duplicated definition here would keep passing after the real one drifted.
    """
    src = SCRIPT.read_text(errors="ignore")
    m = re.search(r"^_repo_ok\(\).*$", src, re.M)
    assert m, "could not find _repo_ok() in session-handoff.sh"
    return m.group(0)


def _accepts(path: pathlib.Path) -> bool:
    fn = _repo_ok_source()
    r = subprocess.run(["bash", "-c", f'{fn}\n_repo_ok "$1"', "_", str(path)])
    return r.returncode == 0


def _make(root: pathlib.Path, *, git: bool, src: bool) -> pathlib.Path:
    d = root / f"co_{git}_{src}"
    (d / "skills").mkdir(parents=True)
    (d / "CLAUDE.md").write_text("# marker\n")
    if git:
        (d / ".git").mkdir()
    if src:
        (d / "src").mkdir()
    return d


class RepoOkAcceptsNonGitCheckout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_app_bundled_checkout_without_git_is_accepted(self):
        """The regression: an engine checkout ships src/ but no .git."""
        self.assertTrue(
            _accepts(_make(self.root, git=False, src=True)),
            "a checkout with CLAUDE.md + skills/ + src/ must validate without .git",
        )

    def test_ordinary_git_checkout_still_accepted(self):
        """Preservation — the path every developer install takes."""
        self.assertTrue(_accepts(_make(self.root, git=True, src=True)))

    def test_git_checkout_without_src_still_accepted(self):
        """.git alone remains sufficient; the new clause is an OR, not a swap."""
        self.assertTrue(_accepts(_make(self.root, git=True, src=False)))

    def test_neither_marker_is_still_rejected(self):
        """Negative control: the validator must not degrade to 'has CLAUDE.md'."""
        self.assertFalse(
            _accepts(_make(self.root, git=False, src=False)),
            "CLAUDE.md + skills/ alone must not validate",
        )

    def test_unrelated_directory_is_still_rejected(self):
        """Negative control: an empty dir must never look like a checkout."""
        d = self.root / "empty"
        d.mkdir()
        self.assertFalse(_accepts(d))


if __name__ == "__main__":
    unittest.main(verbosity=2)
