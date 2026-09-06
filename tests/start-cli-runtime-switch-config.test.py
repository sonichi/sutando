#!/usr/bin/env python3
"""Regression tests for `src/agent/start-cli.sh --runtime` (sonichi#2589 review).

Two blockers, both reachable from the Sutando.app menu:

1. The config writer ran a bare `python3`. This top-level launcher executes
   BEFORE either per-runtime launcher gets control, so it does not inherit their
   `$PY` resolution — and on a fresh Mac bare `python3` is Apple's Xcode-CLT
   stub, which prints an install notice and exits non-zero. The menu switch then
   fails before persisting anything. Same bug class as
   `start-cli-chrome-seed-bundled-python.test.py`.

2. The merge read the existing config with
   `except (OSError, ValueError): cfg = {}` and then wrote the result back. A
   hand-edit typo — a trailing comma is the common one — made the file
   unreadable, so the whole config was replaced by an object containing only
   `core.runtime`. `sutando.config.local.json` is the documented home for
   `workspace.path` and vault settings, so that silently erased them.

These are SOURCE-TIED like the sibling test: the writer heredoc and the resolver
are extracted from the script verbatim and run in a small harness, so the run
stays hermetic (no tmux, no real launch) while still exercising the shipped text.
Drift in either block fails extraction.

Run: python3 tests/start-cli-runtime-switch-config.test.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# resolved with the real PATH, so a test may hand the child an empty one
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = REPO / "src" / "agent" / "start-cli.sh"

# What bare `python3` does on a genuinely fresh Mac.
CLT_STUB = (
    "#!/bin/sh\n"
    'echo "xcode-select: note: No developer tools were found." >&2\n'
    "exit 1\n"
)


def _writer_heredoc() -> str:
    """The python body the script actually feeds to its interpreter."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", src, re.DOTALL)
    assert m, "config-writer heredoc not found in start-cli.sh"
    return m.group(1)


def _resolver_snippet() -> str:
    """The script's own interpreter resolution, extracted verbatim."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(
        r'(\. "\$REPO/scripts/python-binary\.sh".*?\n  fi)\n', src, re.DOTALL)
    assert m, "interpreter resolver not found in start-cli.sh — did it regress to bare python3?"
    return m.group(1)


def _run_writer(repo: str, runtime: str, *, env: dict) -> subprocess.CompletedProcess:
    """Resolve the interpreter the way the script does, then run its writer."""
    body = Path(repo) / ".writer.py"
    body.write_text(_writer_heredoc(), encoding="utf-8")
    # the snippet sources the resolver out of $REPO, so the harness repo carries it
    (Path(repo) / "scripts").mkdir(exist_ok=True)
    shutil.copy(REPO / "scripts" / "python-binary.sh", Path(repo) / "scripts" / "python-binary.sh")
    harness = (
        "set -euo pipefail\n"
        f'REPO="{repo}"\n'
        + _resolver_snippet().replace("  fi", "fi") + "\n"
        f'"$_cfg_py" "{body}" "{repo}" "{runtime}"\n'
    )
    return subprocess.run([BASH, "-c", harness], capture_output=True, text=True, env=env)


class RuntimeSwitchConfig(unittest.TestCase):
    def _env(self, tmp: str, *, stub_python3: bool) -> dict:
        env = dict(os.environ)
        env["SUTANDO_PY"] = sys.executable
        if stub_python3:
            bin_dir = Path(tmp) / "stubbin"
            bin_dir.mkdir(exist_ok=True)
            stub = bin_dir / "python3"
            stub.write_text(CLT_STUB, encoding="utf-8")
            stub.chmod(0o755)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        return env

    def test_no_runnable_interpreter_refuses_before_persisting(self):
        """No SUTANDO_PY, no bundle, no python3 on PATH: refuse, do not write.

        The suite could not reach this while _env always set SUTANDO_PY.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("SUTANDO_PY", None)
            empty = Path(tmp) / "emptypath"
            empty.mkdir()
            env["PATH"] = str(empty)   # /bin holds python3 on Linux; this holds nothing
            r = _run_writer(tmp, "codex", env=env)
            self.assertNotEqual(r.returncode, 0,
                "with no runnable interpreter the switch must fail, not shell a stub")
            self.assertIn("no runnable interpreter", r.stderr)
            self.assertFalse((Path(tmp) / "sutando.config.local.json").exists(),
                "core.runtime must not be persisted when no interpreter resolved")

    def test_source_no_longer_calls_bare_python3_for_the_writer(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(
            "python3 - \"$REPO\" \"$requested_runtime\"", src,
            "the config writer must not invoke a bare python3")

    def test_writer_runs_when_bare_python3_is_the_CLT_stub(self):
        """The menu path must still persist with a broken python3 first on PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "sutando.config.local.json"
            cfg.write_text(json.dumps({"workspace": {"path": "/w"}}), encoding="utf-8")
            r = _run_writer(tmp, "codex", env=self._env(tmp, stub_python3=True))
            self.assertEqual(r.returncode, 0, f"writer failed: {r.stderr}")
            got = json.loads(cfg.read_text())
            self.assertEqual(got["core"]["runtime"], "codex")
            self.assertEqual(got["workspace"], {"path": "/w"}, "unrelated keys must survive")

    def test_unreadable_config_is_refused_not_erased(self):
        """A trailing-comma typo must fail closed, not wipe workspace/vault keys."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "sutando.config.local.json"
            original = '{\n  "workspace": {"path": "/w"},\n  "vault": {"enabled": true},\n}\n'
            cfg.write_text(original, encoding="utf-8")
            r = _run_writer(tmp, "codex", env=self._env(tmp, stub_python3=False))
            self.assertNotEqual(r.returncode, 0, "must not exit 0 on an unreadable config")
            self.assertEqual(cfg.read_text(), original,
                             "the existing config must be left byte-identical")

    def test_non_object_config_is_refused_not_erased(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "sutando.config.local.json"
            cfg.write_text("[1, 2, 3]\n", encoding="utf-8")
            r = _run_writer(tmp, "claude", env=self._env(tmp, stub_python3=False))
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(cfg.read_text(), "[1, 2, 3]\n")

    def test_missing_config_is_created(self):
        """Absent is not corrupt — a first switch on a clean clone must work."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "sutando.config.local.json"
            r = _run_writer(tmp, "claude", env=self._env(tmp, stub_python3=False))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(cfg.read_text())["core"]["runtime"], "claude")


if __name__ == "__main__":
    unittest.main(verbosity=2)
