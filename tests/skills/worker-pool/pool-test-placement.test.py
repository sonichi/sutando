#!/usr/bin/env python3
"""Keep pool-named root tests in tests/skills/worker-pool/."""
from pathlib import Path
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]
UNRELATED_POOL_TESTS = {
    "health-check-git-stdin-pipe-pool.test.py":
        "macOS kernel pipe-buffer pool, unrelated to worker-pool",
}


def misplaced_pool_tests(tests_root: Path) -> list[str]:
    return sorted(
        path.name for path in tests_root.glob("*pool*")
        if path.is_file() and path.name not in UNRELATED_POOL_TESTS
    )


class PoolTestPlacement(unittest.TestCase):
    def test_root_has_no_worker_pool_named_files(self):
        self.assertEqual(
            misplaced_pool_tests(REPO / "tests"),
            [],
            "Move pool-named tests to tests/skills/worker-pool/; "
            "shared pool contracts belong there too.",
        )

    def test_detects_pool_names_anywhere_in_root_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offenders = [
                "pool-router-replay-idempotence.test.py",
                "health-check-pool-suspended.test.py",
                "core-never-names-the-worker-pool-skill.test.py",
                "worker-pool.test.sh",
            ]
            for name in offenders:
                (root / name).touch()
            self.assertEqual(misplaced_pool_tests(root), sorted(offenders))

    def test_pipe_buffer_pool_exemption_is_an_exact_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "health-check-git-stdin-pipe-pool.test.py").touch()
            worker_pool_test = "health-check-git-stdin-worker-pool.test.py"
            (root / worker_pool_test).touch()
            self.assertEqual(misplaced_pool_tests(root), [worker_pool_test])

    def test_allows_unrelated_root_tests_and_scoped_pool_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker-auth-state.test.py").touch()
            scoped = root / "skills" / "worker-pool"
            scoped.mkdir(parents=True)
            (scoped / "pool-router-replay-idempotence.test.py").touch()
            (root / "pool-fixtures").mkdir()
            self.assertEqual(misplaced_pool_tests(root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
