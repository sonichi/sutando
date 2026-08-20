#!/usr/bin/env python3
"""SutandoConfig.resolvePython — the menu-bar app must never spawn the CLT stub.

Why this exists
---------------
`Sutando.app` runs `src/health-check.py` from a 60-second Timer. It used to
pick the interpreter like this:

    let homebrewPython = "/opt/homebrew/opt/python@3.11/libexec/bin/python3"
    let pythonPath = FileManager.default.fileExists(atPath: homebrewPython)
        ? homebrewPython : "/usr/bin/env"

Two problems. The hardcoded formula path is dead on any host that has moved
past python@3.11 (so essentially every host now takes the fallback), and the
fallback `/usr/bin/env python3` resolves to `/usr/bin/python3` — which on macOS
is the Xcode-CLT stub, not python. Without developer tools it raises the modal
"install command line developer tools" dialog and returns nothing. On a Timer,
that dialog reappears forever. Reproduced on a clean macOS VM.

`resolvePython` replaces it with the cascade the shell side already uses
(`scripts/sutando-config.sh`, `src/agent/claude/cli/start-cli.sh`):
$SUTANDO_PY -> bundled runtime -> system python3 *only if* the tools are
installed -> nil, meaning the caller skips instead of prompting.

How it is tested
----------------
Compiles `SutandoConfig.swift` against a small probe that drives each tier with
injected `isExecutable` / `toolsInstalled`, so every branch is exercised without
mutating the host toolchain. Same harness shape as
`tests/sutando-config-swift.test.py`.

Run: python3 tests/sutando-config-python-resolution.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWIFT_CONFIG = ROOT / "src" / "Sutando" / "SutandoConfig.swift"
MAIN_SWIFT = ROOT / "src" / "Sutando" / "main.swift"

# A path the repo's hardcoded-path gate explicitly allows as a fixture
# (REVIEW.md `checks.hardcoded-paths.allow`), so these stand-ins cannot be
# confused for a real host path.
FAKE_PY = "/usr/fake/sutando-py"


def _swiftc_usable() -> bool:
    """swiftc is present AND actually runnable.

    `shutil.which("swiftc")` on its own is not enough on macOS: /usr/bin/swiftc
    is the CLT stub and exists even with no toolchain installed, so a
    which-only guard would make this very test spawn the dialog it exists to
    prevent. Ask xcode-select instead — a real binary that does not prompt.
    """
    if not shutil.which("swiftc"):
        return False
    if sys.platform != "darwin":
        return True
    try:
        return subprocess.run(
            ["xcode-select", "-p"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


PROBE_SOURCE = r"""
import Foundation

let repo = CommandLine.arguments[1]
let mode = CommandLine.arguments[2]
let fakePy = "%FAKE_PY%"

func executableOnly(_ paths: Set<String>) -> (String) -> Bool {
    return { paths.contains($0) }
}

let bundled = URL(fileURLWithPath: repo)
    .deletingLastPathComponent()
    .appendingPathComponent("runtime/python/bin/python3")
    .path

