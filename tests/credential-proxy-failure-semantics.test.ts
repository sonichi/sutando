/**
 * Hermetic tests for the credential proxy's failure semantics: never inject a
 * known-dead token (pass the client's credential through, or fail fast with a
 * legible 401), reload + retry once on an upstream 401 the proxy authored, and
 * keep the OAuth-endpoint failure backoff. Keychain, OAuth refresh, clock, and
 * the upstream are all injected — no network, no real keychain.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { createProxyServer, selectCred, type ProxyDeps } from '../skills/quota-tracker/scripts/credential-proxy.ts';

type Cred = { accessToken: string; refreshToken?: string; expiresAt?: number };
type Stored = { service: string; oauth: Cred } | null;

const HOUR = 3600_000;
let now = 1_700_000_000_000;
let keychain: Stored = null;
// Items after the primary — models the scoped/default keychain split.
let extraKeychainItems: Array<NonNullable<Stored>> = [];
let refreshCalls = 0;
let refreshResult: Cred | null = null;
let upstreamHits: Array<{ auth?: string; apiKey?: string; path?: string }> = [];
let upstreamHandler: (req: IncomingMessage, res: ServerResponse) => void = () => {};
let quotaWrites: Array<Record<string, string>> = [];
const servers: Server[] = [];

beforeEach(() => {
	now = 1_700_000_000_000;
	keychain = null;
	extraKeychainItems = [];
	refreshCalls = 0;
	refreshResult = null;
	upstreamHits = [];
	quotaWrites = [];
});

afterEach(async () => {
	await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

function listen(s: Server): Promise<number> {
	servers.push(s);
	return new Promise((resolve) =>
		s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port)));
}

async function startUpstream(): Promise<number> {
	return listen(createHttpServer((req, res) => {
		upstreamHits.push({
			auth: req.headers['authorization'] as string | undefined,
			apiKey: req.headers['x-api-key'] as string | undefined,
			path: req.url,
		});
		upstreamHandler(req, res);
	}));
}

async function startProxy(upstreamPort: number, extra: Partial<ProxyDeps> = {}): Promise<number> {
	return listen(createProxyServer({
		readCredCandidates: () =>
			[keychain, ...extraKeychainItems]
				.filter((k): k is NonNullable<Stored> => k !== null)
				.map((k) => ({ service: k.service, oauth: { ...k.oauth } })),
		writeCred: (service, oauth) => { keychain = { service, oauth: oauth as Cred }; return true; },
		refreshAccessToken: async () => { refreshCalls += 1; return refreshResult; },
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: (h) => { quotaWrites.push(h); },
		now: () => now,
		idleTimeoutMs: 5000,
		...extra,
	}));
}

function call(port: number, headers: Record<string, string> = {}): Promise<{ status: number; body: string }> {
	return new Promise((resolve, reject) => {
		const req = httpRequest(
			{ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST', headers },
			(res) => {
				const chunks: Buffer[] = [];
				res.on('data', (c) => chunks.push(c));
				res.on('end', () => resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString() }));
			},
		);
		req.on('error', reject);
		req.end('{"model":"x"}');
	});
}

const respond = (res: ServerResponse, status: number, body: string, headers: Record<string, string> = {}) => {
	res.writeHead(status, { 'content-type': 'application/json', ...headers });
	res.end(body);
};

test('expired token + refresh in fail-backoff + client Authorization → client credential passes through untouched', async () => {
	keychain = { service: 's', oauth: { accessToken: 'dead-stored-token-aaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	refreshResult = null; // refresh endpoint failing (revoked/rotated refresh token)
	upstreamHandler = (_req, res) => respond(res, 200, '{"ok":true}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { authorization: 'Bearer client-fresh-login-token' });
	assert.equal(r.status, 200);
	assert.equal(upstreamHits.length, 1);
	// The /login cure: the client's own (fresher) credential must survive.
	assert.equal(upstreamHits[0].auth, 'Bearer client-fresh-login-token');
	assert.equal(refreshCalls, 1);
});

test('expired token + refresh in fail-backoff + no client credential → fast distinct 401, upstream never contacted', async () => {
	keychain = { service: 's', oauth: { accessToken: 'dead-stored-token-aaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	refreshResult = null;
	upstreamHandler = (_req, res) => respond(res, 200, '{"ok":true}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort);
	assert.equal(r.status, 401);
	const parsed = JSON.parse(r.body);
	assert.equal(parsed.error?.type, 'authentication_error');
	assert.match(parsed.error?.message ?? '', /credential-proxy/);
	assert.match(parsed.error?.message ?? '', /\/login/);
	assert.equal(upstreamHits.length, 0, 'a known-dead token must never be forwarded pretending normality');
});

test('upstream 401 on injected token → keychain re-read finds fresh /login token → retry once succeeds', async () => {
	keychain = { service: 's', oauth: { accessToken: 'stale-but-unexpired-token-aa', expiresAt: now + HOUR } };
	upstreamHandler = (req, res) => {
		if (req.headers['authorization'] === 'Bearer stale-but-unexpired-token-aa') {
			// Simulate /login landing while the 401 is in flight.
			keychain = { service: 's', oauth: { accessToken: 'fresh-relogin-token-bbbbbbbb', expiresAt: now + HOUR } };
			respond(res, 401, '{"error":{"type":"authentication_error","message":"OAuth access token has expired"}}');
			return;
		}
		respond(res, 200, '{"recovered":true}');
	};
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 200);
	assert.equal(JSON.parse(r.body).recovered, true);
	assert.equal(upstreamHits.length, 2, 'exactly one retry');
	assert.equal(upstreamHits[1].auth, 'Bearer fresh-relogin-token-bbbbbbbb');
	assert.equal(refreshCalls, 0, 'keychain re-read sufficed; no OAuth-endpoint call needed');
});

test('upstream 401, keychain unchanged, refresh fails → 401 surfaces once, then the rejected token is never re-injected', async () => {
	// Revoked-but-unexpired token: expiry metadata says valid, upstream says no.
	keychain = { service: 's', oauth: { accessToken: 'revoked-unexpired-token-aaaa', refreshToken: 'rt', expiresAt: now + HOUR } };
	refreshResult = null;
	upstreamHandler = (req, res) => {
		if (req.headers['authorization'] === 'Bearer revoked-unexpired-token-aaaa') {
			respond(res, 401, '{"error":{"type":"authentication_error","message":"revoked"}}');
			return;
		}
		respond(res, 200, '{"ok":true}');
	};
	const proxyPort = await startProxy(await startUpstream());

	const r1 = await call(proxyPort, { authorization: 'Bearer client-own-token' });
	assert.equal(r1.status, 401, 'give up loud: the upstream 401 reaches the client');
	assert.equal(upstreamHits[0].auth, 'Bearer revoked-unexpired-token-aaaa');
	assert.equal(refreshCalls, 1);

	// Next request: the 401'd token is invalidated → client credential passes through.
	const r2 = await call(proxyPort, { authorization: 'Bearer client-own-token' });
	assert.equal(r2.status, 200);
	assert.equal(upstreamHits.at(-1)?.auth, 'Bearer client-own-token');
});

test('/login between requests: next request injects the new keychain token without a proxy restart', async () => {
	keychain = { service: 's', oauth: { accessToken: 'first-session-token-aaaaaaaa', expiresAt: now + HOUR } };
	upstreamHandler = (_req, res) => respond(res, 200, '{}');
	const proxyPort = await startProxy(await startUpstream());

	await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(upstreamHits[0].auth, 'Bearer first-session-token-aaaaaaaa');

	keychain = { service: 's', oauth: { accessToken: 'relogin-session-token-bbbbbb', expiresAt: now + HOUR } };
	await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(upstreamHits[1].auth, 'Bearer relogin-session-token-bbbbbb');
});

test('refresh failure keeps the anti-hammer backoff: one OAuth attempt per window, next attempt after it elapses', async () => {
	keychain = { service: 's', oauth: { accessToken: 'dead-stored-token-aaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	refreshResult = null;
	upstreamHandler = (_req, res) => respond(res, 200, '{}');
	const proxyPort = await startProxy(await startUpstream());

	for (let i = 0; i < 3; i++) await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(refreshCalls, 1, 'requests inside the backoff window must not re-hit the OAuth endpoint');

	now += 30_001; // past the first 30s backoff window
	await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(refreshCalls, 2);
});

test('successful refresh of a near-expiry token is injected (healthy path) and quota telemetry still captured', async () => {
	keychain = { service: 's', oauth: { accessToken: 'nearly-expired-token-aaaaaaa', refreshToken: 'rt', expiresAt: now + 60_000 } };
	refreshResult = { accessToken: 'refreshed-token-bbbbbbbbbbbb', refreshToken: 'rt2', expiresAt: now + 8 * HOUR };
	upstreamHandler = (_req, res) =>
		respond(res, 200, '{"ok":true}', { 'anthropic-ratelimit-unified-5h-utilization': '42' });
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 200);
	assert.equal(refreshCalls, 1);
	assert.equal(upstreamHits[0].auth, 'Bearer refreshed-token-bbbbbbbbbbbb');
	assert.equal(keychain?.oauth.accessToken, 'refreshed-token-bbbbbbbbbbbb', 'refreshed cred written back');
	assert.deepEqual(quotaWrites, [{ 'anthropic-ratelimit-unified-5h-utilization': '42' }]);
});

test('requests without Authorization (x-api-key auth) are forwarded untouched when the stored token is healthy', async () => {
	keychain = { service: 's', oauth: { accessToken: 'healthy-stored-token-aaaaaaa', expiresAt: now + HOUR } };
	upstreamHandler = (_req, res) => respond(res, 200, '{}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { 'x-api-key': 'sk-ant-user-key' });
	assert.equal(r.status, 200);
	assert.equal(upstreamHits[0].auth, undefined, 'no injection without a client Authorization header');
	assert.equal(upstreamHits[0].apiKey, 'sk-ant-user-key');
});

test('expired token + x-api-key request → passes through (the client credential is not an Authorization header)', async () => {
	keychain = { service: 's', oauth: { accessToken: 'dead-stored-token-aaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	refreshResult = null;
	upstreamHandler = (_req, res) => respond(res, 200, '{}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { 'x-api-key': 'sk-ant-user-key' });
	assert.equal(r.status, 200);
	assert.equal(upstreamHits.length, 1);
	assert.equal(upstreamHits[0].apiKey, 'sk-ant-user-key');
});

test('dead scoped item does not eclipse a fresh default-item /login (candidate fallback on read)', async () => {
	// /login from a vanilla terminal writes the DEFAULT item; the scoped item
	// still holds a dead cred whose refresh 400s.
	keychain = { service: 'scoped', oauth: { accessToken: 'dead-scoped-token-aaaaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	extraKeychainItems = [{ service: 'default', oauth: { accessToken: 'fresh-default-login-token-bb', expiresAt: now + HOUR } }];
	refreshResult = null;
	upstreamHandler = (_req, res) => respond(res, 200, '{}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 200);
	assert.equal(upstreamHits[0].auth, 'Bearer fresh-default-login-token-bb');
	assert.equal(refreshCalls, 0, 'a usable candidate needs no OAuth-endpoint call');
});

test('upstream 401 on scoped token → recovery finds a fresh token in the default item → retry succeeds', async () => {
	keychain = { service: 'scoped', oauth: { accessToken: 'revoked-scoped-token-aaaaaaa', expiresAt: now + HOUR } };
	upstreamHandler = (req, res) => {
		if (req.headers['authorization'] === 'Bearer revoked-scoped-token-aaaaaaa') {
			// /login (vanilla terminal) lands in the DEFAULT item mid-incident.
			extraKeychainItems = [{ service: 'default', oauth: { accessToken: 'fresh-default-login-token-bb', expiresAt: now + HOUR } }];
			respond(res, 401, '{"error":{"type":"authentication_error","message":"OAuth access token has expired"}}');
			return;
		}
		respond(res, 200, '{"recovered":true}');
	};
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 200);
	assert.equal(upstreamHits.length, 2);
	assert.equal(upstreamHits[1].auth, 'Bearer fresh-default-login-token-bb');
	assert.equal(refreshCalls, 0);
});

// --- selectCred: pure candidate-selection policy ---------------------------

const at = (service: string, accessToken: string, expiresAt?: number) => ({ service, oauth: { accessToken, expiresAt } });

test('selectCred prefers the scoped item when it is usable', () => {
	const picked = selectCred([at('scoped', 'scoped-token', now + HOUR), at('default', 'default-token', now + HOUR)], now, null);
	assert.equal(picked?.service, 'scoped');
});

test('selectCred skips an expired scoped item for a usable default item', () => {
	const picked = selectCred([at('scoped', 'scoped-token', now - 1), at('default', 'default-token', now + HOUR)], now, null);
	assert.equal(picked?.service, 'default');
});

test('selectCred skips an upstream-rejected token for a usable candidate', () => {
	const picked = selectCred([at('scoped', 'rejected-token', now + HOUR), at('default', 'default-token', now + HOUR)], now, 'rejected-token');
	assert.equal(picked?.service, 'default');
});

test('selectCred with no usable candidate falls back to the first present (degraded handling sees it)', () => {
	const picked = selectCred([at('scoped', 'scoped-token', now - 1), at('default', 'default-token', now - 1)], now, null);
	assert.equal(picked?.service, 'scoped');
});

test('selectCred with no candidates → null', () => {
	assert.equal(selectCred([], now, null), null);
});
