#!/usr/bin/env python3
"""The restart chain end to end: health-check's restart env, the dispatcher, the runtime launcher.

- An explicit CODEX_NODE_BIN / CODEX_INSTALL_BIN stays ahead of the bundled node and
  ~/.local/bin through health-check's env (it injects no default ahead of them).
- A launchd-minimal restart through health-check's env still finds a `claude` that lives
  only in ~/.local/bin, or only in an env-named desktop root's cli/claude/bin.
- startup.sh's own bundled-node PATH step, then the dispatcher and codex core, still keeps
  CODEX_NODE_BIN ahead of the bundled node.

The launcher fixtures are the two launcher suites' own; this file only supplies the PATH
health-check hands the launcher. Homebrew is stripped from it: a host's real tmux, claude or
codex there would shadow the fixture's stubs, and homebrew is not what is under test.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load("health_check_chain", REPO / "src" / "health-check.py")
codex_suite = _load("codex_core_launcher_suite", REPO / "tests" / "codex-core-launcher.test.py")
claude_suite = _load("start_cli_restart_verify_suite", REPO / "tests" / "start-cli-restart-verify.test.py")


def restart_path(inherited: str, repo: Path) -> str:
    """The PATH health-check's restart env hands a launcher started with `inherited`."""
    with mock.patch.dict(os.environ, {"PATH": inherited}), mock.patch.object(hc, "REPO_DIR", repo):
        parts = hc._resolve_launch_env()["PATH"].split(":")
    return ":".join(p for p in parts if p not in ("/opt/homebrew/bin", "/usr/local/bin"))


class CodexRestartChain(codex_suite.CodexCoreLauncherTests):
    def test_health_check_restart_keeps_the_explicit_overrides_first(self):
        ran, path, dirs = self._launch_path_order(
            {"CODEX_NODE_BIN": "override_node"},
            restart_path=lambda inherited: restart_path(inherited, self.root))
        self.assertEqual(ran, "install", "~/.local/bin/codex shadowed CODEX_INSTALL_BIN")
        self.assertLess(path.index(dirs["override_node"]), path.index(dirs["bundled"]),
                        "health-check's restart env put the bundled node ahead of CODEX_NODE_BIN")
        self.assertLess(path.index(dirs["install"]), path.index(dirs["local"]))


def startup_node_step(inherited: str, repo: Path, bundled: Path) -> dict:
    """PATH and launch marker after src/startup.sh's bundled-node step, run from its own text."""
    lines = (REPO / "src" / "startup.sh").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith('_APP_NODE_DIR="$('))
    end = next(i for i, l in enumerate(lines) if i > start and '"$_APP_NODE_DIR"' in l)
    script = "\n".join([f'REPO="{repo}"'] + lines[start:end + 1]
                       + ['printf "%s\\n%s\\n" "$PATH" "${_SUTANDO_LAUNCH_PATH_ADDED:-}"'])
    env = isolated_child_env(PATH=inherited, SUTANDO_APP_NODE_DIR=str(bundled))
    out = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, check=True)
    path, marker = (out.stdout.splitlines() + ["", ""])[:2]
    return {"PATH": path, "_SUTANDO_LAUNCH_PATH_ADDED": marker}


class StartupChain(codex_suite.CodexCoreLauncherTests):
    def test_startup_node_step_keeps_codex_node_bin_first(self):
        bundled = Path(self.tmp.name) / "runtime" / "node" / "bin"
        ran, path, dirs = self._launch_path_order(
            {"CODEX_NODE_BIN": "override_node"},
            restart_path=lambda inherited: startup_node_step(inherited, self.root, bundled))
        self.assertEqual(ran, "install")
        self.assertLess(path.index(dirs["override_node"]), path.index(dirs["bundled"]),
                        "startup.sh's bundled-node step put the bundled node ahead of CODEX_NODE_BIN")


class ClaudeRestartChain(unittest.TestCase):
    def test_launchd_minimal_restart_finds_a_local_bin_claude(self):
        rc, out = claude_suite._run(restart=True, session=True, core=True, force=True,
                                    with_claude=False,
                                    restart_path=lambda inherited: restart_path(inherited, REPO))
        self.assertNotIn("claude not found", out, "the launcher did not find ~/.local/bin/claude")
        self.assertIn("FAILED: core did not come up", claude_suite._run.last_attempts,
                      "the launch never reached the liveness poll")

    def test_launchd_minimal_restart_of_a_foreign_checkout_finds_the_desktop_claude(self):
        # The repo is not under <app support>/engine/: only SUTANDO_APP_SUPPORT names the root.
        rc, out = claude_suite._run(restart=True, session=True, core=True, force=True,
                                    with_claude=False, app_claude=True,
                                    restart_path=lambda inherited: restart_path(inherited, REPO))
        self.assertNotIn("claude not found", out, "the launcher did not find <app support>/cli/claude/bin")
        self.assertIn("FAILED: core did not come up", claude_suite._run.last_attempts,
                      "the launch never reached the liveness poll")


def load_tests(loader, tests, pattern):
    """Only this file's chain tests, not the inherited codex launcher suite."""
    suite = unittest.TestSuite()
    suite.addTest(CodexRestartChain("test_health_check_restart_keeps_the_explicit_overrides_first"))
    suite.addTest(StartupChain("test_startup_node_step_keeps_codex_node_bin_first"))
    suite.addTest(ClaudeRestartChain("test_launchd_minimal_restart_finds_a_local_bin_claude"))
    suite.addTest(ClaudeRestartChain("test_launchd_minimal_restart_of_a_foreign_checkout_finds_the_desktop_claude"))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
