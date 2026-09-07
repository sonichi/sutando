/**
 * voice-lock.ts — TS caller of the guarded PID-lock helper
 * (`scripts/voice-lock.py`), used by voice-agent's `acquirePidLock` (impl
 * plan WS1 Step 4, amendments R1/R3/R4).
 *
 * The helper is the SINGLE implementation of the lock transaction; this
 * module only shells out to it. Two normative rules live here:
 *
 * - R4/T1: resolve a smoke-tested absolute interpreter path once. POSIX uses
 *   `sutando-config.sh python-bin`; Windows probes Python with `import msvcrt`.
 * - R3: there is NO unguarded legacy writer. If the interpreter or helper is
 *   unavailable, lock operations FAIL CLOSED with an actionable error — the
 *   caller must not fall back to an in-process bare-pid lock.
 * - R1/S4: exit-time release is NON-BLOCKING (fire-and-forget detached
 *   spawn) — a blocking release can deadlock against the helper that just
 *   TERM'd us; stale metadata is safely replaced by the next guarded
 *   acquisition.
 */

import { spawn, spawnSync } from 'node:child_process';
import type { SpawnSyncReturns } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

export type SpawnSyncFn = (
	cmd: string,
	args: string[],
	opts: { encoding: 'utf-8' },
) => SpawnSyncReturns<string>;

export type PythonResolution =
	| { ok: true; bin: string }
	| { ok: false; detail: string };

/** Resolve the lock helper's interpreter via `sutando-config.sh python-bin`
 * (amendment R4; smoke-tested per T1). Returns a failure — never a guess. */
export function resolveLockPython(
	repoRoot: string = REPO_ROOT,
	spawnImpl: SpawnSyncFn = spawnSync as unknown as SpawnSyncFn,
): PythonResolution {
	if (process.platform === 'win32') {
		const candidates: Array<{ cmd: string; prefix: string[] }> = [];
		if (process.env.SUTANDO_PY) {
			candidates.push({ cmd: process.env.SUTANDO_PY, prefix: [] });
		} else {
			candidates.push(
				{ cmd: 'python', prefix: [] },
				{ cmd: 'py', prefix: ['-3'] },
				{ cmd: 'python3', prefix: [] },
			);
		}
		const failures: string[] = [];
		for (const candidate of candidates) {
			let probe: SpawnSyncReturns<string>;
			try {
				probe = spawnImpl(
					candidate.cmd,
					[...candidate.prefix, '-c', 'import msvcrt,sys;print(sys.executable)'],
					{ encoding: 'utf-8' },
				);
			} catch (e) {
				failures.push(`${candidate.cmd}: ${(e as Error)?.message ?? e}`);
				continue;
			}
			const bin = (probe.stdout ?? '').trim();
			if (!probe.error && probe.status === 0 && bin) {
				return { ok: true, bin };
			}
			failures.push(`${candidate.cmd}: exit ${probe.status}`);
		}
		return {
			ok: false,
			detail: `no usable Windows Python (${failures.join('; ')})`,
		};
	}

	const script = join(repoRoot, 'scripts', 'sutando-config.sh');
	let res: SpawnSyncReturns<string>;
	try {
		res = spawnImpl('/bin/bash', [script, 'python-bin'], { encoding: 'utf-8' });
	} catch (e) {
		return { ok: false, detail: `spawn failed: ${(e as Error)?.message ?? e}` };
	}
	if (res.error) {
		return { ok: false, detail: `spawn failed: ${res.error.message}` };
	}
	const bin = (res.stdout ?? '').trim();
	if (res.status !== 0 || !bin) {
		const stderr = (res.stderr ?? '').trim().split('\n')[0] ?? '';
		return {
			ok: false,
			detail:
				`sutando-config.sh python-bin exited ${res.status}` +
				(stderr ? ` — ${stderr}` : ''),
		};
	}
	return { ok: true, bin };
}

export function voiceLockHelperPath(repoRoot: string = REPO_ROOT): string {
	return join(repoRoot, 'scripts', 'voice-lock.py');
}

export function voiceLockGuardPath(workspaceDir: string): string {
	return join(workspaceDir, '.voice-agent.lock.guard');
}

