import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import type { IncomingMessage } from 'node:http';
import { isDirectLoopback, isLoopback } from '../skills/phone-conversation/scripts/loopback_guard.js';

// Security-relevant regression guard for the loopback-only gate on the
// phone-conversation server's control endpoints.
//
// Pre-fix: every endpoint (incl. `/call`, `/hangup`, `/concurrent-call`,
// `/play-audio`, etc.) was LAN-reachable because the server binds to
// `0.0.0.0` for ngrok. /twilio/* paths validate Twilio's signature;
// everything else was unauthenticated. Worst case: anyone on the LAN
// could POST `/call` with an arbitrary phone number and originate a
// Twilio call on the owner's account.

function fakeReq(remoteAddress: string | undefined, headers: Record<string, string> = {}): IncomingMessage {
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	return { socket: { remoteAddress }, headers } as any;
}

describe('loopback_guard.isLoopback', () => {
	it('accepts IPv4 loopback (127.0.0.1)', () => {
		assert.equal(isLoopback(fakeReq('127.0.0.1')), true);
	});
	it('accepts IPv6 loopback (::1)', () => {
		assert.equal(isLoopback(fakeReq('::1')), true);
	});
	it('accepts IPv4-mapped IPv6 loopback (::ffff:127.0.0.1)', () => {
		assert.equal(isLoopback(fakeReq('::ffff:127.0.0.1')), true);
	});
	it('rejects LAN IPv4 (192.168.x.x)', () => {
		assert.equal(isLoopback(fakeReq('192.168.1.42')), false);
	});
	it('rejects another LAN IPv4 (10.x.x.x)', () => {
		assert.equal(isLoopback(fakeReq('10.0.0.5')), false);
	});
	it('rejects public IPv4', () => {
		assert.equal(isLoopback(fakeReq('1.1.1.1')), false);
	});
	it('rejects link-local IPv6 (fe80::)', () => {
		assert.equal(isLoopback(fakeReq('fe80::1')), false);
	});
	it('rejects ULA IPv6 (fc00::)', () => {
		assert.equal(isLoopback(fakeReq('fc00::1')), false);
	});
	it('rejects IPv4-mapped IPv6 to a NON-loopback address', () => {
		// Defensive: the SSRF-bypass shape (private IPv4 mapped into
		// IPv6) must NOT be treated as loopback unless it actually maps
		// to 127.0.0.1.
		assert.equal(isLoopback(fakeReq('::ffff:192.168.1.42')), false);
		assert.equal(isLoopback(fakeReq('::ffff:10.0.0.5')), false);
	});
	it('rejects empty / undefined remoteAddress', () => {
		assert.equal(isLoopback(fakeReq('')), false);
		assert.equal(isLoopback(fakeReq(undefined)), false);
	});
});

describe('loopback_guard.isDirectLoopback', () => {
	it('accepts an unforwarded loopback request', () => {
		assert.equal(isDirectLoopback(fakeReq('127.0.0.1')), true);
	});
	it('rejects loopback requests carrying x-forwarded-for', () => {
		assert.equal(isDirectLoopback(fakeReq('127.0.0.1', { 'x-forwarded-for': '198.51.100.4' })), false);
	});
	it('rejects loopback requests carrying Forwarded or a Tailscale Funnel header', () => {
		assert.equal(isDirectLoopback(fakeReq('::1', { forwarded: 'for=198.51.100.4' })), false);
		assert.equal(isDirectLoopback(fakeReq('::1', { 'tailscale-funnel-request': '1' })), false);
	});
	it('rejects requests carrying each supported forwarding header', () => {
		for (const header of [
			'cf-connecting-ip',
			'true-client-ip',
			'x-forwarded-server',
			'x-original-forwarded-for',
			'via',
		]) {
			assert.equal(isDirectLoopback(fakeReq('127.0.0.1', { [header]: '198.51.100.4' })), false, header);
		}
	});
	it('rejects LAN requests', () => {
		assert.equal(isDirectLoopback(fakeReq('192.168.1.42')), false);
	});
});


