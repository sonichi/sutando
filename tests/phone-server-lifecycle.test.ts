// The phone server's lifecycle rules (P1-19): a restart never drops a live call,
// ngrok is respawned with backoff, and /health says enough for a supervisor to
// judge staleness without `ps`.
// Run: npx tsx --test --test-force-exit tests/phone-server-lifecycle.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const { healthPayload, isDrainBlocked, drainMayExit, ngrokRespawnDelayMs, DRAIN_CAP_MS, RespawnScheduler } =
	await import('../skills/phone-conversation/scripts/server-lifecycle.js');

describe('health', () => {
	it('reports the start time and the bundle it runs, with a null mtime for a missing bundle', () => {
		const dir = mkdtempSync(join(tmpdir(), 'phone-health-'));
		const bundle = join(dir, 'conversation-server.js');
		writeFileSync(bundle, '// bundle');
		const h = healthPayload({ activeCalls: 2, webhookUrl: 'https://x.ngrok.app', startedAt: 123, bundlePath: bundle }) as { status: string; activeCalls: number; startedAt: number; bundle: { path: string; mtimeMs: number | null } };
		assert.equal(h.status, 'ok');
		assert.equal(h.activeCalls, 2);
		assert.equal(h.startedAt, 123);
		assert.equal(h.bundle.path, bundle);
		assert.equal(typeof h.bundle.mtimeMs, 'number');
		const missing = healthPayload({ activeCalls: 0, webhookUrl: '', startedAt: 1, bundlePath: join(dir, 'nope.js') }) as { bundle: { mtimeMs: number | null } };
		assert.equal(missing.bundle.mtimeMs, null);
	});
});

describe('drain', () => {
	it('refuses only new call work, only while draining, only on POST', () => {
		for (const p of ['/call', '/concurrent-call', '/meeting']) {
			assert.equal(isDrainBlocked(p, 'POST', true), true, p);
			assert.equal(isDrainBlocked(p, 'POST', false), false, `${p} not draining`);
		}
		for (const p of ['/health', '/hangup', '/twilio/connect', '/twilio/status', '/twilio/meeting-ivr']) {
			assert.equal(isDrainBlocked(p, 'POST', true), false, p);
		}
		assert.equal(isDrainBlocked('/call', 'GET', true), false);
	});

	it('exits when the last call ends, or at the cap regardless', () => {
		const t0 = 1_000_000;
		assert.equal(drainMayExit(1, t0, t0 + 1000), false);
		assert.equal(drainMayExit(0, t0, t0 + 1000), true);
		assert.equal(drainMayExit(3, t0, t0 + DRAIN_CAP_MS - 1), false);
		assert.equal(drainMayExit(3, t0, t0 + DRAIN_CAP_MS), true);
		assert.equal(DRAIN_CAP_MS, 10 * 60 * 1000);
	});
});

describe('ngrok respawn backoff', () => {
	it('doubles from 2 s to a 60 s ceiling', () => {
		assert.deepEqual([1, 2, 3, 4, 5, 6, 7].map(ngrokRespawnDelayMs), [2000, 4000, 8000, 16000, 32000, 60000, 60000]);
		assert.equal(ngrokRespawnDelayMs(0), 2000);
	});
});

describe('ngrok respawn scheduler', () => {
	// Simulated clock: timers fire in order at their due time; a failed attempt reports
	// itself twice, as the child's exit and the attempt's own error (the real shape).
	function simulate(seconds: number, failEverything: boolean) {
		let now = 0;
		const queue: Array<{ at: number; fn: () => void; id: number }> = [];
		let ids = 0;
		const timers = {
			set: (fn: () => void, ms: number) => { const id = ++ids; queue.push({ at: now + ms, fn, id }); return id as unknown as ReturnType<typeof setTimeout>; },
			clear: (t: ReturnType<typeof setTimeout>) => { const i = queue.findIndex((q) => q.id === (t as unknown as number)); if (i >= 0) queue.splice(i, 1); },
		};
		const spawns: number[] = [];
		let maxPending = 0;
		const sched = new RespawnScheduler(() => {
			spawns.push(now);
			if (failEverything) { sched.schedule(); sched.schedule(); }  // exit + catch
			else sched.reset();
		}, ngrokRespawnDelayMs, timers);
		sched.schedule(); sched.schedule();  // the first failure, reported twice
		while (queue.length) {
			queue.sort((a, b) => a.at - b.at);
			const next = queue[0];
			if (next.at > seconds * 1000) break;
			queue.shift();
			now = next.at;
			next.fn();
			maxPending = Math.max(maxPending, queue.length);
		}
		return { spawns, maxPending, attempts: sched.attempts };
	}

	it('a failure window of 400 s produces about ten spawns, never a timer per report', () => {
		const { spawns, maxPending } = simulate(400, true);
		assert.deepEqual(spawns, [2000, 6000, 14000, 30000, 62000, 122000, 182000, 242000, 302000, 362000]);
		assert.equal(maxPending, 1, 'never more than one pending respawn');
	});

	it('a second request while one is pending is a no-op, and success starts the backoff over', () => {
		const { spawns, attempts } = simulate(400, false);
		assert.deepEqual(spawns, [2000], 'one attempt, it succeeded');
		assert.equal(attempts, 0, 'reset after success');
		const calls: number[] = [];
		const s = new RespawnScheduler(() => calls.push(1), () => 5000, { set: (fn, ms) => setTimeout(fn, ms), clear: (t) => clearTimeout(t) });
		assert.equal(s.schedule(), 5000);
		assert.equal(s.schedule(), -1);
		assert.equal(s.pending, true);
		s.reset();
		assert.equal(s.pending, false);
		assert.equal(calls.length, 0, 'the cleared timer never fires');
	});
});

describe('the server wires the rules in (source guards)', () => {
	const SRC = readFileSync(new URL('../skills/phone-conversation/scripts/conversation-server.ts', import.meta.url), 'utf-8');
	it('SIGTERM and SIGINT go through beginShutdown, never a bare exit', () => {
		assert.match(SRC, /process\.on\('SIGTERM', \(\) => beginShutdown\('SIGTERM'\)\)/);
		assert.match(SRC, /process\.on\('SIGINT', \(\) => beginShutdown\('SIGINT'\)\)/);
		assert.doesNotMatch(SRC, /process\.on\('SIGTERM', \(\) => \{ cleanupNgrok\(\); process\.exit\(0\); \}\)/);
	});
	it('a dead ngrok is respawned unless the server is shutting down, through the one scheduler', () => {
		assert.match(SRC, /proc\.on\('exit', \(code, signal\) => \{\s*exited = true;\s*if \(shuttingDown \|\| ngrokProcess !== proc\) return;/);
		assert.equal((SRC.match(/ngrokScheduler\.schedule\(\)/g) ?? []).length, 2, 'the exit handler and the failed attempt both ask the one scheduler');
		assert.doesNotMatch(SRC, /setTimeout\(\(\) => \{ void respawnNgrok/, 'no bare retry timer outside the scheduler');
		assert.match(SRC, /ngrokScheduler\.reset\(\)/);
		assert.match(SRC, /if \(exited\) throw new Error\('ngrok exited before its tunnel came up'\)/, 'a dead child ends its own poll, so it never claims a later attempt\'s tunnel');
	});
	it('new call work is refused while draining, before the handlers', () => {
		assert.match(SRC, /if \(isDrainBlocked\(path, req\.method, draining\)\) \{\s*json\(res, 503/);
	});
});
