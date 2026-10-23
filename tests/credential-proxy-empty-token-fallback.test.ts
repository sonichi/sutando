/**
 * A keychain item whose claudeAiOauth.accessToken is blank must read as absent,
 * so a blanked scoped item cannot eclipse a valid default item at startup.
 * The keychain read is injected — the real keychain is never touched.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { readCred, readCredCandidates } from '../skills/quota-tracker/scripts/credential-proxy.ts';

const SCOPED = 'Claude Code-credentials-deadbeef';
const DEFAULT = 'Claude Code-credentials';

function keychain(items: Record<string, string>): (service: string) => string {
	return (service) => {
		if (!(service in items)) throw new Error('item not found');
		return items[service];
	};
}

const item = (accessToken: string) => JSON.stringify({ claudeAiOauth: { accessToken, refreshToken: 'r' } });

test('scoped item with an empty token falls back to the valid default item', () => {
	const read = keychain({ [SCOPED]: item(''), [DEFAULT]: item('sk-default') });
	const cred = readCred([SCOPED, DEFAULT], read);
	assert.strictEqual(cred?.service, DEFAULT);
	assert.strictEqual(cred?.oauth.accessToken, 'sk-default');
});

test('a whitespace-only token is also treated as absent', () => {
	const read = keychain({ [SCOPED]: item('  \n'), [DEFAULT]: item('sk-default') });
	assert.strictEqual(readCred([SCOPED, DEFAULT], read)?.service, DEFAULT);
});

test('every candidate blank yields no credential', () => {
	const read = keychain({ [SCOPED]: item(''), [DEFAULT]: item(' ') });
	assert.deepStrictEqual(readCredCandidates([SCOPED, DEFAULT], read), []);
	assert.strictEqual(readCred([SCOPED, DEFAULT], read), null);
});

test('a valid scoped item still wins over the default item', () => {
	const read = keychain({ [SCOPED]: item('sk-scoped'), [DEFAULT]: item('sk-default') });
	assert.strictEqual(readCred([SCOPED, DEFAULT], read)?.oauth.accessToken, 'sk-scoped');
});
