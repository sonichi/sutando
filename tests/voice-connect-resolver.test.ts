import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { resolveVoiceEndpoint, defaultCandidates, directWsUrl } from '../src/voice-connect-resolver.js';

describe('voice-connect-resolver', () => {
	it('defaultCandidates: local first, cloud last, omits unknowns', () => {
		const full = defaultCandidates({ lanHost: '192.168.1.5', relayWsUrl: 'wss://r', cloudUrl: 'wss://c' });
		assert.deepEqual(full.map((x) => x.tier), ['local', 'lan', 'relay', 'cloud']);
		assert.ok(full[0].url.includes('localhost:9900'));
		const minimal = defaultCandidates({});
		assert.deepEqual(minimal.map((x) => x.tier), ['local']); // only local when nothing else known
	});

	it('tailnet slots between local and lan — mesh VPN beats same-subnet-only (upstream reconciliation)', () => {
		const full = defaultCandidates({
			tailnetHost: 'core.tail1234.ts.net',
			lanHost: '192.168.1.5',
			relayWsUrl: 'wss://r',
			cloudUrl: 'wss://c',
		});
		assert.deepEqual(full.map((x) => x.tier), ['local', 'tailnet', 'lan', 'relay', 'cloud']);
	});

	it('off-localhost candidates dial the webUI /ws proxy, never the loopback-only voice port', () => {
		const [local, tailnet, lan] = defaultCandidates({ tailnetHost: 'core.ts.net', lanHost: '192.168.1.5' });
		assert.equal(local.url, 'ws://localhost:9900', 'local keeps the loopback voice-agent WS');
		assert.equal(tailnet.url, 'ws://core.ts.net:8080/ws', 'tailnet goes through :proxyPort/ws');
		assert.equal(lan.url, 'ws://192.168.1.5:8080/ws', 'lan goes through :proxyPort/ws');
	});

	it('directWsUrl: https → wss (tailscale serve fronts :443); http keeps its port; bare host gets proxyPort', () => {
		assert.equal(directWsUrl('https://core.ts.net', 8080), 'wss://core.ts.net/ws');
		assert.equal(directWsUrl('https://core.ts.net/', 8080), 'wss://core.ts.net/ws', 'trailing slash trimmed');
		assert.equal(directWsUrl('http://192.168.1.5:8080', 9999), 'ws://192.168.1.5:8080/ws', 'advertised port preserved');
		assert.equal(directWsUrl('core.ts.net', 8080), 'ws://core.ts.net:8080/ws');
	});

	it('agentEndpoint identity-routes: dial THAT agent, never generic local; relay/cloud stay as fallbacks', () => {
		const cands = defaultCandidates({
			agentEndpoint: 'https://agent.ts.net',
			relayWsUrl: 'wss://r',
			cloudUrl: 'wss://c',
		});
		assert.deepEqual(cands.map((x) => x.tier), ['tailnet', 'relay', 'cloud']);
		assert.equal(cands[0].url, 'wss://agent.ts.net/ws');
		assert.ok(!cands.some((x) => x.tier === 'local'), 'generic local would answer as the wrong agent');
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

	it('a throwing onAttempt is best-effort — resolution still falls through', async () => {
		// local down, cloud reachable. A telemetry hook that throws on the first
		// (failed) attempt must NOT abort the ladder — we still reach cloud.
		const ep = await resolveVoiceEndpoint(defaultCandidates({ cloudUrl: 'wss://cloud/x' }), {
			probe: async (url) => url.includes('cloud'),
			onAttempt: () => {
				throw new Error('telemetry failed');
			},
		});
		assert.equal(ep?.tier, 'cloud', 'throwing onAttempt did not prevent fall-through');
	});
});
