#!/usr/bin/env python3
"""restart.sh must handle the launchd-supervised gateway bridge the way it handles
the credential proxy: kickstart -k on a restart, bootout on --stop-only, and a bare
pkill only when no launchd job is loaded.

Why: on 2026-09-25 a restart.sh run pkilled remote-gateway-bridge (exit 0); the
job's KeepAlive is crash-only, startup.sh did not run afterwards, and the gateway
stayed down for 27 minutes. A pkill alone can never be the whole stop for a
launchd job whose relaunch depends on a later step.

Run: python3 tests/restart-gateway-launchd.test.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESTART = REPO / "src" / "restart.sh"
SRC = RESTART.read_text()
GW_LABEL = "com.sutando.gateway-bridge"


class StaticContractTest(unittest.TestCase):
    def test_restart_asks_launchd_about_the_gateway_job(self):
        self.assertIn(GW_LABEL, SRC, "restart.sh never names the gateway launchd job")
        self.assertRegex(SRC, r'launchctl print "\$_GW_SERVICE"', "no launchctl print gate for the gateway job")

    def test_stop_only_boots_the_gateway_job_out(self):
        # bootout, so KeepAlive cannot resurrect it and startup.sh re-bootstraps it later
        block = SRC[SRC.index("_GW_SERVICE="):]
        self.assertRegex(block, r'--stop-only.*\n.*\n?.*launchctl bootout "\$_GW_SERVICE"', "stop-only does not bootout the gateway job")

    def test_restart_kickstarts_the_gateway_job(self):
        block = SRC[SRC.index("_GW_SERVICE="):]
        self.assertRegex(block, r'launchctl kickstart -k "\$_GW_SERVICE"', "a restart does not kickstart the gateway job")

    def test_pkill_only_in_the_no_job_fallback(self):
        # The bare pkill must survive as the fallback for a legacy bare launch, but
        # it must sit in the else-branch of the launchctl print gate, never bare.
        gw_block = SRC[SRC.index("_GW_SERVICE="):SRC.index("_PROXY_LABEL=")]
        self.assertEqual(gw_block.count('pkill -f "remote-gateway-bridge"'), 1)
        self.assertRegex(gw_block, r'else\n\s*pkill -f "remote-gateway-bridge"')
        before = SRC[:SRC.index("_GW_SERVICE=")]
        self.assertNotIn('pkill -f "remote-gateway-bridge"', before, "a bare pkill of the gateway still runs before the launchd gate")


class BehaviourTest(unittest.TestCase):
    """Drive the gateway block with a fake launchctl and observe which verbs it issues."""

    def _run_block(self, arg: str, job_loaded: bool) -> str:
        start = SRC.index("# Gateway bridge: same launchd handling")
        end = SRC.index("_PROXY_LABEL=")
        block = SRC[start:end]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            log = d / "calls.log"
            shim = d / "launchctl"
            shim.write_text(textwrap.dedent(f"""\
                #!/bin/bash
                echo "$*" >> "{log}"
                if [ "$1" = print ]; then {'exit 0' if job_loaded else 'exit 113'}; fi
                exit 0
                """))
            shim.chmod(0o755)
            pk = d / "pkill"
            pk.write_text(f'#!/bin/bash\necho "pkill $*" >> "{log}"\nexit 0\n')
            pk.chmod(0o755)
            script = d / "block.sh"
            script.write_text("#!/bin/bash\nset -u\n" + block)
            subprocess.run(["bash", str(script), arg], env={"PATH": f"{d}:/usr/bin:/bin", "HOME": str(d)},
                           capture_output=True, text=True, timeout=30)
            return log.read_text() if log.exists() else ""

    # The slice also holds the unrelated pkills after the gateway block (relay,
    # ngrok, ...), so only the gateway's own pkill is asserted on.
    GW_PKILL = "pkill -f remote-gateway-bridge"

    def test_job_loaded_restart_kickstarts_and_does_not_pkill(self):
        calls = self._run_block("", job_loaded=True)
        self.assertIn("kickstart -k gui/", calls)
        self.assertNotIn(self.GW_PKILL, calls)
        self.assertNotIn("bootout", calls)

    def test_job_loaded_stop_only_boots_out_and_does_not_pkill(self):
        calls = self._run_block("--stop-only", job_loaded=True)
        self.assertIn("bootout gui/", calls)
        self.assertNotIn("kickstart", calls)
        self.assertNotIn(self.GW_PKILL, calls)

    def test_no_job_falls_back_to_pkill_only(self):
        calls = self._run_block("", job_loaded=False)
        self.assertIn("pkill -f remote-gateway-bridge", calls)
        self.assertNotIn("kickstart", calls)
        self.assertNotIn("bootout", calls)


class StopOnlyMarkerTest(unittest.TestCase):
    """--stop-only must clear the wrapper's started marker, or the next startup
    reads as "previous process exited" and raises a false restart alert."""

    def _run_stop_only(self, job_loaded: bool, ws: Path) -> None:
        start = SRC.index("# Gateway bridge: same launchd handling")
        end = SRC.index("_PROXY_LABEL=")
        block = SRC[start:end]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shim = d / "launchctl"
            shim.write_text("#!/bin/bash\n" + ("exit 0\n" if job_loaded else 'if [ "$1" = print ]; then exit 113; fi\nexit 0\n'))
            shim.chmod(0o755)
            (d / "pkill").write_text("#!/bin/bash\nexit 0\n")
            (d / "pkill").chmod(0o755)
            script = d / "block.sh"
            script.write_text(f'#!/bin/bash\nset -u\n_WS="{ws}"\nREPO="{ws}"\n' + block)
            subprocess.run(["bash", str(script), "--stop-only"], env={"PATH": f"{d}:/usr/bin:/bin", "HOME": str(d)},
                           capture_output=True, text=True, timeout=30)

    def test_stop_only_removes_the_started_marker(self):
        with tempfile.TemporaryDirectory() as ws:
            ws = Path(ws)
            marker = ws / "state" / "channel-bridge-supervisor" / "gateway.started"
            marker.parent.mkdir(parents=True)
            marker.write_text("1")
            self._run_stop_only(job_loaded=True, ws=ws)
            self.assertFalse(marker.exists(), "--stop-only left gateway.started behind (false alert on next startup)")

    def test_marker_path_matches_the_wrapper(self):
        wrapper = (REPO / "src" / "launchd" / "gateway-bridge-wrapper.sh").read_text()
        self.assertIn('MARKER="$STATE_DIR/gateway.started"', wrapper)
        self.assertIn('STATE_DIR="$WORKSPACE/state/channel-bridge-supervisor"', wrapper)
        self.assertIn("state/channel-bridge-supervisor/gateway.started", SRC)


class DrainSkipTest(unittest.TestCase):
    """The stop drain must not wait on a gateway that launchd relaunches at once;
    it still waits for one that was pkilled (no job), up to the cap."""

    def _run_drain(self, job_loaded: bool) -> float:
        start = SRC.index("STOP_PATTERNS=(")
        # the drain loop ends at the first "done" after the loop head
        loop_head = SRC.index("for _ in $(seq 1 30); do", start)
        end = SRC.index("\ndone\n", loop_head) + len("\ndone\n")
        block = SRC[start:end]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "launchctl").write_text("#!/bin/bash\n" + ("exit 0\n" if job_loaded else "exit 113\n"))
            (d / "launchctl").chmod(0o755)
            # only the gateway ever reads as alive
            (d / "pgrep").write_text('#!/bin/bash\ncase "$*" in *remote-gateway-bridge*) exit 0 ;; *) exit 1 ;; esac\n')
            (d / "pgrep").chmod(0o755)
            script = d / "drain.sh"
            script.write_text('#!/bin/bash\nset -u\nREPO="/nonexistent"\n_GW_SERVICE="gui/1/com.sutando.gateway-bridge"\n' + block)
            import time
            t0 = time.monotonic()
            subprocess.run(["bash", str(script)], env={"PATH": f"{d}:/usr/bin:/bin", "HOME": str(d)},
                           capture_output=True, text=True, timeout=30)
            return time.monotonic() - t0

    def test_launchd_owned_gateway_is_skipped_by_the_drain(self):
        elapsed = self._run_drain(job_loaded=True)
        self.assertLess(elapsed, 1.5, f"drain waited {elapsed:.1f}s on a launchd-owned gateway (should skip it)")

    def test_pkilled_gateway_is_still_drained_to_the_cap(self):
        elapsed = self._run_drain(job_loaded=False)
        self.assertGreater(elapsed, 2.0, f"drain gave up after {elapsed:.1f}s on a pkilled gateway (should wait ~3s)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
