/**
 * Crash-only fatal path + exit-7 duplicate-instance semantics + structured
 * lock acquisition (impl plan WS1 Steps 1, 2, 4; amendments R1/R2/R3/R4).
 *
 * Unit: `writeCrashRecordAndExit` (design 1d cases — throwing getters, failing
 * fs write, plain Error), centralized exit classification (R2), the one-shot
 * fatal guard, and the R1 exit-listener seam (`releaseOnExitUnlessFatal` —
 * the release helper never runs on the fatal path).
 *
 * Integration (spawned agents against a temp workspace):
 *  - duplicate lock: loser exits 7 with the FATAL line, winner unaffected,
 *    lock file is structured JSON naming the winner (Steps 2+4);
 *  - EADDRINUSE reaches exit 7 through `main().catch` (R2's second path),
 *    with the R1 release suppression pinned on THIS path too (the loser's
 *    own lock is left in place — the exit-time Python release is skipped);
 *  - EADDRINUSE reaches exit 7 through the uncaughtException handler
 *    directly (R2's FIRST path, driven by test-only fault injection);
 *  - a non-EADDRINUSE `main()` failure exits 1 through the bounded crash
 *    record with static-string logging only (design 1d), release suppressed;
 *  - unusable lock-helper interpreter ⇒ FAIL CLOSED with an actionable error,
 *    no unguarded bare-pid lock is ever written (R3/R4/T1).
 */

import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	classifyFatalExitCode,
	writeCrashRecordAndExit,
	markFatalExit,
	isFatalExit,
	resetFatalExitForTest,
	EXIT_CODE_DUPLICATE_INSTANCE,
} from '../src/crash-only.js';
import { releaseOnExitUnlessFatal, resolveLockPython } from '../src/voice-lock.js';

const REPO_ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..');

// ---------------------------------------------------------------------------
// Unit: crash record + exit classification
// ---------------------------------------------------------------------------

describe('writeCrashRecordAndExit (design 1d)', () => {
	it('plain Error: writes bounded JSON and exits 1 via the outermost finally', () => {
		let exited: number | null = null;
		let written = '';
		writeCrashRecordAndExit(new RangeError('boom'), '/nonexistent/crash.json', {
			exit: (code) => { exited = code; },
			mkdir: () => {},
			fdOpen: () => 42,
			fdWrite: (_fd, text) => { written += text; },
			fdClose: () => {},
			now: () => 1234,
			pid: 999,
		});
		assert.equal(exited, 1);
		const rec = JSON.parse(written);
		assert.deepEqual(rec, { name: 'RangeError', message: 'boom', at: 1234, pid: 999 });
	});

	it('bounds name to 64 and message to 512 chars', () => {
		let written = '';
		class VeryLongName extends Error {}
		Object.defineProperty(VeryLongName, 'name', { value: 'N'.repeat(200) });
		writeCrashRecordAndExit(new VeryLongName('m'.repeat(2000)), '/x', {
			exit: () => {},
			mkdir: () => {},
			fdOpen: () => 1,
			fdWrite: (_fd, t) => { written += t; },
			fdClose: () => {},
		});
		const rec = JSON.parse(written);
		assert.equal(rec.name.length, 64);
		assert.equal(rec.message.length, 512);
	});

	it('error whose message getter throws: still exits promptly, record keeps the fallback', () => {
		let exited: number | null = null;
		let written = '';
		const evil: Record<string, unknown> = {};
		Object.defineProperty(evil, 'message', { get() { throw new Error('getter bomb'); } });
		Object.defineProperty(evil, 'stack', { get() { throw new Error('stack bomb'); } });
		writeCrashRecordAndExit(evil, '/x', {
			exit: (code) => { exited = code; },
			mkdir: () => {},
			fdOpen: () => 1,
			fdWrite: (_fd, t) => { written += t; },
			fdClose: () => {},
		});
		assert.equal(exited, 1);
		const rec = JSON.parse(written);
		assert.equal(rec.message, '');
		assert.equal(typeof rec.name, 'string');
	});

	it('fs write path throwing (injected fdOpen failure): exit still called', () => {
		let exited: number | null = null;
		writeCrashRecordAndExit(new Error('x'), '/x', {
			exit: (code) => { exited = code; },
			mkdir: () => {},
			fdOpen: () => { throw new Error('EACCES'); },
		});
		assert.equal(exited, 1);
	});

	it('exit runs even when everything injected throws', () => {
		let exited: number | null = null;
		writeCrashRecordAndExit(new Error('x'), '/x', {
			exit: (code) => { exited = code; },
			mkdir: () => { throw new Error('nope'); },
			fdOpen: () => 3,
			fdWrite: () => { throw new Error('nope'); },
			fdClose: () => { throw new Error('nope'); },
		});
		assert.equal(exited, 1);
	});
});

