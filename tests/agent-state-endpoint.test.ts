import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, ChildProcess } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Integration test for PR #418 / #419 agent-state plumbing.
// Spawns web-client.ts on a random port, exercises /sse-status + /mute-state,
// asserts the `state` field flows through the 4-value enum + rejects invalid
// values. Prevents regression of the avatar-animation chain that shipped
// 2026-04-17 (web-client step 1 of 3, no test coverage at merge time).

const PORT = 18081; // well above the 8080 dev server + 9900 voice-agent

// Test spawns the same web-client.ts the prod server uses. web-client reads
// `../core-status.json` via the #443 coreIsRunning fallback, so a mid-pass
// `{"status":"running"}` write by the proactive loop leaks into the isolated
// test server and turns "idle" assertions into "working". Neutralize by
// writing an idle sentinel before the test + restoring the original bytes on
// teardown. Without this, 2 tests fail whenever `npm test` coincides with a
// /proactive-loop step-0 write (or any other running-state write).
const CORE_STATUS_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', 'core-status.json');
let savedCoreStatus: string | null = null;

// voice-state.json is read by web-client's readVoiceState() as the authoritative
// voiceConnected source (browser POST cache is the fallback). Tests in the
// voice-state describe block below write/remove this file to exercise both
// branches. Stash + restore any real prod value on setup/teardown.
const VOICE_STATE_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', 'voice-state.json');
let savedVoiceState: string | null = null;

let child: ChildProcess;

async function fetchJson(path: string): Promise<any> {
	const res = await fetch(`http://localhost:${PORT}${path}`);
	return res.json();
}

describe('/sse-status + /mute-state — agent state plumbing (PR #418)', () => {
	before(async () => {
		// Stash + neutralize core-status.json so a mid-pass "running" write
		// by a live proactive loop can't leak "working" state into the test.
		if (existsSync(CORE_STATUS_PATH)) {
			savedCoreStatus = readFileSync(CORE_STATUS_PATH, 'utf-8');
		}
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'idle', ts: Math.floor(Date.now() / 1000) }) + '\n');

		// Same rationale as core-status.json: stash + neutralize so a prod
		// voice-state.json at repo root doesn't leak voiceConnected=true into
		// the idle baseline assertion below.
		if (existsSync(VOICE_STATE_PATH)) {
			savedVoiceState = readFileSync(VOICE_STATE_PATH, 'utf-8');
		}
		try { unlinkSync(VOICE_STATE_PATH); } catch { /* already gone */ }

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
		if (child && !child.killed) {
			await new Promise<void>((resolve) => {
				const hardKill = setTimeout(() => {
					try { child.kill('SIGKILL'); } catch { /* already dead */ }
					resolve();
				}, 2_000);
				child.once('exit', () => { clearTimeout(hardKill); resolve(); });
				child.kill('SIGTERM');
			});
		}
		// Restore original core-status.json bytes (or delete the sentinel we
		// wrote if no original existed). Keeps the live proactive loop's
		// post-test reads consistent with what it wrote last.
		if (savedCoreStatus !== null) {
			writeFileSync(CORE_STATUS_PATH, savedCoreStatus);
		}
		// Restore voice-state.json (or clean up our test write)
		if (savedVoiceState !== null) {
			writeFileSync(VOICE_STATE_PATH, savedVoiceState);
		} else {
			try { unlinkSync(VOICE_STATE_PATH); } catch { /* idempotent */ }
		}
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

	// voice-state.json — authoritative over the browser-reported _voiceState cache.
	// Written by voice-agent on client connect/disconnect; web-client reads it in
	// /sse-status. Covers the regression from 2026-04-19 where a web-client restart
	// left voiceConnected=false in the cache even though voice was still connected.
	it('/sse-status uses voice-state.json as authoritative voiceConnected', async () => {
		// Prime the _voiceState cache to false (the desync baseline)
		await fetchJson('/mute-state?voice=false');
		let status = await fetchJson('/sse-status');
		assert.equal(status.voiceConnected, false, 'cache baseline');

		// Write a fresh connected=true file; readVoiceState should return true
		writeFileSync(VOICE_STATE_PATH, JSON.stringify({ connected: true, ts: Math.floor(Date.now() / 1000) }));
		status = await fetchJson('/sse-status');
		assert.equal(status.voiceConnected, true, 'fresh voice-state.json overrides cache');

		// Flip the file to disconnected; readVoiceState should return false
		writeFileSync(VOICE_STATE_PATH, JSON.stringify({ connected: false, ts: Math.floor(Date.now() / 1000) }));
		status = await fetchJson('/sse-status');
		assert.equal(status.voiceConnected, false, 'disconnected voice-state.json overrides cache');

		try { unlinkSync(VOICE_STATE_PATH); } catch {}
	});

	it('stale voice-state.json with connected=true falls back to cache', async () => {
		// Prime cache = false
		await fetchJson('/mute-state?voice=false');

		// Write a stale (>120s old) connected=true file — should NOT trust it
		const staleTs = Math.floor(Date.now() / 1000) - 200;
		writeFileSync(VOICE_STATE_PATH, JSON.stringify({ connected: true, ts: staleTs }));
		const status = await fetchJson('/sse-status');
		assert.equal(status.voiceConnected, false, 'stale connected=true should defer to _voiceState=false');

		// Flip cache to true and re-check — stale file still shouldn't matter,
		// we fall back to cache which is now true
		await fetchJson('/mute-state?voice=true');
		const status2 = await fetchJson('/sse-status');
		assert.equal(status2.voiceConnected, true, 'stale file falls back to cache=true');

		try { unlinkSync(VOICE_STATE_PATH); } catch {}
	});

	it('missing voice-state.json falls back to cache', async () => {
		try { unlinkSync(VOICE_STATE_PATH); } catch {}
		await fetchJson('/mute-state?voice=true');
		const status = await fetchJson('/sse-status');
		assert.equal(status.voiceConnected, true, 'no file → _voiceState cache wins');
	});
});
