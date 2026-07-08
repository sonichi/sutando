import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { resolveVoiceEndpoint, defaultCandidates } from '../src/voice-connect-resolver.js';

describe('voice-connect-resolver', () => {
	it('defaultCandidates: local first, cloud last, omits unknowns', () => {
		const full = defaultCandidates({ lanHost: '192.168.1.5', relayWsUrl: 'wss://r', cloudUrl: 'wss://c' });
		assert.deepEqual(full.map((x) => x.tier), ['local', 'lan', 'relay', 'cloud']);
		assert.ok(full[0].url.includes('localhost:9900'));
		const minimal = defaultCandidates({});
		assert.deepEqual(minimal.map((x) => x.tier), ['local']); // only local when nothing else known
	});

	it('returns the first reachable in ladder order (local down → relay up)', async () => {
		const cands = defaultCandidates({ relayWsUrl: 'wss://relay/x', cloudUrl: 'wss://cloud/x' });
		const seen: string[] = [];
		const ep = await resolveVoiceEndpoint(cands, {
			probe: async (url) => { seen.push(url); return url.includes('relay'); },
		});
		assert.equal(ep?.tier, 'relay');
		assert.ok(seen[0].includes('localhost'), 'probes local first (order preserved)');
	});

	it('picks local (Tier 0) when reachable — best wins', async () => {
		const ep = await resolveVoiceEndpoint(defaultCandidates({ cloudUrl: 'wss://c' }), {
			probe: async () => true, // everything reachable → first (local) wins
		});
		assert.equal(ep?.tier, 'local');
	});

	it('null when nothing is reachable', async () => {
		const ep = await resolveVoiceEndpoint(defaultCandidates({ cloudUrl: 'wss://c' }), { probe: async () => false });
		assert.equal(ep, null);
	});

	it('a throwing probe (e.g. LNA denied) counts as unreachable → falls through', async () => {
		const cands = defaultCandidates({ cloudUrl: 'wss://cloud/x' });
		const ep = await resolveVoiceEndpoint(cands, {
			probe: async (url) => { if (url.includes('localhost')) throw new Error('LNA denied'); return true; },
		});
		assert.equal(ep?.tier, 'cloud'); // local threw → fell through to cloud
	});

	it('a slow probe times out → unreachable → falls through', async () => {
		const cands = defaultCandidates({ cloudUrl: 'wss://cloud/x' });
		const ep = await resolveVoiceEndpoint(cands, {
			timeoutMs: 30,
			probe: async (url) => {
				if (url.includes('localhost')) { await new Promise((r) => setTimeout(r, 200)); return true; }
				return true;
			},
		});
		assert.equal(ep?.tier, 'cloud'); // local exceeded 30ms → treated unreachable
	});

	it('onAttempt fires per candidate with the outcome', async () => {
		const attempts: Array<[string, boolean]> = [];
		await resolveVoiceEndpoint(defaultCandidates({ cloudUrl: 'wss://cloud/x' }), {
			probe: async (url) => url.includes('cloud'),
			onAttempt: (ep, ok) => attempts.push([ep.tier, ok]),
		});
		assert.deepEqual(attempts, [['local', false], ['cloud', true]]);
	});
});
