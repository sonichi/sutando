import assert from 'node:assert/strict';
import type { IncomingHttpHeaders } from 'node:http';
import { describe, it } from 'node:test';

import {
	installVoiceConnectionGate,
	voiceConnectionDecision,
} from '../src/local-api-gate.ts';

const token = 'voice-owner-token';

function request(url = '/', headers: IncomingHttpHeaders = {}) {
	return { url, headers };
}

describe('voice WebSocket owner decision', () => {
	it('allows state-only probes and requires credentials for all other connections', () => {
		assert.deepEqual(voiceConnectionDecision(request('/?probe=1'), ''), { ok: true });
		assert.deepEqual(voiceConnectionDecision(request('/'), ''), {
			ok: false, code: 4401, reason: 'unauthorized',
		});
		assert.deepEqual(voiceConnectionDecision(request('/', { authorization: 'Bearer wrong' }), token), {
			ok: false, code: 4401, reason: 'unauthorized',
		});
		assert.deepEqual(voiceConnectionDecision(request('/?takeover=1'), token), {
			ok: false, code: 4401, reason: 'unauthorized',
		});
	});

	it('accepts header or access_token credentials but rejects non-loopback Origins', () => {
		assert.deepEqual(voiceConnectionDecision(request('/', { authorization: `Bearer ${token}` }), token), { ok: true });
		assert.deepEqual(voiceConnectionDecision(request(`/?access_token=${token}`), token), { ok: true });
		assert.deepEqual(voiceConnectionDecision(request(`/?access_token=${token}`, { origin: 'https://evil.example' }), token), {
			ok: false, code: 4403, reason: 'origin not allowed',
		});
	});

	it('admits a credential-free loopback client and refuses everything else', () => {
		const loopback = (url: string, headers: IncomingHttpHeaders = {}) =>
			({ url, headers, socket: { remoteAddress: '127.0.0.1' } });
		assert.deepEqual(voiceConnectionDecision(loopback('/'), token), { ok: true });
		assert.deepEqual(voiceConnectionDecision(loopback('/', { authorization: 'Bearer wrong' }), token), {
			ok: false, code: 4401, reason: 'unauthorized',
		});
		assert.deepEqual(voiceConnectionDecision(loopback('/', { origin: 'https://evil.example' }), token), {
			ok: false, code: 4403, reason: 'origin not allowed',
		});
		assert.deepEqual(voiceConnectionDecision(loopback('/?probe=1', { origin: 'https://evil.example' }), token), {
			ok: false, code: 4403, reason: 'origin not allowed',
		});
		const nonLoopback = { url: '/', headers: {}, socket: { remoteAddress: '192.168.1.20' } };
		assert.deepEqual(voiceConnectionDecision(nonLoopback, token), {
			ok: false, code: 4401, reason: 'unauthorized',
		});
	});
});

describe('voice WebSocket gate installation', () => {
	it('closes refusals before attachment and preserves the original transport receiver', () => {
		type Socket = {
			on(event: string, listener: () => void): unknown;
			close(code: number, reason: string): unknown;
		};
		type Request = { url?: string; headers: IncomingHttpHeaders };
		type Transport = { handleConnection: (ws: Socket, req: Request) => void };
		let listenerInstalled = false;
		let closed: [number, string] | null = null;
		let tokenReads = 0;
		let attached = false;
		const transport: Transport = {
			handleConnection(this: unknown, _ws, _req) {
				attached = this === transport;
			},
		};
		installVoiceConnectionGate({ clientTransport: transport }, () => {
			tokenReads++;
			return token;
		});
		const socket = {
			on: () => { listenerInstalled = true; },
			close: (code: number, reason: string) => { closed = [code, reason]; },
		};
		transport.handleConnection(socket, request('/?takeover=1'));
		assert.deepEqual(closed, [4401, 'unauthorized']);
		assert.equal(listenerInstalled, true);
		assert.equal(attached, false);

		closed = null;
		transport.handleConnection(socket, request('/?probe=1'));
		assert.equal(closed, null);
		assert.equal(attached, true);
		assert.equal(tokenReads, 1, 'state probes do not read or mint an owner token');
	});

	it('fails startup when the Bodhi connection seam is absent', () => {
		assert.throws(() => installVoiceConnectionGate({}, () => token), /voice WS gate seam is missing/);
	});
});
