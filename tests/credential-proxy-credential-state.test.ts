/**
 * Witnesses the credential_state signal (this PR's feature): the proxy must
 * report `exhausted` on each of the three terminal 401 paths and `ok` on the
 * next successful token injection. Nothing in this repo READS credential_state
 * (the consumer is the AG2 Space desktop app), so without this test a change
 * that drops the signal would surface nowhere — hence a test, not a nicety.
 *
 * The recorder is injected through ProxyDeps.recordCredentialState, so each
 * assertion pins the CALL SITE, not the module-private file write. Neutralizing
 * the feature (dropping a call site) makes the matching case fail. Keychain,
 * refresh, clock, upstream, and both quota recorders are injected — no network,
 * no real keychain, no real quota-state.json.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { createProxyServer, type ProxyDeps, type CredentialState } from '../skills/quota-tracker/scripts/credential-proxy.ts';

type Cred = { accessToken: string; refreshToken?: string; expiresAt?: number };
type Stored = { service: string; oauth: Cred } | null;

const HOUR = 3600_000;
let now = 1_700_000_000_000;
let keychain: Stored = null;
let refreshResult: Cred | null = null;
let upstreamHandler: (req: IncomingMessage, res: ServerResponse) => void = () => {};
let credStateCalls: Array<{ state: CredentialState; detail: string }> = [];
const servers: Server[] = [];

beforeEach(() => {
	now = 1_700_000_000_000;
	keychain = null;
	refreshResult = null;
	credStateCalls = [];
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
	return listen(createHttpServer((req, res) => upstreamHandler(req, res)));
}

async function startProxy(upstreamPort: number): Promise<number> {
	return listen(createProxyServer({
		readCredCandidates: () =>
			keychain ? [{ service: keychain.service, oauth: { ...keychain.oauth } }] : [],
		writeCred: (service, oauth) => { keychain = { service, oauth: oauth as Cred }; return true; },
		refreshAccessToken: async () => refreshResult,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: () => {},
		recordRejection: () => {},
		// The seam under test: capture every credential_state transition.
		recordCredentialState: (state, detail = '') => { credStateCalls.push({ state, detail }); },
		now: () => now,
		idleTimeoutMs: 5000,
	}));
}

function call(port: number, headers: Record<string, string> = {}): Promise<{ status: number }> {
	return new Promise((resolve, reject) => {
		const req = httpRequest(
			{ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST', headers },
			(res) => { res.resume(); res.on('end', () => resolve({ status: res.statusCode ?? 0 })); },
		);
		req.on('error', reject);
		req.end('{"model":"x"}');
	});
}

const respond = (res: ServerResponse, status: number, body: string) => {
	res.writeHead(status, { 'content-type': 'application/json' });
	res.end(body);
};

test('ok: a healthy stored token injected on the request → credential_state ok', async () => {
	keychain = { service: 's', oauth: { accessToken: 'healthy-stored-token-aaaaaaa', expiresAt: now + HOUR } };
	upstreamHandler = (_req, res) => respond(res, 200, '{"ok":true}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 200);
	assert.deepEqual(credStateCalls, [{ state: 'ok', detail: '' }]);
});

test('exhausted (fail-fast): dead token, refresh unavailable, no client credential → credential_state exhausted', async () => {
	keychain = { service: 's', oauth: { accessToken: 'dead-stored-token-aaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	refreshResult = null; // refresh endpoint failing → token stays dead
	upstreamHandler = (_req, res) => respond(res, 200, '{"ok":true}');
	const proxyPort = await startProxy(await startUpstream());

	const r = await call(proxyPort); // no client Authorization → fail fast
	assert.equal(r.status, 401);
	assert.equal(credStateCalls.length, 1);
	assert.equal(credStateCalls[0].state, 'exhausted');
	assert.match(credStateCalls[0].detail, /refresh unavailable/);
});

test('exhausted (post-401 recovery failure): injected token 401s, no fresh login, refresh fails → exhausted', async () => {
	// Revoked-but-unexpired: metadata says valid, upstream rejects; keychain
	// never changes and refresh fails, so reload→refresh→retry all fail.
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

	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 401, 'gives up loud: the upstream 401 reaches the client');
	assert.ok(
		credStateCalls.some((c) => c.state === 'exhausted' && /recovery failed/.test(c.detail)),
		`expected an exhausted (recovery failed) transition, got ${JSON.stringify(credStateCalls)}`,
	);
});

test('recovery: exhausted, then a fresh /login lands → the next request injects it and reports ok', async () => {
	keychain = { service: 's', oauth: { accessToken: 'dead-stored-token-aaaaaaaa', refreshToken: 'rt', expiresAt: now - 1000 } };
	refreshResult = null;
	upstreamHandler = (_req, res) => respond(res, 200, '{"ok":true}');
	const proxyPort = await startProxy(await startUpstream());

	await call(proxyPort); // exhausted
	assert.equal(credStateCalls.at(-1)?.state, 'exhausted');

	// /login lands a fresh, healthy credential.
	keychain = { service: 's', oauth: { accessToken: 'fresh-relogin-token-bbbbbbbb', expiresAt: now + HOUR } };
	const r = await call(proxyPort, { authorization: 'Bearer client-token' });
	assert.equal(r.status, 200);
	assert.equal(credStateCalls.at(-1)?.state, 'ok', 'a successful injection clears exhausted → ok');
});