describe('centralized exit classification (amendment R2)', () => {
	it('EADDRINUSE classifies as exit 7 (duplicate-instance semantics)', () => {
		const err = Object.assign(new Error('listen EADDRINUSE'), { code: 'EADDRINUSE' });
		assert.equal(classifyFatalExitCode(err), EXIT_CODE_DUPLICATE_INSTANCE);
	});
	it('everything else classifies as exit 1 — including a throwing code getter', () => {
		assert.equal(classifyFatalExitCode(new Error('plain')), 1);
		assert.equal(classifyFatalExitCode(undefined), 1);
		const evil = {};
		Object.defineProperty(evil, 'code', { get() { throw new Error('bomb'); } });
		assert.equal(classifyFatalExitCode(evil), 1);
	});
});

describe('one-shot fatal guard + R1 release suppression', () => {
	it('markFatalExit is one-shot: first call returns false, second true', () => {
		resetFatalExitForTest();
		assert.equal(isFatalExit(), false);
		assert.equal(markFatalExit(), false);
		assert.equal(isFatalExit(), true);
		assert.equal(markFatalExit(), true);
		resetFatalExitForTest();
	});

	it('releaseOnExitUnlessFatal never spawns the release helper on the fatal path', () => {
		let spawned = 0;
		const fakeSpawn = (() => { spawned += 1; return { unref() {} }; }) as unknown as typeof spawn;
		const opts = { pidfile: '/p', guard: '/g', pid: 1, pythonBin: '/py' };
		assert.equal(releaseOnExitUnlessFatal(opts, () => true, fakeSpawn), false);
		assert.equal(spawned, 0, 'fatal path must not invoke the Python release helper');
		assert.equal(releaseOnExitUnlessFatal(opts, () => false, fakeSpawn), true);
		assert.equal(spawned, 1, 'non-fatal exit releases (non-blocking)');
	});
});

describe('resolveLockPython (amendment R4 — injectable resolver)', () => {
	it('maps a non-zero python-bin exit to a fail-closed error', () => {
		const fake = (() => ({ status: 1, stdout: '', stderr: 'no usable python3' })) as never;
		const res = resolveLockPython(REPO_ROOT, fake);
		assert.equal(res.ok, false);
	});
	it('returns the smoke-tested absolute path on success', () => {
		const fake = (() => ({ status: 0, stdout: '/opt/py/bin/python3\n', stderr: '' })) as never;
		const res = resolveLockPython(REPO_ROOT, fake);
		assert.deepEqual(res, { ok: true, bin: '/opt/py/bin/python3' });
	});
});

// ---------------------------------------------------------------------------
// Integration: spawned agents
// ---------------------------------------------------------------------------

const FAKE_KEY = 'test-fake-gemini-key-0123456789abcdef';
const children: ChildProcess[] = [];
const tempDirs: string[] = [];

function makeWorkspace(tag: string): string {
	const ws = mkdtempSync(join(tmpdir(), `voice-crash-only-${tag}-`));
	tempDirs.push(ws);
	return ws;
}

interface AgentHandle {
	child: ChildProcess;
	stderr: () => string;
	stdout: () => string;
	exited: Promise<number | null>;
}

function spawnAgent(
	ws: string,
	port: number,
	visionPort: number,
	extraEnv: Record<string, string> = {},
	testMode = true,
): AgentHandle {
	// detached: own process group, so cleanup can kill npx + tsx worker
	// together (the worker, not the npx parent, holds the lock and the port).
	const env: NodeJS.ProcessEnv = {
		...process.env,
		SUTANDO_WORKSPACE: ws,
		GEMINI_VOICE_API_KEY: FAKE_KEY,
		PORT: String(port),
		VISION_CONTROL_PORT: String(visionPort),
		...extraEnv,
	};
	if (testMode) env.SUTANDO_TEST_MODE = '1';
	else delete env.SUTANDO_TEST_MODE;
	const tsxCli = join(REPO_ROOT, 'node_modules', 'tsx', 'dist', 'cli.mjs');
	const child = spawn(process.execPath, [tsxCli, 'src/voice-agent.ts'], {
		cwd: REPO_ROOT,
		detached: true,
		env,
		stdio: ['ignore', 'pipe', 'pipe'],
	});
	children.push(child);
	let out = '';
	let err = '';
	child.stdout?.on('data', (c) => { out += String(c); });
	child.stderr?.on('data', (c) => { err += String(c); });
	const exited = new Promise<number | null>((resolve) => {
		child.once('exit', (code) => resolve(code));
	});
	return { child, stderr: () => err, stdout: () => out, exited };
}

