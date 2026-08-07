// Restart lifecycle state machine for the menu-bar graceful restart.
// Extracted from main.swift so the click interleavings are executable in tests.
import Foundation

/// A launched restart the coordinator may have to cancel. `Process` conforms as-is;
/// tests substitute a recorder.
protocol RestartCancellable: AnyObject {
    func terminate()
    func waitUntilExit()
}

extension Process: RestartCancellable {}

/// Outcome of a "Restart Core CLI" click.
enum RestartClaim: Equatable {
    case accepted(epoch: Int)
    case rejectedAlreadyQueued
    case rejectedPastKill
}

/// Outcome of the launch gate. `.aborted` means a Force superseded us before `run()`.
enum LaunchOutcome: Equatable {
    case launched
    case aborted
    case failed(String)
}

/// What a "Force Restart" click found, so the caller can message and cancel.
enum ForceClaim: Equatable {
    case rejectedPastKill
    case cancelledBeforeLaunch
    case cancelledWhileWaiting
    case nothingToCancel
}

final class RestartCoordinator {
    enum Phase { case idle, starting, waiting, killing }

    private var phase: Phase = .idle
    /// Bumped on every claim AND every cancel, so an in-flight closure whose epoch
    /// no longer matches knows it was superseded and must not run.
    private var epoch = 0
    private var tracked: RestartCancellable?
    private let lock = NSLock()

    /// Test-only observation. Never branch on this in production code.
    var currentPhase: Phase {
        lock.lock(); defer { lock.unlock() }
        return phase
    }

    /// Refuse a second concurrent waiter: a single tracked slot would otherwise be
    /// overwritten and the first waiter left live and untrackable.
    func claimRestart() -> RestartClaim {
        lock.lock(); defer { lock.unlock() }
        guard phase == .idle else {
            return phase == .killing ? .rejectedPastKill : .rejectedAlreadyQueued
        }
        epoch += 1
        phase = .starting
        return .accepted(epoch: epoch)
    }

    /// Publish the process before `run()` so a Force in that window can stop it.
    func track(_ proc: RestartCancellable, epoch: Int) {
        lock.lock(); defer { lock.unlock() }
        guard self.epoch == epoch else { return }
        tracked = proc
    }

    /// THE force-before-run gate. `body` runs INSIDE the lock on purpose: checking
    /// the epoch and launching must be one atomic step or the race reopens.
    func launch(epoch: Int, _ body: () throws -> Void) -> LaunchOutcome {
        lock.lock()
        guard self.epoch == epoch, phase == .starting else {
            lock.unlock()
            return .aborted
        }
        do {
            try body()
        } catch {
            phase = .idle
            tracked = nil
            lock.unlock()
            return .failed(error.localizedDescription)
        }
        phase = .waiting
        lock.unlock()
        return .launched
    }

    /// True only while OUR waiter is still on the quiet gate.
    func nudgeApplies(epoch: Int) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return self.epoch == epoch && phase == .waiting
    }

    /// Past this point the script has exec'd the relaunch; Force must not signal.
    func enterKilling(epoch: Int) {
        lock.lock(); defer { lock.unlock() }
        guard self.epoch == epoch else { return }
        phase = .killing
    }

    /// Only OUR epoch may reset the lifecycle; a Force already moved it on.
    func finish(epoch: Int, proc: RestartCancellable?) {
        lock.lock(); defer { lock.unlock() }
        if self.epoch == epoch { phase = .idle }
        if let proc = proc, tracked === proc { tracked = nil }
    }

    /// Bumping the epoch is what stops a `.starting` waiter that has not called
    /// `run()` yet: `isRunning` is false there, so an isRunning check misses it.
    func claimForce() -> (claim: ForceClaim, queued: RestartCancellable?) {
        lock.lock()
        let found = phase
        let queued = tracked
        if found == .killing {
            lock.unlock()
            return (.rejectedPastKill, nil)
        }
        epoch += 1
        phase = .idle
        tracked = nil
        lock.unlock()
        switch found {
        case .starting: return (.cancelledBeforeLaunch, nil)
        case .waiting: return (.cancelledWhileWaiting, queued)
        case .idle, .killing: return (.nothingToCancel, nil)
        }
    }
}
