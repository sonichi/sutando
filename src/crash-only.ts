/**
 * crash-only.ts — voice-agent fatal-path helpers (design 1d; impl plan WS1
 * Steps 1–2, amendments R1/R2).
 *
 * The 2026-08-04 incident profile showed the wedge lived *inside* error
 * reporting (`TriggerUncaughtException` → `InspectorConsoleCall` /
 * `ErrorStackGetter`) — the crash-*reporting* path itself was the spin. So the
 * fatal path here must not depend on the machinery that caused the incident:
 * no logger, no `console.error(err)` (object inspection), no recorder, no
 * SQLite. Termination is the invariant: `exit()` runs in the outermost
 * `finally` regardless of any failure above it.
 *
 * Exit-code classification is centralized (amendment R2): EADDRINUSE means
 * "another instance owns the singleton resource; my exit is the expected
 * outcome of a race" → exit 7 (duplicate-instance semantics, same code as the
 * duplicate-lock exit) on BOTH the uncaught-handler path and `main().catch`.
 * Everything else fatal → exit 1.
 *
 * The one-shot fatal flag doubles as amendment R1's release-suppressor: the
 * `process.on('exit')` lock-release listener must skip the Python helper when
 * a fatal fired (the helper can block on the guard; a stale structured lock is
 * left for the supervisor to replace safely).
 */

import { openSync, writeSync, closeSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

/** Exit code for "another instance owns the singleton resource" (duplicate
 * lock or duplicate port). The supervisor's exit-7 grace window keys on it. */
export const EXIT_CODE_DUPLICATE_INSTANCE = 7;

let fatalFired = false;

/** True once a fatal handler has fired — the exit-time lock release must skip
 * the Python helper under it (amendment R1). */
export function isFatalExit(): boolean {
	return fatalFired;
}

/** Mark the fatal flag. Returns the PREVIOUS value, so callers implement the
 * one-shot guard ("a second fatal while handling the first goes straight to
 * process.exit(1)") with a single call. */
export function markFatalExit(): boolean {
	const prev = fatalFired;
	fatalFired = true;
	return prev;
}

/** Test-only reset for the module-level one-shot flag. */
export function resetFatalExitForTest(): void {
	fatalFired = false;
}

/** Centralized exit classification (amendment R2): EADDRINUSE → 7, else 1.
 * Reads `err.code` inside try/catch — even property getters can throw. */
export function classifyFatalExitCode(err: unknown): number {
	try {
		if ((err as NodeJS.ErrnoException)?.code === 'EADDRINUSE') {
			return EXIT_CODE_DUPLICATE_INSTANCE;
		}
	} catch {
		/* a throwing getter is just a plain fatal */
	}
	return 1;
}

export interface CrashRecordDeps {
	/** Injected exit — production passes `(c) => process.exit(c)`. */
	exit: (code: number) => void;
	/** Injected fd open for write-failure tests. Defaults to fs.openSync. */
	fdOpen?: (path: string, flags: string) => number;
	fdWrite?: (fd: number, text: string) => void;
	fdClose?: (fd: number) => void;
	mkdir?: (dir: string) => void;
	now?: () => number;
	pid?: number;
}

/**
 * Write a bounded, primitive-only crash record and exit non-zero.
 *
 * Record shape (overwritten each crash): `{"name": <constructor name ≤64>,
 * "message": <String(message) ≤512>, "at": Date.now(), "pid": process.pid}`.
 * `name` and `message` are each extracted inside their OWN try/catch (getters
 * can throw — a design-mandated test case). Never touches `err.stack`, never
 * `util.inspect`, only `fs.openSync`/`writeSync`/`closeSync`.
 *
 * `exit(1)` runs in the outermost `finally` so termination is the invariant
 * regardless of any failure above.
 */
export function writeCrashRecordAndExit(
	err: unknown,
	crashPath: string,
	deps: CrashRecordDeps,
): void {
	try {
		let name = 'Unknown';
		try {
			const n = (err as { constructor?: { name?: unknown } })?.constructor?.name;
			if (n !== undefined && n !== null) name = String(n).slice(0, 64);
		} catch {
			/* throwing getter — keep the fallback */
		}
		let message = '';
		try {
			const m = (err as { message?: unknown })?.message;
			if (m !== undefined && m !== null) message = String(m).slice(0, 512);
		} catch {
			/* throwing getter — keep the fallback */
		}
		const record = JSON.stringify({
			name,
			message,
			at: deps.now ? deps.now() : Date.now(),
			pid: deps.pid ?? process.pid,
		});
		try {
			(deps.mkdir ?? ((d: string) => mkdirSync(d, { recursive: true })))(dirname(crashPath));
		} catch {
			/* best-effort — the open below will fail and we still exit */
		}
		const open = deps.fdOpen ?? ((p: string, f: string) => openSync(p, f));
		const write = deps.fdWrite ?? ((fd: number, text: string) => writeSync(fd, text));
		const close = deps.fdClose ?? ((fd: number) => closeSync(fd));
		const fd = open(crashPath, 'w');
		try {
			write(fd, record + '\n');
		} finally {
			close(fd);
		}
	} catch {
		/* the record is best-effort; termination is not */
	} finally {
		deps.exit(1);
	}
}
