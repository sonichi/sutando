/**
 * credential-resolver — the G8 seam. Proves the drop-in property: with no
 * managed source the resolver is identical to the raw env chain; a registered
 * managed source supplies the token interchangeably (zero consumer change).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	resolveCredential,
	setManagedCredentialSource,
	getManagedCredentialSource,
} from '../src/credential-resolver.ts';

test('no managed source (default) → falls through to env chain, first non-empty wins', () => {
	setManagedCredentialSource(null);
	assert.equal(resolveCredential('gemini-voice', ['voicekey', 'mainkey']), 'voicekey');
	assert.equal(resolveCredential('gemini-voice', [undefined, 'mainkey']), 'mainkey');
	assert.equal(resolveCredential('gemini-voice', [undefined, '']), '');
	assert.equal(resolveCredential('gemini-voice', []), '');
});

test('managed source wins over env when it returns a token (managed-first default)', () => {
	setManagedCredentialSource({ get: (cap) => (cap === 'gemini-voice' ? 'managed-tok' : undefined) });
	try {
		assert.equal(resolveCredential('gemini-voice', ['voicekey', 'mainkey']), 'managed-tok');
		// A capability the managed source can't supply → falls through to env.
		assert.equal(resolveCredential('anthropic', ['anthr-byo']), 'anthr-byo');
	} finally {
		setManagedCredentialSource(null);
	}
});

test('managed source returning empty/undefined falls through to env (no accidental blanking)', () => {
	setManagedCredentialSource({ get: () => undefined });
	try {
		assert.equal(resolveCredential('gemini-voice', ['voicekey']), 'voicekey');
	} finally {
		setManagedCredentialSource(null);
	}
	setManagedCredentialSource({ get: () => '' });
	try {
		assert.equal(resolveCredential('gemini-voice', ['voicekey']), 'voicekey');
	} finally {
		setManagedCredentialSource(null);
	}
});

test('register/clear round-trips', () => {
	assert.equal(getManagedCredentialSource(), null);
	const src = { get: () => 'x' };
	setManagedCredentialSource(src);
	assert.equal(getManagedCredentialSource(), src);
	setManagedCredentialSource(null);
	assert.equal(getManagedCredentialSource(), null);
});
