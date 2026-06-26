#!/usr/bin/env python3
"""PR #1440 v1 — production-path tests for src/sutando_config.py.

Python twin of tests/sutando-config.prod-path.test.ts. Mini's v1 review
(2026-06-04 02:30Z) flagged that the existing test suite leans on
`SUTANDO_TEST_MODE=1`, leaving the production code path (env-set, no escape
hatch) under-covered. This file exercises that path directly and asserts the
B4 safety properties on the Python side too.

Coverage parity with the TS twin:
  - NO_COLOR=1 → no ANSI escapes in deprecation stderr.
  - non-TTY stderr → no ANSI escapes regardless of NO_COLOR (sys.stderr.isatty()
    is False under unittest's StringIO capture).
  - Warning text contains NO literal `'<value>'` interpolation for either the
    env-var value or the .env-declared value (c58270d safety pass — verifies
    no regression).
  - Warning fires exactly once per process (_LEGACY_ENV_WARN_PRINTED guard).
  - resolver returns config/default path with env set + no TEST_MODE.

Run: python3 tests/sutando-config.prod-path.test.py
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sutando_config  # noqa: E402
from sutando_config import (  # noqa: E402
    _reset_cache_for_tests,
    resolve_workspace,
)

ANSI_RE = re.compile(r"\x1b\[")


@contextmanager
def capture_stderr() -> io.StringIO:
    """Redirect sys.stderr to a StringIO for the duration of the block."""
    orig = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = orig


class TestProdPath(unittest.TestCase):
    """End-to-end resolver invocations with $SUTANDO_WORKSPACE set + no TEST_MODE."""

    def setUp(self) -> None:
        # Snapshot + clear env; reset per-process cache for warning state.
        self._saved_env = os.environ.pop("SUTANDO_WORKSPACE", None)
        self._saved_no_color = os.environ.pop("NO_COLOR", None)
        self._saved_test_mode = os.environ.pop("SUTANDO_TEST_MODE", None)
        _reset_cache_for_tests()
        self._tmpdir = tempfile.mkdtemp(prefix="sutando-prod-path-")
        self.repo = Path(self._tmpdir)

    def tearDown(self) -> None:
        _reset_cache_for_tests()
        if self._saved_env is None:
            os.environ.pop("SUTANDO_WORKSPACE", None)
        else:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        if self._saved_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = self._saved_no_color
        if self._saved_test_mode is None:
            os.environ.pop("SUTANDO_TEST_MODE", None)
        else:
            os.environ["SUTANDO_TEST_MODE"] = self._saved_test_mode
        # Clean up tmp dir
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # --- B4: NO_COLOR honored --------------------------------------------- #

    def test_no_color_one_strips_ansi(self) -> None:
        """NO_COLOR=1 → deprecation warning has zero ANSI escapes."""
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        os.environ["NO_COLOR"] = "1"
        with capture_stderr() as buf:
            resolve_workspace(self.repo)
        captured = buf.getvalue()
        self.assertIn("NO LONGER HONORED", captured, "expected deprecation text in stderr")
        self.assertIsNone(
            ANSI_RE.search(captured),
            f"expected zero ANSI escapes, got: {captured!r}",
        )

    def test_non_tty_stderr_strips_ansi(self) -> None:
        """Non-TTY stderr (StringIO) → no ANSI escapes regardless of NO_COLOR."""
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        # NO_COLOR explicitly unset.
        with capture_stderr() as buf:
            resolve_workspace(self.repo)
        captured = buf.getvalue()
        self.assertIn("NO LONGER HONORED", captured)
        self.assertIsNone(
            ANSI_RE.search(captured),
            f"expected zero ANSI escapes on non-TTY, got: {captured!r}",
        )

    # --- B4: path-leak — warnings must not contain literal path values ----- #

    def test_warning_omits_literal_env_value(self) -> None:
        """c58270d safety pass — env-set warning omits the env-var value."""
        sneaky = "/this/literal/path/must/not/appear/in/stderr"
        os.environ["SUTANDO_WORKSPACE"] = sneaky
        with capture_stderr() as buf:
            resolve_workspace(self.repo)
        captured = buf.getvalue()
        self.assertNotIn(
            sneaky,
            captured,
            f"warning leaked the env-var value: {captured!r}",
        )

    def test_warning_omits_literal_dotenv_value(self) -> None:
        """c58270d safety pass — .env-drift warning omits the dotenv value."""
        sneaky = "/sneaky/dotenv/value/must/not/leak"
        (self.repo / ".env").write_text(f"SUTANDO_WORKSPACE={sneaky}\n", encoding="utf-8")
        with capture_stderr() as buf:
            resolve_workspace(self.repo)
        captured = buf.getvalue()
        self.assertNotIn(
            sneaky,
            captured,
            f".env-drift warning leaked the value: {captured!r}",
        )

    # --- One-shot warning ---------------------------------------------------- #

    def test_warning_is_one_shot_across_calls(self) -> None:
        """Deprecation warning fires exactly once across 3 resolve calls."""
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        with capture_stderr() as buf:
            resolve_workspace(self.repo)
            resolve_workspace(self.repo)
            resolve_workspace(self.repo)
        captured = buf.getvalue()
        n = captured.count("NO LONGER HONORED")
        self.assertEqual(
            n,
            1,
            f"expected exactly one deprecation warning across 3 calls, got {n}; full: {captured!r}",
        )

    # --- Resolver returns the config-driven / default path with env set ---- #

    def test_env_set_plus_local_config_returns_config_path(self) -> None:
        """env set + sutando.config.local.json present → config wins."""
        (self.repo / "sutando.config.local.json").write_text(
            json.dumps({"workspace": {"path": "/from/local/config"}}),
            encoding="utf-8",
        )
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        with capture_stderr():
            resolved = resolve_workspace(self.repo)
        self.assertEqual(str(resolved), "/from/local/config")

    def test_env_set_plus_no_config_returns_baked_in_default(self) -> None:
        """env set + no config → resolver returns {repoRoot}/workspace.

        Compare against resolve()d expected path because macOS /var symlinks
        to /private/var; the resolver returns the realpath form.
        """
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        with capture_stderr():
            resolved = resolve_workspace(self.repo)
        expected = (self.repo / "workspace").resolve()
        self.assertEqual(str(resolved), str(expected))


if __name__ == "__main__":
    unittest.main()
