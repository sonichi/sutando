// The phone server's lifecycle rules (P1-19): a restart never drops a live call,
// ngrok is respawned with backoff, and /health says enough for a supervisor to
// judge staleness without `ps`.
// Run: npx tsx --test --test-force-exit tests/phone-server-lifecycle.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const { healthPayload, isDrainBlocked, drainMayExit, ngrokRespawnDelayMs, DRAIN_CAP_MS } =
	await import('../skills/phone-conversation/scripts/server-lifecycle.js');

describe('health', () => {
	it('reports the start time and the bundle it runs, with a null mtime for a missing bundle', () => {
		const dir = mkdtempSync(join(tmpdir(), 'phone-health-'));
		const bundle = join(dir, 'conversation-server.js');
		writeFileSync(bundle, '// bundle');
		const h = healthPayload({ activeCalls: 2, webhookUrl: 'https://x.ngrok.app', startedAt: 123, bundlePath: bundle }) as Record<string, any>;
		assert.equal(h.status, 'ok');
		assert.equal(h.activeCalls, 2);
		assert.equal(h.startedAt, 123);
		assert.equal(h.bundle.path, bundle);
		assert.equal(typeof h.bundle.mtimeMs, 'number');
		const missing = healthPayload({ activeCalls: 0, webhookUrl: '', startedAt: 1, bundlePath: join(dir, 'nope.js') }) as Record<string, any>;
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

describe('the server wires the rules in (source guards)', () => {
	const SRC = readFileSync(new URL('../skills/phone-conversation/scripts/conversation-server.ts', import.meta.url), 'utf-8');
	it('SIGTERM and SIGINT go through beginShutdown, never a bare exit', () => {
		assert.match(SRC, /process\.on\('SIGTERM', \(\) => beginShutdown\('SIGTERM'\)\)/);
		assert.match(SRC, /process\.on\('SIGINT', \(\) => beginShutdown\('SIGINT'\)\)/);
		assert.doesNotMatch(SRC, /process\.on\('SIGTERM', \(\) => \{ cleanupNgrok\(\); process\.exit\(0\); \}\)/);
	});
	it('a dead ngrok is respawned unless the server is shutting down', () => {
		assert.match(SRC, /proc\.on\('exit', \(code, signal\) => \{\s*if \(shuttingDown \|\| ngrokProcess !== proc\) return;/);
		assert.match(SRC, /void respawnNgrok\(port\)/);
	});
	it('new call work is refused while draining, before the handlers', () => {
		assert.match(SRC, /if \(isDrainBlocked\(path, req\.method, draining\)\) \{\s*json\(res, 503/);
	});
});
