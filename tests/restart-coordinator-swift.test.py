#!/usr/bin/env python3
"""Menu-click interleavings for the graceful-restart lifecycle.

tests/graceful-restart.test.sh covers the shell orchestrator; it cannot execute
the Swift state machine that decides how two clicks interact. Skipped on hosts
without swiftc.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR = ROOT / "src" / "Sutando" / "RestartCoordinator.swift"

PROBE = r"""
import Foundation

final class Recorder: RestartCancellable {
    var terminated = 0
    var waited = 0
    func terminate() { terminated += 1 }
    func waitUntilExit() { waited += 1 }
}

func name(_ p: RestartCoordinator.Phase) -> String {
    switch p {
    case .idle: return "idle"
    case .starting: return "starting"
    case .waiting: return "waiting"
    case .killing: return "killing"
    }
}

func show(_ c: RestartClaim) -> String {
    switch c {
    case .accepted(let e): return "accepted(\(e))"
    case .rejectedAlreadyQueued: return "rejectedAlreadyQueued"
    case .rejectedPastKill: return "rejectedPastKill"
    }
}

func show(_ o: LaunchOutcome) -> String {
    switch o {
    case .launched: return "launched"
    case .aborted: return "aborted"
    case .failed(let r): return "failed(\(r))"
    }
}

func show(_ f: ForceClaim) -> String {
    switch f {
    case .rejectedPastKill: return "rejectedPastKill"
    case .cancelledBeforeLaunch: return "cancelledBeforeLaunch"
    case .cancelledWhileWaiting: return "cancelledWhileWaiting"
    case .nothingToCancel: return "nothingToCancel"
    }
}

struct Boom: Error, LocalizedError {
    var errorDescription: String? { "boom" }
}

let scenario = CommandLine.arguments[1]
let c = RestartCoordinator()
let rec = Recorder()