async function waitFor(cond: () => boolean, ms: number, what: string): Promise<void> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		if (cond()) return;
		await delay(150);
	}
	throw new Error(`timed out waiting for ${what}`);
}

async function killAndWait(child: ChildProcess): Promise<void> {
	// Kill the whole detached process group — the tsx WORKER (not the npx
	// parent we spawned) holds the lock and the port.
	try {
		if (child.pid) {
			if (process.platform === 'win32') {
				execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
			} else {
				process.kill(-child.pid, 'SIGKILL');
			}
		}
	} catch { /* gone */ }
	if (child.exitCode !== null || child.signalCode !== null) return;
	await new Promise<void>((resolve) => {
		const hard = setTimeout(() => { try { child.kill('SIGKILL'); } catch {} resolve(); }, 2000);
		child.once('exit', () => { clearTimeout(hard); resolve(); });
		try { child.kill('SIGKILL'); } catch { clearTimeout(hard); resolve(); }
	});
}

function processArgv(pid: number): string {
	if (process.platform === 'win32') {
		return execFileSync('powershell.exe', [
			'-NoProfile',
			'-NonInteractive',
			'-Command',
			`(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`,
		], { encoding: 'utf-8' });
	}
	return execFileSync('ps', ['-p', String(pid), '-o', 'args='], { encoding: 'utf-8' });
}

