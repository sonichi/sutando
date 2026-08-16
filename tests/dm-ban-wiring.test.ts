import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, readdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { setupTempWorkspace } from './_helpers/temp-workspace.js';

// Drives the PRODUCTION onExhausted callback, not isDmBanned(). Testing the
// helper alone leaves a bypass of the call site green.
const { workspace: WS, cleanup } = setupTempWorkspace('dm-ban-wiring');
mkdirSync(join(WS, 'results'), { recursive: true });
mkdirSync(join(WS, 'state'), { recursive: true });

const { wireDurableChannels } = await import('../src/live-agent-runtime.js');

/** Connected but never active — the only window that reaches onExhausted. A
 * disconnected client makes the watcher HOLD the result instead of delivering. */
function stuckSession(): any {
	return {
		sessionManager: { isActive: false },
		clientConnected: true,
		transport: { sendContent: () => {} },
	};
}

// Accumulate SIGHTINGS: the same watcher consumes proactive-* files, so a
// stuck-voice DM that was written is gone again within one poll.
async function sightingsOver(ms: number, seen: Set<string>): Promise<Set<string>> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		for (const f of readdirSync(join(WS, 'results'))) {
			if (f.startsWith('proactive-voice-stuck-')) seen.add(f);
		}
		await new Promise(r => setTimeout(r, 150));
	}
	return seen;
}

describe('dm-ban at the wiring boundary', () => {
	after(cleanup);

	it('writes the stuck-voice DM when NOT banned, and suppresses it when banned', async () => {
		wireDurableChannels(stuckSession(), {});

		// BANNED FIRST, on purpose. A written fallback is itself a proactive-*
		// file the same watcher re-delivers, so an unbanned run cascades.
		const seen = new Set<string>();
		writeFileSync(join(WS, 'state', 'dm-ban.sentinel'), '');
		writeFileSync(join(WS, 'results', 'voice-banned.txt'), 'result body A');
		await sightingsOver(9000, seen);
		assert.equal(seen.size, 0,
			`banned must write no stuck-voice DM; saw ${[...seen].join(', ')}`);

		rmSync(join(WS, 'state', 'dm-ban.sentinel'));
		writeFileSync(join(WS, 'results', 'voice-unbanned.txt'), 'result body B');
		await sightingsOver(9000, seen);
		assert.ok(seen.size >= 1,
			'unbanned must produce one — at 0 the watcher never fired and the ' +
			'banned assertion above passed vacuously');
	});
});
