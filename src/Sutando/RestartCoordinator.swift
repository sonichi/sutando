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


/// argv for graceful-restart.sh.
enum GracefulRestartInvocation {
    /// Rehearsal skips only the kill — prep still runs for real, sync included.
    /// `--dry-run` must precede `--`: that is where the script stops reading its own
    /// flags, so a trailing one is passed through to start-cli.sh and silently ignored.
    static func args(script: String, env: [String: String],
                     extra: [String] = []) -> [String] {
        // `extra` rides after `--`, which is how the shell layer forwards flags
        // (a runtime switch adds --runtime <name>); empty keeps the old vector.
        let rehearse = (env["SUTANDO_RESTART_REHEARSE"] ?? "") == "1"
        return rehearse ? [script, "--dry-run", "--"] + extra + ["--visible"]
                        : [script, "--"] + extra + ["--visible"]
    }

    /// The other half of the same contract: what each documented exit status
    /// means. nil = the caller decides (143 cancel, or an undocumented failure).
    static func outcomeMessage(for status: Int32) -> String? {
        switch status {
        case 0:
            return "Core restarted. Attach via Open Core CLI in menu."
        case 3:
            return "Restart stopped before the kill — prep failed, "
                 + "so the core is STILL RUNNING. Nothing was killed."
        case 4:
            // The lock is released on every exit, dry runs included, so a
            // lingering one means a crashed holder — reaped at LOCK_STALE_S.
            return "Another restart is already in progress — this one deferred. "
                 + "(A lock left by a crashed run is reaped after 15 min.)"
        case 5:
            // Rehearsal reaches the same success path having killed nothing,
            // so exit 0 here would report an action that did not happen.
            return "Rehearsal only (SUTANDO_RESTART_REHEARSE=1) — the core was "
                 + "NOT restarted and nothing was killed. Prep did run."
        default:
            return nil
        }
    }
}
