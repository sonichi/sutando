#!/usr/bin/env python3
"""`swiftc_usable()` gates the Swift probes on the platform, not on the binary.

Two incidents, one guard. A which-only check is wrong in opposite directions on
the two hosts this repo runs on, and each direction cost a real failure:

* **Linux CI.** The sources under `src/Sutando/` target macOS. When a runner
  image began shipping a Swift toolchain (ubuntu24 20260920.314), every Swift
  probe started running there for the first time and erroring in `setUp` with a
  bare exit status — on `main` as well as on PRs.
* **macOS without the Command Line Tools.** `/usr/bin/swiftc` is the CLT stub
  and exists with no toolchain, so invoking it raises the install dialog the
  probes exist to avoid (#2473).

Both are decided before anything is compiled, so this test needs no toolchain:
it drives the predicate with the platform and `which` patched to each host.

Run: python3 tests/os-probes-swiftc-guard.test.py  (exit 0/1)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
import os_probes  # noqa: E402

GUARDED = (
    "tests/sutando-config-swift.test.py",
    "tests/sutando-config-python-resolution.test.py",
    "tests/session-core-runtime-swift.test.py",
    "tests/restart-coordinator-swift.test.py",
)


class SwiftcUsable(unittest.TestCase):
    def _verdict(self, platform: str, which) -> bool:
        with mock.patch.object(sys, "platform", platform), \
             mock.patch.object(os_probes.shutil, "which", which):
            return os_probes.swiftc_usable()

    def test_linux_with_a_toolchain_present_still_does_not_run(self):
        """THE CI REGRESSION: a Linux image that ships swiftc must still skip."""
        self.assertIs(self._verdict("linux", lambda n: "/usr/bin/swiftc"), False)

    def test_any_non_macos_platform_does_not_run(self):
        for platform in ("linux", "win32", "freebsd13"):
            with self.subTest(platform=platform):
                self.assertIs(self._verdict(platform, lambda n: "/usr/bin/swiftc"), False)

    def test_macos_without_swiftc_does_not_run(self):
        """The CLT-dialog case (#2473): absent binary, no invocation."""
        self.assertIs(self._verdict("darwin", lambda n: None), False)

    def test_macos_with_a_stub_but_no_toolchain_does_not_run(self):
        """`xcode-select -p` answers without prompting; a non-zero exit means no toolchain."""
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(os_probes.shutil, "which", lambda n: "/usr/bin/swiftc"), \
             mock.patch.object(os_probes.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            self.assertIs(os_probes.swiftc_usable(), False)

    def test_macos_with_a_real_toolchain_runs(self):
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(os_probes.shutil, "which", lambda n: "/usr/bin/swiftc"), \
             mock.patch.object(os_probes.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            self.assertIs(os_probes.swiftc_usable(), True)

    def test_a_probe_that_cannot_answer_does_not_run(self):
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(os_probes.shutil, "which", lambda n: "/usr/bin/swiftc"), \
             mock.patch.object(os_probes.subprocess, "run", side_effect=OSError("boom")):
            self.assertIs(os_probes.swiftc_usable(), False)

    def test_the_reason_names_the_platform_requirement(self):
        self.assertIn("macOS", os_probes.SWIFTC_SKIP_REASON)


class EverySwiftProbeUsesIt(unittest.TestCase):
    """One owner for the policy: no file may carry its own swiftc guard again."""

    def test_each_swift_probe_guards_on_the_shared_predicate(self):
        for rel in GUARDED:
            with self.subTest(test=rel):
                src = (REPO / rel).read_text(encoding="utf-8")
                self.assertIn("swiftc_usable()", src)
                self.assertNotIn('skipUnless(shutil.which("swiftc")', src)

    def test_no_test_file_reimplements_the_check(self):
        me = Path(__file__).name  # this file names the pattern in order to ban it
        strays = [p.name for p in (REPO / "tests").glob("*.test.py")
                  if p.name != me
                  and 'skipUnless(shutil.which("swiftc")' in p.read_text(encoding="utf-8")]
        self.assertEqual(strays, [], f"these guard on the binary alone: {strays}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