export type AcquireResult =
	| { status: 'acquired'; lockId?: string }
	| { status: 'held'; holderPid?: number; holderRaw?: string }
	| { status: 'error'; detail: string };

export interface LockCallOpts {
	pidfile: string;
	/** Pre-move root path (#2722): a live record there holds acquisition. */
	legacyPidfile?: string;
	guard: string;
	pid: number;
	pythonBin: string;
	repoRoot?: string;
}

/** Guarded structured-lock acquisition (helper `acquire`). Exit 0 → acquired;
 * exit 7 → held (another live owner); anything else → error (fail closed). */
export function acquireVoiceLock(
	opts: LockCallOpts & { entry: string; workspace: string },
	spawnImpl: SpawnSyncFn = spawnSync as unknown as SpawnSyncFn,
): AcquireResult {
	const helper = voiceLockHelperPath(opts.repoRoot);
	let res: SpawnSyncReturns<string>;
	try {
		res = spawnImpl(
			opts.pythonBin,
			[
				helper,
				'acquire',
				'--pidfile', opts.pidfile,
				'--guard', opts.guard,
				'--pid', String(opts.pid),
				'--entry', opts.entry,
				'--workspace', opts.workspace,
				...(opts.legacyPidfile ? ['--legacy-pidfile', opts.legacyPidfile] : []),
			],
			{ encoding: 'utf-8' },
		);
	} catch (e) {
		return { status: 'error', detail: `helper spawn failed: ${(e as Error)?.message ?? e}` };
	}
	if (res.error) {
		return { status: 'error', detail: `helper spawn failed: ${res.error.message}` };
	}
	if (res.status === 0) {
		// Surface the lock record's per-acquisition lockId for the capability-marker
		// binding. Best-effort: unparseable → undefined (no marker), never a lock failure.
		let lockId: string | undefined;
		try {
			const parsed = JSON.parse((res.stdout ?? '').trim());
			if (typeof parsed?.lock?.lockId === 'string' && parsed.lock.lockId) lockId = parsed.lock.lockId;
		} catch {
			/* token is best-effort */
		}
		return { status: 'acquired', lockId };
	}
	if (res.status === 7) {
		let holderPid: number | undefined;
		try {
			const parsed = JSON.parse((res.stdout ?? '').trim());
			if (typeof parsed?.holder?.pid === 'number') holderPid = parsed.holder.pid;
		} catch {
			/* holder detail is best-effort */
		}
		return { status: 'held', holderPid, holderRaw: (res.stdout ?? '').trim() };
	}
	const stderr = (res.stderr ?? '').trim().split('\n')[0] ?? '';
	return {
		status: 'error',
		detail: `voice-lock.py acquire exited ${res.status}${stderr ? ` — ${stderr}` : ''}`,
	};
}

/** The exit-listener body (amendment R1): the fatal path must NEVER invoke
 * the Python release helper — `process.exit()` runs synchronous `exit`
 * listeners, and a helper invocation there can block on the guard. Returns
 * whether the release was attempted (false = suppressed by the fatal flag);
 * the stale structured lock is left for the supervisor to replace safely
 * under the guard. */
export function releaseOnExitUnlessFatal(
	opts: LockCallOpts,
	isFatal: () => boolean,
	spawnImpl: typeof spawn = spawn,
): boolean {
	if (isFatal()) return false;
	releaseVoiceLockNonBlocking(opts, spawnImpl);
	return true;
}

/** Fire-and-forget guarded release (helper `release`) — non-blocking per
 * amendments R1/S4. The fork/exec happens synchronously inside spawn(), so
 * the helper is created even from a `process.on('exit')` listener; we never
 * wait on it. */
export function releaseVoiceLockNonBlocking(
	opts: LockCallOpts,
	spawnImpl: typeof spawn = spawn,
): void {
	const helper = voiceLockHelperPath(opts.repoRoot);
	try {
		const child = spawnImpl(
			opts.pythonBin,
			[
				helper,
				'release',
				'--pidfile', opts.pidfile,
				'--guard', opts.guard,
				'--pid', String(opts.pid),
			],
			{ detached: true, stdio: 'ignore' },
		);
		child.unref();
	} catch {
		/* best-effort — stale metadata is replaced by the next guarded acquire */
	}
}