switch scenario {

case "restart-twice-while-starting":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    print("claim1=accepted(\(e1))")
    print("claim2=" + show(c.claimRestart()))
    print("launch1=" + show(c.launch(epoch: e1) { }))
    print("phase=" + name(c.currentPhase))

case "restart-twice-while-waiting":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    print("launch1=" + show(c.launch(epoch: e1) { }))
    print("phase=" + name(c.currentPhase))
    print("claim2=" + show(c.claimRestart()))

case "force-before-run":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    c.track(rec, epoch: e1)
    let (f, q) = c.claimForce()
    print("force=" + show(f))
    print("queued=" + (q == nil ? "nil" : "some"))
    print("launch1=" + show(c.launch(epoch: e1) { print("SIDE-EFFECT-RAN") }))
    print("terminated=\(rec.terminated)")
    print("phase=" + name(c.currentPhase))

case "force-while-waiting":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    c.track(rec, epoch: e1)
    print("launch1=" + show(c.launch(epoch: e1) { }))
    let (f, q) = c.claimForce()
    print("force=" + show(f))
    if let q = q { q.terminate(); q.waitUntilExit() }
    print("terminated=\(rec.terminated) waited=\(rec.waited)")
    print("phase=" + name(c.currentPhase))

case "force-after-killing":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    _ = c.launch(epoch: e1) { }
    c.enterKilling(epoch: e1)
    print("phase=" + name(c.currentPhase))
    let (f, q) = c.claimForce()
    print("force=" + show(f))
    print("queued=" + (q == nil ? "nil" : "some"))
    print("phase=" + name(c.currentPhase))

case "restart-while-killing":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    _ = c.launch(epoch: e1) { }
    c.enterKilling(epoch: e1)
    print("claim2=" + show(c.claimRestart()))

case "nudge-not-after-force":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    _ = c.launch(epoch: e1) { }
    print("nudge-before=\(c.nudgeApplies(epoch: e1))")
    _ = c.claimForce()
    print("nudge-after=\(c.nudgeApplies(epoch: e1))")

case "superseded-finish-does-not-reset":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    _ = c.launch(epoch: e1) { }
    _ = c.claimForce()
    guard case .accepted(let e3) = c.claimRestart() else { fatalError("claim3") }
    print("e1=\(e1) e3=\(e3)")
    c.finish(epoch: e1, proc: nil)
    print("phase=" + name(c.currentPhase))

case "launch-failure-resets":
    guard case .accepted(let e1) = c.claimRestart() else { fatalError("claim1") }
    c.track(rec, epoch: e1)
    print("launch1=" + show(c.launch(epoch: e1) { throw Boom() }))
    print("phase=" + name(c.currentPhase))
    print("claim2=" + show(c.claimRestart()))

default:
    fatalError("unknown scenario \(scenario)")
}
"""


@unittest.skipUnless(shutil.which("swiftc"), "swiftc not available")
class TestRestartCoordinator(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="sutando-restart-coord-"))
        probe_dir = cls.tmp / "probe"
        probe_dir.mkdir()
        (probe_dir / "main.swift").write_text(PROBE, encoding="utf-8")
        cls.bin = probe_dir / "probe"
        env = os.environ.copy()
        env["CLANG_MODULE_CACHE_PATH"] = str(cls.tmp / "module-cache")
        subprocess.run(
            ["swiftc", str(COORDINATOR), str(probe_dir / "main.swift"), "-o", str(cls.bin)],
            env=env, check=True, text=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_scenario(self, name: str) -> list[str]:
        r = subprocess.run([str(self.bin), name], text=True, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, f"probe failed: {r.stderr}")
        return r.stdout.strip().splitlines()

    def test_second_restart_while_starting_is_refused(self):
        out = self.run_scenario("restart-twice-while-starting")
        self.assertIn("claim1=accepted(1)", out)
        self.assertIn("claim2=rejectedAlreadyQueued", out)
        # Exactly one decision proceeds.
        self.assertEqual(1, sum(1 for line in out if line.startswith("claim") and "accepted" in line))
        self.assertIn("launch1=launched", out)

    def test_second_restart_while_waiting_is_refused(self):
        out = self.run_scenario("restart-twice-while-waiting")
        self.assertIn("phase=waiting", out)
        self.assertIn("claim2=rejectedAlreadyQueued", out)

    def test_force_before_run_stops_the_launch(self):
        out = self.run_scenario("force-before-run")
        self.assertIn("force=cancelledBeforeLaunch", out)
        # Nothing had launched, so there is nothing to signal.
        self.assertIn("queued=nil", out)
        self.assertIn("terminated=0", out)
        # THE regression pin: the queued waiter must not run after a Force.
        self.assertIn("launch1=aborted", out)
        self.assertNotIn("SIDE-EFFECT-RAN", out)

    def test_force_while_waiting_terminates_the_queued_restart(self):
        out = self.run_scenario("force-while-waiting")
        self.assertIn("launch1=launched", out)
        self.assertIn("force=cancelledWhileWaiting", out)
        self.assertIn("terminated=1 waited=1", out)
        self.assertIn("phase=idle", out)

    def test_force_after_killing_is_declined(self):
        out = self.run_scenario("force-after-killing")
        self.assertIn("force=rejectedPastKill", out)
        self.assertIn("queued=nil", out)
        # The phase must survive the refusal: a relaunch is already underway.
        self.assertEqual(["phase=killing", "force=rejectedPastKill", "queued=nil", "phase=killing"], out)

    def test_restart_while_killing_is_refused_distinctly(self):
        out = self.run_scenario("restart-while-killing")
        self.assertIn("claim2=rejectedPastKill", out)

    def test_nudge_does_not_fire_for_a_superseded_epoch(self):
        out = self.run_scenario("nudge-not-after-force")
        self.assertIn("nudge-before=true", out)
        self.assertIn("nudge-after=false", out)

    def test_superseded_finish_does_not_reset_a_live_claim(self):
        out = self.run_scenario("superseded-finish-does-not-reset")
        self.assertIn("e1=1 e3=3", out)
        # A stale waiter completing must not idle the claim that replaced it.
        self.assertIn("phase=starting", out)

    def test_launch_failure_releases_the_lifecycle(self):
        out = self.run_scenario("launch-failure-resets")
        self.assertIn("launch1=failed(boom)", out)
        self.assertIn("phase=idle", out)
        self.assertIn("claim2=accepted(2)", out)


if __name__ == "__main__":
    unittest.main()