var result: String?
switch mode {
case "explicit":
    result = SutandoConfig.resolvePython(
        repoRoot: repo,
        environment: ["SUTANDO_PY": fakePy],
        isExecutable: executableOnly([fakePy, bundled]),
        toolsInstalled: { true })
case "explicit-not-executable":
    // SUTANDO_PY points at something that isn't runnable -> fall through.
    result = SutandoConfig.resolvePython(
        repoRoot: repo,
        environment: ["SUTANDO_PY": fakePy],
        isExecutable: executableOnly([bundled]),
        toolsInstalled: { true })
case "explicit-empty":
    result = SutandoConfig.resolvePython(
        repoRoot: repo,
        environment: ["SUTANDO_PY": ""],
        isExecutable: executableOnly([bundled]),
        toolsInstalled: { true })
case "bundled":
    result = SutandoConfig.resolvePython(
        repoRoot: repo,
        environment: [:],
        isExecutable: executableOnly([bundled]),
        toolsInstalled: { false })
case "system-with-tools":
    result = SutandoConfig.resolvePython(
        repoRoot: repo,
        environment: [:],
        isExecutable: executableOnly([]),
        toolsInstalled: { true })
case "no-tools":
    result = SutandoConfig.resolvePython(
        repoRoot: repo,
        environment: [:],
        isExecutable: executableOnly([]),
        toolsInstalled: { false })
case "bundled-path":
    result = bundled
case "system-constant":
    result = SutandoConfig.systemPython
default:
    FileHandle.standardError.write("unknown mode\n".data(using: .utf8)!)
    exit(2)
}
print(result ?? "nil")
"""


@unittest.skipUnless(_swiftc_usable(), "swiftc not usable on this host")
class ResolvePython(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="sutando-python-resolution-"))
        probe_dir = cls.tmp / "probe"
        probe_dir.mkdir()
        (probe_dir / "main.swift").write_text(
            PROBE_SOURCE.replace("%FAKE_PY%", FAKE_PY), encoding="utf-8"
        )
        cls.probe = probe_dir / "probe"
        env = os.environ.copy()
        # Persistent, NOT under cls.tmp: a per-run cache is deleted in teardown, so
        # every run rebuilt Foundation's modules cold — measured 4.9x the warm cost.
        cache = Path(tempfile.gettempdir()) / "sutando-swift-module-cache"
        cache.mkdir(parents=True, exist_ok=True)
        env["CLANG_MODULE_CACHE_PATH"] = str(cache)
        subprocess.run(
            ["swiftc", str(SWIFT_CONFIG), str(probe_dir / "main.swift"), "-o", str(cls.probe)],
            env=env,
            check=True,
            capture_output=True,
        )
        # An "engine" dir whose sibling is the vendored runtime, matching the
        # bundle layout scripts/sutando-config.sh documents.
        cls.repo = cls.tmp / "engine"
        cls.repo.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, mode: str) -> str:
        out = subprocess.run(
            [str(self.probe), str(self.repo), mode],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    def test_explicit_sutando_py_wins(self):
        self.assertEqual(self._run("explicit"), FAKE_PY)

    def test_explicit_is_ignored_when_not_executable(self):
        """A stale SUTANDO_PY must not shadow a working bundled runtime."""
        self.assertEqual(self._run("explicit-not-executable"), self._run("bundled-path"))

    def test_explicit_is_ignored_when_empty(self):
        self.assertEqual(self._run("explicit-empty"), self._run("bundled-path"))

    def test_bundled_runtime_is_used_without_developer_tools(self):
        """The bundled-install case: no toolchain needed at all."""
        self.assertEqual(self._run("bundled"), self._run("bundled-path"))

    def test_system_python_only_when_tools_are_installed(self):
        self.assertEqual(self._run("system-with-tools"), self._run("system-constant"))

    def test_system_constant_is_exactly_the_stub_path(self):
        """Pin the VALUE, not just "whatever the constant says".

        `systemPython` is assembled from `systemBin + "/python3"` rather than
        written as one literal, because REVIEW.md lesson 7 flags that exact
        token in production sources (#2474 added it; this PR's own defining
        site was the first thing it caught). Composing it keeps the scanner
        honest about exec targets, but it also means a typo in either half
        would still satisfy the assertion above, which only checks that the
        resolver agrees with the constant.

        So assert the concrete path here. Tests are scanner-exempt, so the
        literal lives where it is allowed and stays greppable.
        """
        self.assertEqual(self._run("system-constant"), "/usr/bin/python3")
        self.assertEqual(self._run("system-with-tools"), "/usr/bin/python3")

    def test_returns_nil_rather_than_spawning_the_stub(self):
        """The clean-VM case — the whole point of the fix.

        No $SUTANDO_PY, no bundled runtime, no developer tools. The old code
        returned "/usr/bin/env" here and spawned the stub; the fix returns nil
        so the caller skips.
        """
        self.assertEqual(self._run("no-tools"), "nil")


class CallSiteIsWired(unittest.TestCase):
    """Source-tied guards — these run everywhere, including hosts with no swiftc."""

    def test_main_swift_uses_the_resolver(self):
        # assertTrue on a precomputed bool, not assertIn on the file body:
        # assertIn prints the whole container on failure, and main.swift is
        # ~130KB — the finding would be buried.
        wired = "SutandoConfig.resolvePython(repoRoot: repoRoot)" in MAIN_SWIFT.read_text(
            encoding="utf-8"
        )
        self.assertTrue(
            wired, "main.swift no longer resolves the interpreter via SutandoConfig"
        )

    def test_main_swift_does_not_fall_back_to_env_python3(self):
        hits = [
            f"  main.swift:{n}: {line.strip()}"
            for n, line in enumerate(
                MAIN_SWIFT.read_text(encoding="utf-8").splitlines(), start=1
            )
            if '"/usr/bin/env"' in line
        ]
        self.assertEqual(
            hits,
            [],
            "\nmain.swift resolves an interpreter through /usr/bin/env again.\n"
            "On a Mac without developer tools that lands on the CLT stub and "
            "raises the install dialog.\n" + "\n".join(hits),
        )

    def test_main_swift_does_not_hardcode_a_homebrew_python(self):
        # Comment lines are exempt: the fix's own rationale names python@3.11
        # to explain what was removed, and that prose is the reason a future
        # reader does not put it back. Only executable lines are the hazard.
        hits = [
            f"  main.swift:{n}: {line.strip()}"
            for n, line in enumerate(
                MAIN_SWIFT.read_text(encoding="utf-8").splitlines(), start=1
            )
            if "python@" in line and not line.strip().startswith("//")
        ]
        self.assertEqual(
            hits,
            [],
            "\nmain.swift hardcodes a versioned Homebrew python formula path "
            "again.\nThose go stale silently — python@3.11 was already absent "
            "on the maintainer's own dev machine.\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
