#!/usr/bin/env python3
"""The SESSION's runtime outranks config, executed rather than grepped.

`SUTANDO_CORE_RUNTIME=codex bash src/agent/start-cli.sh --restart` (docs/codex-core.md)
starts a Codex session without writing config, so config still says claude. Resolving
from config emits `--runtime claude` and the watchdog submits an operator's draft.
Skipped where swiftc is absent, matching tests/sutando-config-swift.test.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
from os_probes import SWIFTC_SKIP_REASON, swiftc_usable  # noqa: E402
SWIFT_CONFIG = REPO / "src" / "Sutando" / "SutandoConfig.swift"
_MODULE_CACHE = Path(tempfile.gettempdir()) / "sutando-swift-module-cache"


@unittest.skipUnless(swiftc_usable(), SWIFTC_SKIP_REASON)
class SessionCoreRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        # Config says claude — the state a one-command Codex trial leaves behind.
        (self.repo / "sutando.config.json").write_text(
            json.dumps({"core": {"runtime": "claude"}}), encoding="utf-8")
        probe_dir = self.tmp / "probe"
        probe_dir.mkdir()
        (probe_dir / "main.swift").write_text(
            "import Foundation\n"
            "let repo = CommandLine.arguments[1]\n"
            "let tmux = CommandLine.arguments[2]\n"
            "let r = SutandoConfig.sessionCoreRuntime(socket: \"/tmp/x.sock\",\n"
            "                                         repoRoot: repo, tmuxPath: tmux)\n"
            "print(r ?? \"nil\")\n",
            encoding="utf-8")
        self.probe = probe_dir / "probe"
        env = os.environ.copy()
        env["CLANG_MODULE_CACHE_PATH"] = str(_MODULE_CACHE)
        subprocess.run(["swiftc", str(SWIFT_CONFIG), str(probe_dir / "main.swift"),
                        "-o", str(self.probe)],
                       env=env, check=True, text=True, capture_output=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tmux(self, answer: str) -> Path:
        p = self.tmp / f"tmux-{abs(hash(answer))}"
        p.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" {answer!r}\nexit 0\n',
                     encoding="utf-8")
        p.chmod(0o755)
        return p

    def _run(self, answer: str) -> str:
        env = os.environ.copy()
        env.pop("SUTANDO_CORE_RUNTIME", None)
        r = subprocess.run([str(self.probe), str(self.repo), str(self._tmux(answer))],
                           env=env, text=True, capture_output=True, check=False)
        return r.stdout.strip()

    def test_session_codex_beats_config_claude(self) -> None:
        self.assertEqual(self._run("SUTANDO_CORE_RUNTIME=codex"), "codex")

    def test_session_unset_falls_back_to_config(self) -> None:
        # tmux prints `-NAME` when the session has no such variable.
        self.assertEqual(self._run("-SUTANDO_CORE_RUNTIME"), "claude")

    def test_session_names_an_unparseable_runtime(self) -> None:
        # Not a runtime whose pane we can parse: nil, so the caller refuses to type.
        self.assertEqual(self._run("SUTANDO_CORE_RUNTIME=fish"), "nil")


if __name__ == "__main__":
    unittest.main(verbosity=2)
