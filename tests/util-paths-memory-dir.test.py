#!/usr/bin/env python3
"""Tests for the SUTANDO_PRIVATE_DIR -> SUTANDO_MEMORY_DIR rename (#870).

Verifies:
  1. SUTANDO_MEMORY_DIR is honored as the canonical env var.
  2. SUTANDO_PRIVATE_DIR is honored as a legacy fallback.
  3. SUTANDO_MEMORY_DIR wins when both are set.
  4. A deprecation warning is emitted on every read of the legacy alias.
  5. Neither set -> None (no warning).

Run: python3 tests/util-paths-memory-dir.test.py
Exit: 0 on pass, 1 on fail.
"""
import io
import os
import socket
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Import after sys.path setup
from util_paths import (  # noqa: E402
    _memory_dir_env,
    _private_machine_dir,
    shared_personal_path,
)
import util_paths  # noqa: E402


def clear_env():
    for k in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR", "SUTANDO_HOST_LABEL"):
        os.environ.pop(k, None)


class MemoryDirEnvTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_unset_returns_none(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(_memory_dir_env())
        self.assertEqual(buf.getvalue(), "")  # no warning when unset

    def test_new_var_returned(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/new-memory"
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(_memory_dir_env(), "/tmp/new-memory")
        self.assertEqual(buf.getvalue(), "")  # new var: no warning

    def test_legacy_var_returned_with_warning(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-private"
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(_memory_dir_env(), "/tmp/legacy-private")
        self.assertIn("DEPRECATION", buf.getvalue())
        self.assertIn("SUTANDO_PRIVATE_DIR", buf.getvalue())
        self.assertIn("SUTANDO_MEMORY_DIR", buf.getvalue())

    def test_new_wins_when_both_set(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/new"
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy"
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(_memory_dir_env(), "/tmp/new")
        # No warning when new wins — legacy path not consulted.
        self.assertEqual(buf.getvalue(), "")

    def test_every_read_warns(self):
        # Cron environments miss startup-only warnings, so the warning must
        # fire on every call, not just first. Regression guard for #870.
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy"
        for _ in range(3):
            buf = io.StringIO()
            with redirect_stderr(buf):
                _memory_dir_env()
            self.assertIn("DEPRECATION", buf.getvalue())


class PrivateMachineDirTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_returns_none_when_unset(self):
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(_private_machine_dir())

    def test_new_var_drives_machine_dir(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/memdir"
        host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/memdir/machine-{host}"))

    def test_legacy_var_drives_machine_dir(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-dir"
        host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/legacy-dir/machine-{host}"))


class HostLabelTests(unittest.TestCase):
    """SUTANDO_HOST_LABEL overrides hostname for machine-<host> dir (#871)."""

    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_host_label_used_when_set(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/memdir"
        os.environ["SUTANDO_HOST_LABEL"] = "my-stable-mac"
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path("/tmp/memdir/machine-my-stable-mac"))

    def test_hostname_used_when_label_unset(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/memdir"
        host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/memdir/machine-{host}"))

    def test_empty_label_falls_back_to_hostname(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/memdir"
        os.environ["SUTANDO_HOST_LABEL"] = ""
        host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/memdir/machine-{host}"))


class SharedPersonalPathTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_resolves_via_new_var(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/mem"
        with redirect_stderr(io.StringIO()):
            p = shared_personal_path("MEMORY.md", workspace=Path("/tmp/ws"))
        # Neither path exists -> returns preferred memory-dir path.
        self.assertEqual(p, Path("/tmp/mem/MEMORY.md"))

    def test_resolves_via_legacy_var(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-mem"
        with redirect_stderr(io.StringIO()):
            p = shared_personal_path("MEMORY.md", workspace=Path("/tmp/ws"))
        self.assertEqual(p, Path("/tmp/legacy-mem/MEMORY.md"))



class MemoryDirResolver(unittest.TestCase):
    """`memory_dir()` / `default_memory_dir()` — the one composition every
    reader of the core-memory corpus shares."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_is_claude_home_projects_slug_memory(self):
        d = util_paths.default_memory_dir()
        self.assertEqual(d.name, "memory")
        self.assertEqual(d.parent.parent.name, "projects")
        # the slug is derived, never a re-implemented regex
        repo = Path(util_paths.__file__).parent.parent.resolve()
        self.assertEqual(d.parent.name, util_paths.claude_project_slug(repo))

    def test_override_wins_over_the_default(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/some-corpus"
        self.assertEqual(util_paths.memory_dir(), Path("/tmp/some-corpus"))

    def test_unset_falls_back_to_default(self):
        self.assertEqual(util_paths.memory_dir(), util_paths.default_memory_dir())

    def test_legacy_alias_is_honored_like_its_siblings(self):
        # A legacy-alias host must not silently read an empty default path —
        # that reproduces the very NONE-FOUND bug the corpus reader fixes.
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-corpus"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(util_paths.memory_dir(), Path("/tmp/legacy-corpus"))

    def test_canonical_var_still_wins_over_the_legacy_alias(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-corpus"
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/canonical-corpus"
        self.assertEqual(util_paths.memory_dir(), Path("/tmp/canonical-corpus"))


class HealthCheckDelegates(unittest.TestCase):
    """health-check must not re-compose the path; a second copy is what drifts."""

    def test_default_memory_dir_delegates(self):
        src = (ROOT / "src" / "health-check.py").read_text()
        body = src.split("def _default_memory_dir")[1].split("\ndef ")[0]
        self.assertIn("default_memory_dir()", body)
        for recomposed in ('"projects"', "claude_project_slug(repo)"):
            self.assertNotIn(recomposed, body,
                             f"health-check re-composes the memory path ({recomposed})")

    def test_health_check_keeps_its_own_env_policy(self):
        # health-check reads os.environ directly and does NOT honor the legacy
        # alias. That divergence predates this change and is left untouched.
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertIn('MEMORY_DIR = Path(os.environ.get("SUTANDO_MEMORY_DIR", _default_memory_dir()))', src)


if __name__ == "__main__":
    unittest.main()
