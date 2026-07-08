import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import {
	isPrivateLanIpv4,
	detectLanIpv4,
	lanEndpointUrl,
	parseTailnetHost,
	detectTailnetHost,
	tailnetEndpointUrl,
	directEndpoints,
	lanShareEnabled,
	tailnetServeEnabled,
	composeTailnetUrl,
} from '../src/reachability-endpoints.js';

// US-10 / Tier 2b: the core detects the direct endpoints it can advertise
// (tailnet preferred, LAN fallback). These are the CORE-side halves; both are
// gated behind SUTANDO_LAN_SHARE so nothing is advertised unless the /ws proxy
// (PR #2021) is opted in.

const origShare = process.env.SUTANDO_LAN_SHARE;
const origPort = process.env.CLIENT_PORT;
const origServe = process.env.SUTANDO_TAILNET_SERVE;
beforeEach(() => {
	delete process.env.SUTANDO_LAN_SHARE;
	delete process.env.CLIENT_PORT;
	delete process.env.SUTANDO_TAILNET_SERVE;
});
afterEach(() => {
	if (origShare === undefined) delete process.env.SUTANDO_LAN_SHARE;
	else process.env.SUTANDO_LAN_SHARE = origShare;
	if (origPort === undefined) delete process.env.CLIENT_PORT;
	else process.env.CLIENT_PORT = origPort;
	if (origServe === undefined) delete process.env.SUTANDO_TAILNET_SERVE;
	else process.env.SUTANDO_TAILNET_SERVE = origServe;
});

describe('isPrivateLanIpv4', () => {
	it('accepts RFC1918 ranges', () => {
		for (const ip of ['10.0.0.4', '172.16.5.9', '172.31.255.1', '192.168.1.5']) {
			assert.equal(isPrivateLanIpv4(ip), true, ip);
		}
	});
	it('rejects loopback, link-local, tailnet/CGNAT, and public', () => {
		for (const ip of ['127.0.0.1', '169.254.1.1', '100.115.82.19', '8.8.8.8', '172.32.0.1']) {
			assert.equal(isPrivateLanIpv4(ip), false, ip);
		}
	});
	it('rejects malformed input', () => {
		for (const ip of ['', 'not.an.ip', '10.0.0', '999.1.1.1', '1.2.3.4.5']) {
			assert.equal(isPrivateLanIpv4(ip), false, ip);
		}
	});
});

describe('detectLanIpv4', () => {
	it('picks the private IPv4, skipping internal/tailnet/ipv6', () => {
		const ifaces = {
			lo0: [{ address: '127.0.0.1', family: 'IPv4', internal: true } as any],
			en0: [
				{ address: 'fe80::1', family: 'IPv6', internal: false } as any,
				{ address: '192.168.1.42', family: 'IPv4', internal: false } as any,
			],
			utun3: [{ address: '100.115.82.19', family: 'IPv4', internal: false } as any],
		};
		assert.equal(detectLanIpv4(ifaces), '192.168.1.42');
	});
	it('returns null when only loopback/tailnet present', () => {
		const ifaces = {
			lo0: [{ address: '127.0.0.1', family: 'IPv4', internal: true } as any],
			utun3: [{ address: '100.115.82.19', family: 'IPv4', internal: false } as any],
		};
		assert.equal(detectLanIpv4(ifaces), null);
	});
	it('is deterministic across interface ordering (lowest name wins)', () => {
		const ifaces = {
			en5: [{ address: '10.0.0.9', family: 'IPv4', internal: false } as any],
			en0: [{ address: '192.168.1.7', family: 'IPv4', internal: false } as any],
		};
		assert.equal(detectLanIpv4(ifaces), '192.168.1.7');
	});
});

describe('parseTailnetHost', () => {
	it('prefers the MagicDNS name (trailing dot stripped)', () => {
		const status = {
			Self: {
				DNSName: 'qingyuns-macbook-pro.taila1a7c4.ts.net.',
				TailscaleIPs: ['100.115.82.19', 'fd7a:115c:a1e0::b73b:5214'],
				Online: true,
			},
		};
		assert.equal(parseTailnetHost(status), 'qingyuns-macbook-pro.taila1a7c4.ts.net');
	});
	it('falls back to the 100.x IPv4 when MagicDNS is absent', () => {
		const status = { Self: { DNSName: '', TailscaleIPs: ['100.115.82.19', 'fd7a::1'] } };
		assert.equal(parseTailnetHost(status), '100.115.82.19');
	});
	it('returns null when the node is offline', () => {
		const status = { Self: { DNSName: 'x.ts.net.', TailscaleIPs: ['100.1.1.1'], Online: false } };
		assert.equal(parseTailnetHost(status), null);
	});
	it('returns null for missing/garbage self', () => {
		assert.equal(parseTailnetHost(null), null);
		assert.equal(parseTailnetHost({}), null);
		assert.equal(parseTailnetHost({ Self: { TailscaleIPs: [] } }), null);
		assert.equal(parseTailnetHost({ Self: { TailscaleIPs: ['fd7a::1'] } }), null); // no v4
	});
});

describe('detectTailnetHost (injected runner)', () => {
	it('parses a good status blob', () => {
		const run = () => JSON.stringify({ Self: { DNSName: 'host.ts.net.', Online: true } });
		assert.equal(detectTailnetHost(run), 'host.ts.net');
	});
	it('returns null when tailscale is absent (runner yields null)', () => {
		assert.equal(detectTailnetHost(() => null), null);
	});
	it('returns null on unparseable output rather than throwing', () => {
		assert.equal(detectTailnetHost(() => 'not json'), null);
	});
});

describe('composeTailnetUrl — TLS scheme switch (wss:// enablement)', () => {
	const host = 'qingyuns-macbook-pro.taila1a7c4.ts.net';
	it('serve OFF → plain http on CLIENT_PORT (native-only path)', () => {
		assert.equal(composeTailnetUrl(host, false), `http://${host}:8080`);
	});
	it('serve ON → https on the MagicDNS name, no explicit port (browser wss-ready)', () => {
		// tailscale serve fronts :443, so the client derives wss://host/ws — which
		// a browser on an HTTPS page can open (plain ws:// would be blocked).
		assert.equal(composeTailnetUrl(host, true), `https://${host}`);
	});
	it('reads the SUTANDO_TAILNET_SERVE env when serve arg omitted', () => {
		assert.equal(tailnetServeEnabled(), false);
		assert.equal(composeTailnetUrl(host), `http://${host}:8080`);
		process.env.SUTANDO_TAILNET_SERVE = '1';
		assert.equal(tailnetServeEnabled(), true);
		assert.equal(composeTailnetUrl(host), `https://${host}`);
	});
});

describe('endpoint URLs are gated by SUTANDO_LAN_SHARE', () => {
	it('lanEndpointUrl/tailnetEndpointUrl/directEndpoints are null/empty when unshared', () => {
		assert.equal(lanShareEnabled(), false);
		assert.equal(lanEndpointUrl(), null);
		assert.equal(tailnetEndpointUrl(), null);
		assert.deepEqual(directEndpoints(), { tailnet: null, lan: null });
	});
});