describe('voice-agent duplicate-instance + fail-closed lock (integration)', () => {
	after(async () => {
		for (const c of children) await killAndWait(c);
		for (const d of tempDirs) rmSync(d, { recursive: true, force: true });
	});

	it('non-test launch refuses unsupported platforms', {
		skip: process.platform === 'darwin' || process.platform === 'win32',
	}, async () => {
		const ws = makeWorkspace('platform');
		const agent = spawnAgent(ws, 19921, 19922, {}, false);
		const code = await agent.exited;
		assert.equal(code, 1);
		assert.match(agent.stderr(), /Sutando requires macOS/);
	});

	it('duplicate spawn: loser exits 7 with the FATAL line; winner keeps a structured lock', async () => {
		const ws = makeWorkspace('dup');
		const pidfile = join(ws, 'state', 'locks', 'voice-agent.pid');
		// #2722 self-healing migration: a pre-move root copy is retired on acquisition.
		writeFileSync(join(ws, '.voice-agent.pid'), '{"stale":"pre-move"}');
		const winner = spawnAgent(ws, 19931, 19932);
		// The lock is written before any side effect — poll for it, then race
		// the loser against the SAME workspace (different ports: the loser must
		// die at the LOCK, not at the port bind).
		await waitFor(() => existsSync(pidfile), 90_000, 'winner lock file');
		assert.equal(
			existsSync(join(ws, '.voice-agent.pid')),
			false,
			'legacy root copy retired on acquisition (#2722 shim)',
		);
		const lock = JSON.parse(readFileSync(pidfile, 'utf-8'));
		assert.equal(lock.v, 1, 'lock is structured schema v1');
		// `npx tsx` spawns a worker: the lock names the WORKER (the process
		// that ran acquirePidLock — dev tsx parent/worker topology, amendment
		// Z1), which is a live descendant running the voice-agent entry.
		assert.equal(typeof lock.pid, 'number');
		const argv = processArgv(lock.pid);
		assert.match(argv, /voice-agent/, `lock pid ${lock.pid} runs the voice-agent entry`);
		assert.match(String(lock.lockId), /^vl1-/, 'lock carries a lockId (amendment R3)');
		assert.equal(typeof lock.startTimeMs, 'number');
		assert.match(String(lock.entry), /voice-agent/);

		const loser = spawnAgent(ws, 19933, 19934);
		const loserCode = await loser.exited;
		assert.equal(loserCode, 7, `loser must exit 7; stderr: ${loser.stderr()}`);
		assert.match(loser.stderr(), /FATAL: voice-agent already running \(pid \d+\)/);

		// Winner unaffected: still alive, lock unchanged.
		assert.equal(winner.child.exitCode, null, 'winner still running');
		const lockAfter = JSON.parse(readFileSync(pidfile, 'utf-8'));
		assert.equal(lockAfter.lockId, lock.lockId, 'winner lock untouched by the loser');
		await killAndWait(winner.child);
	});

	it('EADDRINUSE reaches exit 7 through main().catch (amendment R2)', async () => {
		const wsA = makeWorkspace('porta');
		const wsB = makeWorkspace('portb');
		const port = 19941;
		const a = spawnAgent(wsA, port, 19942);
		// Wait until A owns the WS port (its runtime state or just the lock +
		// some boot progress; poll the port itself).
		await waitFor(() => a.stdout().includes(`ws://localhost:${port}`) || a.stdout().includes('Voice agent'), 90_000, 'agent A listening');
		// B: different workspace (lock acquisition succeeds), same WS port.
		const b = spawnAgent(wsB, port, 19943);
		const bCode = await b.exited;
		assert.equal(bCode, 7, `port loser must exit 7; stderr: ${b.stderr()} stdout: ${b.stdout()}`);
		assert.match(b.stdout() + b.stderr(), /already in use|EADDRINUSE/);
		// R1 release suppression pinned on the main().catch path too: this is
		// a FATAL exit, so the exit-time Python release helper is skipped and
		// the loser's own (now stale) lock stays for the supervisor to replace
		// under the guard — identical crash-only rules on both fatal paths.
		assert.equal(
			existsSync(join(wsB, 'state', 'locks', 'voice-agent.pid')),
			true,
			'main().catch fatal must NOT release the lock (amendment R1)',
		);
		await killAndWait(a.child);
	});

	it('non-EADDRINUSE main() failure: bounded crash record, static log, release suppressed (1d/R1/R2)', async () => {
		const ws = makeWorkspace('mainfatal');
		const agent = spawnAgent(ws, 19961, 19962, { SUTANDO_TEST_FAIL_MAIN: 'boom-from-main' });
		const code = await agent.exited;
		assert.equal(code, 1, `generic fatal exits 1; stderr: ${agent.stderr()}`);
		// Static-string logging only — never `console.error('Fatal:', err)`
		// (object inspection inside error reporting was the 2026-08-04 spin).
		assert.match(agent.stderr(), /\[FATAL\] main\(\) failed — crash-only exit/);
		assert.doesNotMatch(agent.stderr(), /Fatal: Error/, 'no object-inspected err dump');
		// Bounded primitive-only crash record, same file as the uncaught path.
		const rec = JSON.parse(readFileSync(join(ws, 'logs', 'voice-agent.crash.json'), 'utf-8'));
		assert.equal(rec.name, 'Error');
		assert.equal(rec.message, 'boom-from-main');
		assert.equal(typeof rec.at, 'number');
		assert.equal(typeof rec.pid, 'number');
		// R1: the acquired lock is left in place (release helper suppressed).
		assert.equal(
			existsSync(join(ws, 'state', 'locks', 'voice-agent.pid')),
			true,
			'fatal main() exit must NOT release the lock (amendment R1)',
		);
	});

	it('EADDRINUSE reaches exit 7 through the uncaughtException handler directly (amendment R2)', async () => {
		const ws = makeWorkspace('uncaught');
		const agent = spawnAgent(ws, 19971, 19972, { SUTANDO_TEST_RAISE_UNCAUGHT: 'EADDRINUSE' });
		// The injection fires only AFTER the fatal handlers are installed
		// (post-session-construction), so this pins onFatal — not main().catch.
		const code = await agent.exited;
		assert.equal(code, 7, `uncaught EADDRINUSE must exit 7; stderr: ${agent.stderr()} stdout: ${agent.stdout()}`);
		assert.match(agent.stderr(), /\[FATAL\] EADDRINUSE on :\d+ — another voice-agent is listening/);
		// The EADDRINUSE branch is static-string only — no crash record.
		assert.equal(existsSync(join(ws, 'logs', 'voice-agent.crash.json')), false, 'duplicate-instance exit writes no crash record');
		// And the fatal flag suppresses the exit-time release here too (R1).
		assert.equal(
			existsSync(join(ws, 'state', 'locks', 'voice-agent.pid')),
			true,
			'uncaught fatal must NOT release the lock (amendment R1)',
		);
	});

	it('unusable interpreter: lock ops fail closed, no bare-pid fallback lock (R3/R4/T1)', async () => {
		const ws = makeWorkspace('failclosed');
		// A broken "interpreter": executable, but cannot run the smoke test —
		// sutando-config.sh python-bin (tier 1: SUTANDO_PY) must reject it and
		// the agent must fail closed instead of writing a legacy bare-pid lock.
		const brokenPy = join(ws, 'broken-python3');
		writeFileSync(brokenPy, '#!/bin/sh\nexit 47\n');
		chmodSync(brokenPy, 0o755);
		const agent = spawnAgent(ws, 19951, 19952, { SUTANDO_PY: brokenPy });
		const code = await agent.exited;
		assert.notEqual(code, 0, 'agent must not boot');
		assert.equal(code, 1, `fail-closed exit is 1, got ${code}; stderr: ${agent.stderr()}`);
		assert.match(agent.stderr(), /fail closed/i);
		assert.match(agent.stderr(), /python/i);
		assert.equal(existsSync(join(ws, 'state', 'locks', 'voice-agent.pid')), false, 'no unguarded legacy lock writer (amendment R3)');
	});
});
