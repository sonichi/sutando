/**
 * credential-resolver (G8) — chain semantics + legacy-equivalence pins.
 *
 * The load-bearing claims under test:
 *  1. No managed file → resolution is IDENTICAL to the legacy env chain
 *     (GEMINI_VOICE_API_KEY → GEMINI_API_KEY → ''), source 'env'/'none'.
 *  2. Managed tier wins over env when present; voice falls back to the managed
 *     text credential BEFORE dropping to env (tier order beats slot order).
 *  3. Malformed/wrong-shape managed files skip the tier — never throw.
 */
import { strict as assert } from 'node:assert';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, test } from 'node:test';
import { resolveCredential } from '../src/credential-resolver.js';

const ENV_KEYS = ['GEMINI_VOICE_API_KEY', 'GEMINI_API_KEY'] as const;
let savedEnv: Record<string, string | undefined> = {};
let dir: string;

beforeEach(() => {
	savedEnv = {};
	for (const k of ENV_KEYS) { savedEnv[k] = process.env[k]; delete process.env[k]; }
	dir = mkdtempSync(join(tmpdir(), 'cred-resolver-'));
});

afterEach(() => {
	for (const k of ENV_KEYS) {
		if (savedEnv[k] === undefined) delete process.env[k];
		else process.env[k] = savedEnv[k];
	}
});

const missing = () => join(dir, 'no-such-file.json');

function writeManaged(caps: unknown): string {
	const p = join(dir, 'managed-credentials.json');
	writeFileSync(p, JSON.stringify({ version: 1, capabilities: caps }));
	return p;
}

test('legacy equivalence: no managed file, VOICE key wins', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	process.env.GEMINI_API_KEY = 'mk';
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: missing() }),
		{ key: 'vk', source: 'env' });
});

test('legacy equivalence: no managed file, MAIN-key fallback for voice', () => {
	process.env.GEMINI_API_KEY = 'mk';
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: missing() }),
		{ key: 'mk', source: 'env' });
});

test('legacy equivalence: nothing set → empty key, source none', () => {
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: missing() }),
		{ key: '', source: 'none' });
});

test('text capability never reads the voice env var', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	assert.deepEqual(resolveCredential('gemini-text', { managedPath: missing() }),
		{ key: '', source: 'none' });
});

test('managed tier beats env', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	const p = writeManaged({ 'gemini-voice': { key: 'managed-v' } });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'managed-v', source: 'managed' });
});

test('tier order beats slot order: managed TEXT beats env VOICE', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	const p = writeManaged({ 'gemini-text': { key: 'managed-t' } });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'managed-t', source: 'managed' });
});

test('empty managed key falls through to env', () => {
	process.env.GEMINI_API_KEY = 'mk';
	const p = writeManaged({ 'gemini-voice': { key: '' } });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'mk', source: 'env' });
});

test('malformed JSON skips managed tier, never throws', () => {
	const p = join(dir, 'managed-credentials.json');
	writeFileSync(p, '{not json');
	process.env.GEMINI_API_KEY = 'mk';
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'mk', source: 'env' });
});

test('wrong-shape capabilities (array / non-string key) skip managed tier', () => {
	process.env.GEMINI_API_KEY = 'mk';
	const pArr = join(dir, 'arr.json');
	writeFileSync(pArr, JSON.stringify({ version: 1, capabilities: [] }));
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: pArr }),
		{ key: 'mk', source: 'env' });
	const pNum = writeManaged({ 'gemini-voice': { key: 42 } });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: pNum }),
		{ key: 'mk', source: 'env' });
});
