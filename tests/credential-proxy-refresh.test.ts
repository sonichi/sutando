/**
 * Offline unit test for #1767's OAuth-refresh response handling.
 *
 * Validates the highest-risk logic — field-name handling + the access-token
 * guard in parseRefreshResponse — WITHOUT any network, keychain, or token
 * rotation. The live round-trip (real endpoint behavior) is validated
 * separately on a proxy-routed node; this catches the parse/guard bugs that
 * would otherwise only surface in production.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { parseRefreshResponse } from '../skills/quota-tracker/scripts/credential-proxy.ts';

const base = { accessToken: 'old-access', refreshToken: 'old-refresh', expiresAt: 1000 };
const A = 'a'.repeat(40); // a plausible (>=20 char) access token

test('snake_case response → fresh cred, expiresAt from expires_in', () => {
	const r = parseRefreshResponse(200, JSON.stringify({ access_token: A, refresh_token: 'new-refresh', expires_in: 3600 }), base, 0);
	assert.equal(r?.accessToken, A);
	assert.equal(r?.refreshToken, 'new-refresh');
	assert.equal(r?.expiresAt, 3600 * 1000);
});

test('camelCase response is tolerated', () => {
	const r = parseRefreshResponse(200, JSON.stringify({ accessToken: A, refreshToken: 'r2', expiresAt: 9999 }), base, 0);
	assert.equal(r?.accessToken, A);
	assert.equal(r?.refreshToken, 'r2');
	assert.equal(r?.expiresAt, 9999);
});

test('no rotation in response → keeps the existing refresh token', () => {
	const r = parseRefreshResponse(200, JSON.stringify({ access_token: A, expires_in: 60 }), base, 0);
	assert.equal(r?.refreshToken, 'old-refresh');
});

test('HTTP >=400 → null (no write)', () => {
	assert.equal(parseRefreshResponse(401, JSON.stringify({ access_token: A }), base), null);
	assert.equal(parseRefreshResponse(500, JSON.stringify({ access_token: A }), base), null);
});

test('garbage / non-JSON body → null', () => {
	assert.equal(parseRefreshResponse(200, 'not-json', base), null);
	assert.equal(parseRefreshResponse(200, '', base), null);
});

test('missing access token → null', () => {
	assert.equal(parseRefreshResponse(200, JSON.stringify({ refresh_token: 'x' }), base), null);
});

test('short access token (<20 chars) → null (the anti-garbage guard)', () => {
	assert.equal(parseRefreshResponse(200, JSON.stringify({ access_token: 'short' }), base), null);
});

test('non-string access token → null', () => {
	assert.equal(parseRefreshResponse(200, JSON.stringify({ access_token: 12345 }), base), null);
});

test('preserves unrelated existing fields (scopes etc.)', () => {
	const withScopes = { ...base, scopes: ['a', 'b'], subscriptionType: 'max' };
	const r = parseRefreshResponse(200, JSON.stringify({ access_token: A, expires_in: 10 }), withScopes, 0);
	assert.deepEqual(r?.scopes, ['a', 'b']);
	assert.equal(r?.subscriptionType, 'max');
});
