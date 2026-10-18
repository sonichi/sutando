#!/usr/bin/env python3
"""fix_launchd must repair voice-agent through the GUARDED restart wrapper
(impl plan amendment T4 — kill-path inventory), never a direct
`launchctl kickstart -k gui/<uid>/com.sutando.voice-agent`.

kickstart -k is a kill-and-restart: the pre-kickstart validation (identity of
the running job pid) must run as ONE guarded voice-lock.py takeover
transaction — scripts/restart-voice-agent.sh wraps exactly that and fails
closed without an interpreter. Other labels (web-client) keep the direct
kickstart, which never names voice-agent.

Run: python3 tests/health-check-voice-guarded-fix.test.py
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hc)


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class GuardedVoiceFixTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="hc-guarded-fix-"))
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.addCleanup(__import__("shutil").rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def write_plist(self, label):
        (self.home / "Library" / "LaunchAgents" / f"{label}.plist").write_text("<plist/>\n")

    def run_fix(self, label, results):
        """Run fix_launchd with subprocess.run recorded; `results` maps a
        recognizable argv token to a canned CompletedProcess."""

        def fake_run(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[0] == "/usr/bin/id":
                return _completed(stdout="501\n")
            for token, result in results.items():
                if any(token in str(part) for part in cmd):
                    return result
            return _completed(returncode=1, stderr="unexpected call")

        with mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
            return hc.fix_launchd(label)

    def cmd_strings(self):
        return [" ".join(str(part) for part in cmd) for cmd in self.calls]

    def test_voice_agent_repair_goes_through_the_guarded_wrapper(self):
        self.write_plist("com.sutando.voice-agent")
        out = self.run_fix(
            "com.sutando.voice-agent",
            {"restart-voice-agent.sh": _completed(returncode=0)},
        )
        self.assertIn("guarded restart wrapper", out)
        joined = self.cmd_strings()
        self.assertTrue(
            any("restart-voice-agent.sh" in c for c in joined),
            f"wrapper never invoked: {joined}",
        )
        self.assertFalse(
            any("kickstart" in c for c in joined),
            f"direct kickstart of voice-agent must not happen: {joined}",
        )

    def test_voice_agent_wrapper_failure_falls_back_to_bootstrap_not_kickstart(self):
        self.write_plist("com.sutando.voice-agent")
        out = self.run_fix(
            "com.sutando.voice-agent",
            {
                # exit 6 = the wrapper's fail-closed no-interpreter path — it
                # must NOT be worked around with a raw kickstart.
                "restart-voice-agent.sh": _completed(returncode=6, stdout="FAIL no usable python3"),
                "bootstrap": _completed(returncode=0),
            },
        )
        self.assertIn("bootstrapped", out)
        joined = self.cmd_strings()
        self.assertFalse(any("kickstart" in c for c in joined), joined)
        self.assertTrue(any("bootstrap" in c for c in joined), joined)

    def test_web_client_keeps_the_direct_kickstart(self):
        self.write_plist("com.sutando.web-client")
        out = self.run_fix(
            "com.sutando.web-client",
            {"kickstart": _completed(returncode=0)},
        )
        self.assertEqual(out, "restarted com.sutando.web-client")
        joined = self.cmd_strings()
        self.assertFalse(any("restart-voice-agent.sh" in c for c in joined), joined)

    def test_voice_agent_without_plist_stays_advisory_and_signals_nothing(self):
        out = self.run_fix("com.sutando.voice-agent", {})
        self.assertIn("not launchd-managed", out)
        non_id = [c for c in self.calls if c[0] != "/usr/bin/id"]
        self.assertEqual(non_id, [], f"no process may be signaled: {non_id}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
