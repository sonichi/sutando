import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { describe, it } from 'node:test';

const source = readFileSync(fileURLToPath(new URL('../src/web-client.ts', import.meta.url)), 'utf8');

describe('web-client voice token cache', () => {
	it('connects synchronously in the click path when a token is cached', () => {
		const start = source.indexOf('\nfunction connectWs() {');
		const end = source.indexOf('\nfunction resetTurnTranscriptState()', start);
		assert.notEqual(start, -1);
		assert.notEqual(end, -1);
		const calls: string[] = [];
		let fetches = 0;
		const context: Record<string, unknown> = {
			URL,
			window: { location: { href: 'http://localhost:8080/' } },
			_voiceUrlOverride: null,
			cachedVoiceWsToken: 'cached-token',
			CAPTURE_BUF: 1024,
			INPUT_RATE: 24000,
			OUTPUT_RATE: 24000,
			$() { return { value: 'ws://localhost:9900' }; },
			fetch() { fetches++; return Promise.reject(new Error('unexpected token fetch')); },
			dbg() {},
			setStatus() {},
			onVoiceStatus() {},
			onVoiceConnectFailure() {},
			handleTranscript() {},
			resetTurnTranscriptState() {},
			handleProtocolMessage() {},
			SutandoVoice: {
				VoiceTransport: class {
					connect(url: string) {
						calls.push(url);
						return Promise.resolve();
					}
				},
			},
		};
		runInNewContext(`${source.slice(start + 1, end)}\nconnectWs();`, context);
		assert.equal(fetches, 0);
		assert.equal(calls.length, 1);
		assert.equal(new URL(calls[0]).searchParams.get('access_token'), 'cached-token');
	});
});
