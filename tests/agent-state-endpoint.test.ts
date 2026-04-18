import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, ChildProcess } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

// Integration test for PR #418 / #419 agent-state plumbing.
// Spawns web-client.ts on a random port, exercises /sse-status + /mute-state,
// asserts the `state` field flows through the 4-value enum + rejects invalid
// values. Prevents regression of the avatar-animation chain that shipped
// 2026-04-17 (web-client step 1 of 3, no test coverage at merge time).

const PORT = 18081; // well above the 8080 dev server + 9900 voice-agent

let child: ChildProcess;

async function fetchJson(path: string): Promise<any> {
	const res = await fetch(`http://localhost:${PORT}${path}`);
	return res.json();
}

describe('/sse-status + /mute-state — agent state plumbing (PR #418)', () => {
	before(async () => {
		child = spawn(
			'npx',
			['tsx', 'src/web-client.ts'],
			{
				env: { ...process.env, CLIENT_PORT: String(PORT), PORT: '19900', CLIENT_HOST: '127.0.0.1' },
				// 'ignore' prevents the pipe buffer from filling in CI (stdout isn't drained),
				// which would block the child and cause the /sse-status poll to time out.
				stdio: 'ignore',
			}
		);
		// Wait up to 20s for server to start listening. CI cold-start on `npx tsx`
		// with fresh node_modules can take significantly longer than a dev machine.
		const deadline = Date.now() + 20_000;
		while (Date.now() < deadline) {
			try {
				const res = await fetch(`http://localhost:${PORT}/sse-status`);
				if (res.ok) return;
			} catch { /* not ready */ }
			await delay(200);
		}
		throw new Error('web-client did not start within 20s');
	});

	after(async () => {
		// Hang-safe teardown: SIGTERM, wait up to 2s, SIGKILL fallback. Without
		// awaiting exit, the live child-process handle keeps node --test alive
		// past the CI job timeout (observed: 9m43s hangs after #423 merged).
		if (!child || child.killed) return;
		await new Promise<void>((resolve) => {
			const hardKill = setTimeout(() => {
				try { child.kill('SIGKILL'); } catch { /* already dead */ }
				resolve();
			}, 2_000);
			child.once('exit', () => { clearTimeout(hardKill); resolve(); });
			child.kill('SIGTERM');
		});
	});

	it('default /sse-status returns state:"idle"', async () => {
		const body = await fetchJson('/sse-status');
		assert.equal(body.state, 'idle');
		assert.equal(body.muted, false);
		assert.equal(body.voiceConnected, false);
		assert.equal(typeof body.clients, 'number');
	});

	it('accepts all 4 valid agent states', async () => {
		for (const state of ['idle', 'listening', 'speaking', 'working']) {
			const body = await fetchJson(`/mute-state?state=${state}`);
			assert.equal(body.state, state, `POST state=${state} should echo back`);
			// Verify persistence: /sse-status returns the same.
			const status = await fetchJson('/sse-status');
			assert.equal(status.state, state, `/sse-status should reflect ${state}`);
		}
	});

	it('rejects invalid agent state (keeps previous value)', async () => {
		// Set a known baseline
		await fetchJson('/mute-state?state=listening');
		// Try invalid
		const body = await fetchJson('/mute-state?state=bogus');
		assert.equal(body.state, 'listening', 'invalid value should not overwrite');
		const status = await fetchJson('/sse-status');
		assert.equal(status.state, 'listening');
	});

	it('mute/voice params continue working independently of state', async () => {
		const body = await fetchJson('/mute-state?muted=true&voice=true&state=working');
		assert.equal(body.muted, true);
		assert.equal(body.voiceConnected, true);
		assert.equal(body.state, 'working');
	});
});
