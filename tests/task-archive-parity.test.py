"""Cross-language parity test for task-archive (#1335 sub-PR-1).

Asserts that ``src/task_archive.py:archive_file()`` (Python) and
``src/task-archive.ts:archiveFile()`` (TypeScript) produce **identical
observable filesystem mutations** for the same fixtures.

Why this matters: bridges using either impl must agree on archive layout.
A drift between Python and TS would mean the same task-id archives to
different paths depending on which bridge handled it, breaking the
"bin-of-archived-tasks-by-month" mental model the archive is for.

Contract documented in ``docs/bridge-helpers-design.md`` § task-archive
helper. If you update the contract, update both impls AND this test.

Strategy: for each fixture, run the Python helper against its own
tempdir and the TS helper against a parallel tempdir, then compare the
filesystem trees. They must match exactly (same dirs, same files, same
contents). Per-impl unit tests live in tests/task-archive.test.py and
tests/task-archive.test.ts.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "src"))
from task_archive import archive_file as py_archive_file


_SHIM_PATH = REPO_ROOT / "tests" / "_helpers" / "task-archive-shim.ts"


def _ts_archive_file(src: Path, kind: str, task_id: str, base: Path) -> None:
    """Invoke the TS archiveFile via tests/_helpers/task-archive-shim.ts.

    Args go via the PARITY_ARGS env var (avoids argv-quoting pain across
    tsx --eval). The shim itself calls archiveFile(), whose silent-on-
    failure contract matches the Python impl — the test asserts via
    filesystem state, not subprocess exit code."""
    payload = json.dumps({
        "src": str(src),
        "kind": kind,
        "taskId": task_id,
        "base": str(base),
    })
    env = {**os.environ, "PARITY_ARGS": payload}
    result = subprocess.run(
        ["npx", "tsx", str(_SHIM_PATH)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    # archiveFile() handles its own errors and falls back to unlink; the
    # shim doesn't propagate them as non-zero exit codes. Only fail the
    # test if the shim itself crashed before reaching archiveFile.
    if result.returncode != 0 and "task-archive-shim" not in result.stderr:
        raise RuntimeError(
            f"ts shim crashed (non-helper error): {result.stderr}"
        )


def _fs_snapshot(root: Path) -> dict[str, str]:
    """Return a dict of relative_path -> content for every file under root.
    Used to compare filesystem state across Python/TS runs."""
    if not root.exists():
        return {}
    snap: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            try:
                snap[rel] = p.read_text()
            except UnicodeDecodeError:
                snap[rel] = f"<binary {p.stat().st_size}b>"
    return snap


@unittest.skipIf(shutil.which("npx") is None, "npx not on PATH; skipping cross-lang parity")
class TestArchiveFileParity(unittest.TestCase):
    """Each test runs the same fixture through Python and TS and asserts
    identical filesystem state after."""

    def _run_pair(self, setup):
        """``setup(base)`` should:
        1. Create input files (e.g. src/task-X.txt) inside ``base``.
        2. Return ``(src_relpath, kind, task_id)``.
        Both runs use a fresh base, so each impl starts with identical
        precondition. After both run, the bases should be identical."""
        py_base = Path(tempfile.mkdtemp(prefix="parity-py-"))
        ts_base = Path(tempfile.mkdtemp(prefix="parity-ts-"))
        self.addCleanup(lambda: shutil.rmtree(py_base, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(ts_base, ignore_errors=True))

        py_args = setup(py_base)
        ts_args = setup(ts_base)
        # Sanity-check the setups produced the same precondition:
        self.assertEqual(_fs_snapshot(py_base), _fs_snapshot(ts_base),
                         "fixture setup must be identical for both impls")

        py_src_rel, kind, task_id = py_args
        ts_src_rel, _, _ = ts_args
        py_archive_file(py_base / py_src_rel, kind, task_id, base=py_base)
        _ts_archive_file(ts_base / ts_src_rel, kind, task_id, base=ts_base)

        py_state = _fs_snapshot(py_base)
        ts_state = _fs_snapshot(ts_base)
        self.assertEqual(py_state, ts_state,
                         f"\nPython produced:\n{py_state}\n\nTS produced:\n{ts_state}")

    def test_parity_moves_task_file(self):
        def setup(base):
            (base / "tasks").mkdir()
            src = base / "tasks" / "task-A.txt"
            src.write_text("hello task A")
            return "tasks/task-A.txt", "tasks", "task-A"
        self._run_pair(setup)

    def test_parity_moves_result_file(self):
        def setup(base):
            (base / "results").mkdir()
            src = base / "results" / "task-B.txt"
            src.write_text("hello result B")
            return "results/task-B.txt", "results", "task-B"
        self._run_pair(setup)

    def test_parity_silent_noop_when_src_missing(self):
        def setup(base):
            (base / "tasks").mkdir()
            # No src file written.
            return "tasks/task-missing.txt", "tasks", "task-missing"
        self._run_pair(setup)

    def test_parity_creates_archive_dir_recursively(self):
        def setup(base):
            (base / "tasks").mkdir()
            src = base / "tasks" / "task-C.txt"
            src.write_text("recursive mkdir")
            # archive/ doesn't exist yet under base/tasks/; both impls
            # must create it.
            return "tasks/task-C.txt", "tasks", "task-C"
        self._run_pair(setup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
