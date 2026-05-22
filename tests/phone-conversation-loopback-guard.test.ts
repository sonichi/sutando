import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { IncomingMessage } from 'node:http';
import { isLoopback } from '../skills/phone-conversation/scripts/loopback_guard.js';

// Security-relevant regression guard for the loopback-only gate on the
// phone-conversation server's control endpoints.
//
// Pre-fix: every endpoint (incl. `/call`, `/hangup`, `/concurrent-call`,
// `/play-audio`, etc.) was LAN-reachable because the server binds to
// `0.0.0.0` for ngrok. /twilio/* paths validate Twilio's signature;
// everything else was unauthenticated. Worst case: anyone on the LAN
// could POST `/call` with an arbitrary phone number and originate a
// Twilio call on the owner's account.

function fakeReq(remoteAddress: string | undefined): IncomingMessage {
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	return { socket: { remoteAddress } } as any;
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

describe('conversation-server.ts wiring', () => {
	const SRC = readFileSync(
		join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
		'utf-8',
	);

	it('imports isLoopback from the guard module', () => {
		assert.match(SRC, /from\s+['"]\.\/loopback_guard(?:\.js)?['"]/, (
			'conversation-server.ts must import isLoopback from ./loopback_guard'
		));
	});

	it('gates non-/twilio non-/health endpoints with isLoopback', () => {
		assert.match(
			SRC,
			/path\s*!==\s*['"]\/health['"][\s\S]{0,80}!\s*path\.startsWith\(['"]\/twilio\/['"]\)[\s\S]{0,80}!isLoopback\(req\)/,
			'conversation-server.ts must check `path !== "/health" && !path.startsWith("/twilio/") && !isLoopback(req)` before dispatching to control endpoints',
		);
	});

	it('returns 403 (not 200) on the rejection path', () => {
		assert.match(
			SRC,
			/json\(res,\s*403,\s*\{\s*error:\s*['"]control endpoints are loopback-only['"]/,
			'conversation-server.ts must respond 403 with the documented error string',
		);
	});

	it('logs the rejected remote address for incident triage', () => {
		assert.match(SRC, /REJECTED non-loopback/);
	});
});
