/**
 * emit-call-tiers.ts — the core-advertise half of the availability-driven
 * call-tier menu (Track 9). Covers tier composition from reachability detection
 * and the state-file write that `sutando-config.sh runtime` folds into the
 * descriptor's `call_tiers`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, rmSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { composeCallTiers, emitCallTiers, parseReemitInterval, type CallTiersFile } from '../src/emit-call-tiers.ts';

// composeCallTiers reads process.env (SUTANDO_LAN_SHARE) via reachability-endpoints.
// With LAN sharing OFF (default in CI), both direct tiers are unreachable — the
// exact "nothing to offer -> client falls back to cloud" default.
test('composeCallTiers: both direct tiers present, unreachable when LAN-share off', () => {
	const prev = process.env.SUTANDO_LAN_SHARE;
	delete process.env.SUTANDO_LAN_SHARE;
	try {
		const tiers = composeCallTiers();
		assert.equal(tiers.length, 2);
		const byTier = Object.fromEntries(tiers.map((t) => [t.tier, t]));
		assert.deepEqual(Object.keys(byTier).sort(), ['direct-lan', 'direct-tailnet']);
		for (const t of tiers) {
			assert.equal(t.url, null, `${t.tier} url should be null when LAN-share off`);
			assert.equal(t.reachable, false, `${t.tier} unreachable when url null`);
			assert.equal(typeof t.label, 'string');
			assert.ok(t.label.length > 0);
		}
	} finally {
		if (prev !== undefined) process.env.SUTANDO_LAN_SHARE = prev;
	}
});

test('reachable is exactly (url !== null) — the invariant the client binds to', () => {
	for (const t of composeCallTiers()) {
		assert.equal(t.reachable, t.url !== null);
	}
});

test('emitCallTiers writes a parseable state file with the descriptor shape', () => {
	const dir = mkdtempSync(join(tmpdir(), 'call-tiers-'));
	const dest = join(dir, 'call-tiers.json');
	try {
		const written = emitCallTiers(dest);
		assert.equal(written, dest);
		const rec = JSON.parse(readFileSync(dest, 'utf8')) as CallTiersFile;
		assert.equal(typeof rec.ts, 'number');
		assert.equal(typeof rec.pid, 'number');
		assert.ok(Array.isArray(rec.call_tiers));
		assert.equal(rec.call_tiers.length, 2);
		// Every entry carries the four keys the client renders from.
		for (const t of rec.call_tiers) {
			assert.deepEqual(Object.keys(t).sort(), ['label', 'reachable', 'tier', 'url']);
		}
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('emitCallTiers overwrites in place — re-emit advances ts (freshness for the resident loop)', () => {
	const dir = mkdtempSync(join(tmpdir(), 'call-tiers-'));
	const dest = join(dir, 'call-tiers.json');
	try {
		emitCallTiers(dest);
		const first = JSON.parse(readFileSync(dest, 'utf8')) as CallTiersFile;
		// Freeze a later wall-clock so the second write's ts is strictly greater.
		const realNow = Date.now;
		Date.now = () => realNow() + 5000;
		try {
			emitCallTiers(dest);
		} finally {
			Date.now = realNow;
		}
		const second = JSON.parse(readFileSync(dest, 'utf8')) as CallTiersFile;
		assert.ok(second.ts >= first.ts, 're-emit ts is non-decreasing');
		assert.ok(second.ts > first.ts, 're-emit refreshes ts under a later clock');
		assert.equal(second.call_tiers.length, 2);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('acquireSingleInstance: guard decision over injected probes', async () => {
	const { acquireSingleInstance, releaseSingleInstance } = await import('../src/emit-call-tiers.ts');
	const dir = mkdtempSync(join(tmpdir(), 'call-tiers-pid-'));
	const pidFile = join(dir, 'call-tiers-emitter.pid');
	const check = (alive: boolean, cmd: string) => ({ isAlive: () => alive, cmdline: () => cmd });
	try {
		// No PID file → run, and the slot is claimed with my pid.
		assert.equal(acquireSingleInstance(pidFile, 111, check(true, 'x')), 'run');
		assert.equal(readFileSync(pidFile, 'utf8'), '111');
		// Live owner whose cmdline IS this script → skip.
		assert.equal(acquireSingleInstance(pidFile, 222, check(true, 'node …/emit-call-tiers.ts --interval 60')), 'skip');
		assert.equal(readFileSync(pidFile, 'utf8'), '111', 'skip must not steal the slot');
		// Dead owner → replaced.
		assert.equal(acquireSingleInstance(pidFile, 222, check(false, '')), 'run');
		assert.equal(readFileSync(pidFile, 'utf8'), '222');
		// Alive pid but UNRELATED command line (pid reuse) → replaced, not blocked.
		assert.equal(acquireSingleInstance(pidFile, 333, check(true, '/usr/sbin/somethingelse')), 'run');
		assert.equal(readFileSync(pidFile, 'utf8'), '333');
		// Release only removes when the file still names me.
		releaseSingleInstance(pidFile, 999); // not mine → no-op
		assert.equal(readFileSync(pidFile, 'utf8'), '333');
		releaseSingleInstance(pidFile, 333);
		assert.throws(() => readFileSync(pidFile, 'utf8'), 'released file should be gone');
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('lifecycle regression: two resident launches → exactly one survives (review ask on #2129)', async (t) => {
	const { spawn } = await import('node:child_process');
	const { statusPath } = await import('../src/workspace_default.ts');
	const { fileURLToPath } = await import('node:url');
	const { createRequire } = await import('node:module');
	// Resolve tsx's CLI to an absolute path. CI runs from the repo root after
	// `npm ci`, so the cwd resolution always works there; a bare scratch
	// checkout without node_modules skips (the unit guard tests above still run).
	let tsxCli: string;
	try {
		tsxCli = createRequire(join(process.cwd(), 'package.json')).resolve('tsx/cli');
	} catch {
		t.skip('tsx not resolvable from cwd (no node_modules here) — covered in CI');
		return;
	}
	const pidFile = statusPath('call-tiers-emitter.pid');
	rmSync(pidFile, { force: true });
	// Absolute script path: anchored to this test file, so the spawn works
	// regardless of which directory the suite is invoked from.
	const script = fileURLToPath(new URL('../src/emit-call-tiers.ts', import.meta.url));
	const launch = () => spawn(process.execPath, [tsxCli, script, '--interval', '60'],
		{ stdio: ['ignore', 'pipe', 'pipe'] });
	const waitExit = (p: ReturnType<typeof spawn>, ms: number) => new Promise<number | null>((res) => {
		const t = setTimeout(() => res(null), ms);
		p.on('exit', (code) => { clearTimeout(t); res(code ?? -1); });
	});
	const p1 = launch();
	try {
		// First launch claims the slot and stays resident.
		const first = await waitExit(p1, 4000);
		assert.equal(first, null, 'first emitter must stay resident');
		// Second launch must see the live owner and exit 0 promptly.
		const p2 = launch();
		const second = await waitExit(p2, 8000);
		assert.equal(second, 0, 'second emitter must exit 0 (single-instance skip)');
		assert.equal(p1.exitCode, null, 'first emitter still resident after the second exits');
	} finally {
		p1.kill('SIGTERM');
		await waitExit(p1, 3000);
		rmSync(pidFile, { force: true });
	}
});

test('parseReemitInterval: --interval / --interval= / env, arg wins; junk & absent → one-shot (null)', () => {
	// Absent everywhere → one-shot.
	assert.equal(parseReemitInterval([], {}), null);
	// Space form and = form.
	assert.equal(parseReemitInterval(['node', 'x', '--interval', '60'], {}), 60);
	assert.equal(parseReemitInterval(['node', 'x', '--interval=90'], {}), 90);
	// Env fallback when no arg.
	assert.equal(parseReemitInterval(['node', 'x'], { SUTANDO_CALL_TIERS_INTERVAL_S: '45' }), 45);
	// Arg wins over env.
	assert.equal(parseReemitInterval(['node', 'x', '--interval', '30'], { SUTANDO_CALL_TIERS_INTERVAL_S: '999' }), 30);
	// Non-positive / non-integer / junk → one-shot (null), never a crash.
	assert.equal(parseReemitInterval(['node', 'x', '--interval', '0'], {}), null);
	assert.equal(parseReemitInterval(['node', 'x', '--interval', '-5'], {}), null);
	assert.equal(parseReemitInterval(['node', 'x', '--interval', 'abc'], {}), null);
	assert.equal(parseReemitInterval(['node', 'x', '--interval', '1.5'], {}), null);
	assert.equal(parseReemitInterval(['node', 'x'], { SUTANDO_CALL_TIERS_INTERVAL_S: 'nope' }), null);
});
