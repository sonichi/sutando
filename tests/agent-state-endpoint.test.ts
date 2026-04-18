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

	it('accepts all 5 valid agent states via the correct track', async () => {
		// Browser track (no source=tool): idle / listening / speaking only.
		for (const state of ['idle', 'listening', 'speaking']) {
			const body = await fetchJson(`/mute-state?state=${state}`);
			assert.equal(body.state, state, `POST state=${state} should echo back`);
			const status = await fetchJson('/sse-status');
			assert.equal(status.state, state, `/sse-status should reflect ${state}`);
		}
		// Tool track (source=tool): working / seeing.
		for (const state of ['working', 'seeing']) {
			const body = await fetchJson(`/mute-state?state=${state}&source=tool`);
			assert.equal(body.state, state, `POST state=${state}&source=tool should echo back`);
			const status = await fetchJson('/sse-status');
			assert.equal(status.state, state, `/sse-status should reflect ${state}`);
			// Clear tool track before next iteration so seeing's TTL
			// auto-revert doesn't race with the working assertion above.
			await fetchJson('/mute-state?state=idle&source=tool');
		}
	});

	it('clamps browser-sourced working/seeing to listening (tool track only)', async () => {
		// Prime browser track to a known value, and make sure tool track is idle
		await fetchJson('/mute-state?state=idle&source=tool');
		await fetchJson('/mute-state?state=listening');
		// Browser mis-posts working (without source=tool) — should clamp to listening
		const body = await fetchJson('/mute-state?state=working');
		assert.equal(body.state, 'listening', 'browser-sourced working must clamp to listening');
		// Same for seeing
		const body2 = await fetchJson('/mute-state?state=seeing');
		assert.equal(body2.state, 'listening', 'browser-sourced seeing must clamp to listening');
	});

	it('tool track takes precedence over browser track', async () => {
		await fetchJson('/mute-state?state=listening');
		await fetchJson('/mute-state?state=working&source=tool');
		const body = await fetchJson('/mute-state?state=listening'); // browser keeps pinging
		assert.equal(body.state, 'working', 'tool track must not be overwritten by browser');
		// Release tool track → falls through to browser track
		await fetchJson('/mute-state?state=idle&source=tool');
		const body2 = await fetchJson('/sse-status');
		assert.equal(body2.state, 'listening', 'clearing tool track reveals browser track');
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
		const body = await fetchJson('/mute-state?muted=true&voice=true&state=working&source=tool');
		assert.equal(body.muted, true);
		assert.equal(body.voiceConnected, true);
		assert.equal(body.state, 'working');
	});
});
