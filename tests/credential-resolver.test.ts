/**
 * credential-resolver (G8) — chain semantics + legacy-equivalence pins.
 *
 * The load-bearing claims under test:
 *  1. No managed file → resolution is IDENTICAL to the legacy env chain
 *     (GEMINI_VOICE_API_KEY → GEMINI_API_KEY → ''), source 'env'/'none'.
 *  2. Managed tier wins over env when present; voice falls back to the managed
 *     text credential BEFORE dropping to env (tier order beats slot order).
 *  3. Malformed/wrong-shape managed files skip the tier — never throw.
 *  4. The S1 `voicePreference`/`quarantined` truth table (design 2b):
 *     unset ⇒ legacy managed→env; 'managed' ⇒ ONLY a non-quarantined managed
 *     entry satisfies (env keys never silently satisfy it); 'byok' ⇒ env
 *     only; quarantined entries are absent in EVERY mode.
 *  5. S3/R15 read side: opaque generations are REPORTED (managed `generation`
 *     field / SUTANDO_VOICE_CREDENTIAL_GENERATION), never minted; top-level
 *     `preferenceRevision`/`sessionRevision` are tolerated and ignored.
 *
 * TWIN: tests/credential-resolver.test.py mirrors this file one-for-one; any
 * contract change must land in both (policy-twin lesson, #2516).
 */
import { strict as assert } from 'node:assert';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, test } from 'node:test';
import { credentialSourceLabel, resolveCredential } from '../src/credential-resolver.js';

const ENV_KEYS = [
	'GEMINI_VOICE_API_KEY',
	'GEMINI_API_KEY',
	'SUTANDO_VOICE_CREDENTIAL_GENERATION',
] as const;
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

function writeManaged(caps: unknown, topLevel: Record<string, unknown> = {}): string {
	const p = join(dir, 'managed-credentials.json');
	writeFileSync(p, JSON.stringify({ version: 1, capabilities: caps, ...topLevel }));
	return p;
}

/** Both managed slots filled — the design's canonical S1 fixture shape. */
const BOTH_SLOTS = {
	'gemini-voice': { key: 'managed-v' },
	'gemini-text': { key: 'managed-t' },
};

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

test('non-object ROOT document: caps empty, preference unset, not quarantined', () => {
	process.env.GEMINI_API_KEY = 'mk';
	const p = join(dir, 'root-arr.json');
	writeFileSync(p, JSON.stringify([1, 2]));
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'mk', source: 'env' });
});

// --- S1 truth table: voicePreference × quarantined (design 2b) --------------

test('byok preference: managed voice+text entries + env key → env wins', () => {
	process.env.GEMINI_API_KEY = 'byo-mk';
	const p = writeManaged(BOTH_SLOTS, { voicePreference: 'byok' });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'byo-mk', source: 'env' });
});

test('byok preference + NO env key → none (the "fail actionably" input)', () => {
	const p = writeManaged(BOTH_SLOTS, { voicePreference: 'byok' });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: '', source: 'none' });
});

test('managed preference: non-quarantined managed entry satisfies (env key irrelevant)', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	const p = writeManaged(BOTH_SLOTS, { voicePreference: 'managed' });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'managed-v', source: 'managed' });
});

test('S1: managed preference + env key + managed entries MISSING → none, never env', () => {
	// The logout-quarantine bypass row: a present env key must NOT silently
	// satisfy a managed preference.
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	process.env.GEMINI_API_KEY = 'mk';
	const p = writeManaged({}, { voicePreference: 'managed' });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: '', source: 'none' });
});

test('S1: managed preference + env key + QUARANTINED entries → none, never env', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	const p = writeManaged(BOTH_SLOTS, { voicePreference: 'managed', quarantined: true });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: '', source: 'none' });
});

test('quarantined (unset preference): managed entries absent → env fallback', () => {
	process.env.GEMINI_API_KEY = 'mk';
	const p = writeManaged(BOTH_SLOTS, { quarantined: true });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'mk', source: 'env' });
});

test('quarantined (unset preference) + no env key → none', () => {
	const p = writeManaged(BOTH_SLOTS, { quarantined: true });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: '', source: 'none' });
});

test('quarantined only as strict JSON true; false/absent keep the tier', () => {
	const p = writeManaged(BOTH_SLOTS, { quarantined: false });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'managed-v', source: 'managed' });
});

test('R15 read side: revisions + unset preference → byte-identical legacy behavior', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	const p = writeManaged(BOTH_SLOTS, { preferenceRevision: 7, sessionRevision: 3 });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'managed-v', source: 'managed' });
});

test('out-of-vocabulary voicePreference reads as unset (legacy walk)', () => {
	for (const bad of ['MANAGED', 'Byok', 42, null, {}]) {
		const p = writeManaged(BOTH_SLOTS, { voicePreference: bad });
		assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
			{ key: 'managed-v', source: 'managed' }, `voicePreference=${JSON.stringify(bad)}`);
	}
});

test('voicePreference scopes VOICE: gemini-text ignores byok; quarantine still hides it', () => {
	const p = writeManaged(BOTH_SLOTS, { voicePreference: 'byok' });
	assert.deepEqual(resolveCredential('gemini-text', { managedPath: p }),
		{ key: 'managed-t', source: 'managed' });
	const q = writeManaged(BOTH_SLOTS, { voicePreference: 'byok', quarantined: true });
	assert.deepEqual(resolveCredential('gemini-text', { managedPath: q }),
		{ key: '', source: 'none' });
});

// --- S3/Y4 read side: opaque generation reporting ---------------------------

test('managed entry generation is reported verbatim; legacy entries omit it', () => {
	const p = writeManaged({ 'gemini-voice': { key: 'managed-v', generation: 'cg1-abc' } });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: p }),
		{ key: 'managed-v', source: 'managed', credentialGeneration: 'cg1-abc' });
	const legacy = writeManaged({ 'gemini-voice': { key: 'managed-v' } });
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: legacy }),
		{ key: 'managed-v', source: 'managed' });
});

test('env voice key reports SUTANDO_VOICE_CREDENTIAL_GENERATION only when injected', () => {
	process.env.GEMINI_VOICE_API_KEY = 'vk';
	process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION = 'cg1-injected';
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: missing() }),
		{ key: 'vk', source: 'env', credentialGeneration: 'cg1-injected' });
	delete process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION;
	// Y4/Z4: manual/legacy env keys stay generationless.
	assert.deepEqual(resolveCredential('gemini-voice', { managedPath: missing() }),
		{ key: 'vk', source: 'env' });
});

test('gemini-text env key never picks up the VOICE generation env var', () => {
	process.env.GEMINI_API_KEY = 'mk';
	process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION = 'cg1-injected';
	assert.deepEqual(resolveCredential('gemini-text', { managedPath: missing() }),
		{ key: 'mk', source: 'env' });
});

// --- credentialSourceLabel: the design's user-facing vocabulary -------------

test("credentialSourceLabel: managed→managed, env→byok, none→none", () => {
	assert.equal(credentialSourceLabel('managed'), 'managed');
	assert.equal(credentialSourceLabel('env'), 'byok');
	assert.equal(credentialSourceLabel('none'), 'none');
});
