/**
 * The proxy must not put a client on the machine's SHARED login. With a config
 * dir set it injects, refreshes and retries ONLY within the keychain item scoped
 * to that dir. When that item is missing, or its token is rejected or expired
 * and cannot be refreshed, a request that carries its own credential is
 * forwarded untouched and one without gets a 401; the default
 * `Claude Code-credentials` item is never used in its place. With no config dir
 * the default item is used as before. The selected service is named once at
 * start and on change, by name only. Keychain (`security`) reads, the upstream and the clock
 * are injected — no network, no real keychain.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import {
	createProxyServer, readCredCandidates, keychainServiceCandidates, scopedKeychainService,
	ownCandidates, describeKeychainSelection, type ProxyDeps,
} from '../skills/quota-tracker/scripts/credential-proxy.ts';

const CORE_DIR = '/ws/.claude-sutando';
const SCOPED = scopedKeychainService(CORE_DIR) as string;
const DEFAULT = 'Claude Code-credentials';
const HOUR = 3600_000;
const SCOPED_TOKEN = 'scoped-core-login-token-aaaa';
const SHARED_TOKEN = 'shared-machine-login-token-bb';
const CLIENT_TOKEN = 'client-own-login-token-cccccc';
const REFRESHED_TOKEN = 'scoped-refreshed-token-eeeeee';

let now = 1_700_000_000_000;
let items: Record<string, string> = {};
let securityReads: string[] = [];
let refreshCalls = 0;
let refreshWorks = false;
let upstreamHits: Array<{ auth?: string }> = [];
let upstreamHandler: (req: IncomingMessage, res: ServerResponse) => void = () => {};
let quotaWrites: Array<Record<string, string>> = [];
let logs: string[] = [];
const servers: Server[] = [];
const realLog = console.log;
const realError = console.error;

const item = (accessToken: string, expiresAt = now + HOUR, withRefresh = true) =>
	JSON.stringify({ claudeAiOauth: { accessToken, expiresAt, ...(withRefresh ? { refreshToken: 'refresh-token-never-logged-dd' } : {}) } });

// The fake `security` reader: throws for a missing item, as the real one does.
const fakeSecurity = (service: string): string => {
	securityReads.push(service);
	if (!(service in items)) throw new Error('SecKeychainSearchCopyNext: The specified item could not be found in the keychain.');
	return items[service];
};

const ok = (_req: IncomingMessage, res: ServerResponse) => {
	res.writeHead(200, { 'content-type': 'application/json', 'anthropic-ratelimit-unified-status': 'allowed' });
	res.end('{"ok":true}');
};
// Upstream that 401s exactly the given tokens and serves every other credential.
const rejecting = (...dead: string[]) => (req: IncomingMessage, res: ServerResponse) => {
	if (dead.some((t) => req.headers['authorization'] === `Bearer ${t}`)) {
		res.writeHead(401, { 'content-type': 'application/json' });
		res.end('{"error":{"type":"authentication_error"}}');
		return;
	}
	ok(req, res);
};

beforeEach(() => {
	now = 1_700_000_000_000;
	items = {};
	securityReads = [];
	refreshCalls = 0;
	refreshWorks = false;
	upstreamHits = [];
	quotaWrites = [];
	logs = [];
	upstreamHandler = ok;
	console.log = (...a: unknown[]) => { logs.push(a.map(String).join(' ')); };
	console.error = (...a: unknown[]) => { logs.push(a.map(String).join(' ')); };
});

afterEach(async () => {
	console.log = realLog;
	console.error = realError;
	await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

function listen(s: Server): Promise<number> {
	servers.push(s);
	return new Promise((resolve) =>
		s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port)));
}

async function startProxy(extra: Partial<ProxyDeps> = {}): Promise<number> {
	const upstreamPort = await listen(createHttpServer((req, res) => {
		upstreamHits.push({ auth: req.headers['authorization'] as string | undefined });
		upstreamHandler(req, res);
	}));
	return listen(createProxyServer({
		readCredCandidates: () => readCredCandidates(keychainServiceCandidates(CORE_DIR), fakeSecurity),
		scopedItem: SCOPED,
		configDir: CORE_DIR,
		// A refresh lands in the item it was read from, as the real writeCred does.
		writeCred: (service, oauth) => { items[service] = JSON.stringify({ claudeAiOauth: oauth }); return true; },
		refreshAccessToken: async (oauth) => {
			refreshCalls += 1;
			return refreshWorks ? { ...oauth, accessToken: REFRESHED_TOKEN, expiresAt: now + HOUR } : null;
		},
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: (h) => { quotaWrites.push(h); },
		recordRejection: () => {},
		recordCredentialState: () => {},
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

const matching = (re: RegExp) => logs.filter((l) => re.test(l));
const assertNoTokenLogged = () => {
	for (const secret of [SCOPED_TOKEN, SHARED_TOKEN, CLIENT_TOKEN, REFRESHED_TOKEN, 'refresh-token-never-logged-dd']) {
		assert.ok(!logs.some((l) => l.includes(secret)), `a credential reached the log: ${secret.slice(0, 6)}…`);
	}
};

test('the scoped service is named from sha256(CLAUDE_CONFIG_DIR) and read before the shared one', () => {
	assert.match(SCOPED, /^Claude Code-credentials-[0-9a-f]{8}$/);
	items = { [SCOPED]: item(SCOPED_TOKEN), [DEFAULT]: item(SHARED_TOKEN) };
	const found = readCredCandidates(keychainServiceCandidates(CORE_DIR), fakeSecurity);
	assert.deepEqual(securityReads, [SCOPED, DEFAULT]);
	assert.equal(found[0].service, SCOPED);
	assert.deepEqual(ownCandidates(found, SCOPED).map((c) => c.service), [SCOPED]);
});

test('scoped item present → its token is injected over the client Authorization, as before', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN), [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy();

	const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(r.status, 200);
	assert.equal(upstreamHits[0].auth, `Bearer ${SCOPED_TOKEN}`);
	assert.equal(matching(/WARNING/).length, 0);
	assertNoTokenLogged();
});

test('scoped item missing + shared present + client Authorization → client credential forwarded untouched, warned once', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy();

	for (let i = 0; i < 3; i++) {
		const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
		assert.equal(r.status, 200);
	}
	assert.equal(upstreamHits.length, 3);
	for (const hit of upstreamHits) {
		assert.equal(hit.auth, `Bearer ${CLIENT_TOKEN}`, 'the shared login must not replace the client credential');
	}
	const warnings = matching(/WARNING/);
	assert.equal(warnings.length, 1, 'one warning, not one per request');
	assert.ok(warnings[0].includes(SCOPED), 'the warning names the missing scoped item');
	assert.ok(warnings[0].includes(`'${DEFAULT}'`), 'the warning names the shared item it does not use');
	assert.ok(warnings[0].includes(CORE_DIR), 'the warning names the config dir the proxy was given');
	// The proxy still does its quota job on the passed-through request.
	assert.equal(quotaWrites.length, 3);
	assertNoTokenLogged();
});

test('scoped item missing + client Authorization: the shared login is not refreshed either', async () => {
	// Near expiry would normally trigger a refresh, rotating another login's refresh token for nothing.
	items = { [DEFAULT]: item(SHARED_TOKEN, now + 60_000) };
	const port = await startProxy();

	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(refreshCalls, 0);
	assert.equal(upstreamHits[0].auth, `Bearer ${CLIENT_TOKEN}`);
});

test('scoped item missing: an upstream 401 on the client credential is returned as-is, never retried on the shared login', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN) };
	upstreamHandler = (_req, res) => {
		res.writeHead(401, { 'content-type': 'application/json' });
		res.end('{"error":{"type":"authentication_error"}}');
	};
	const port = await startProxy();

	const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(r.status, 401);
	assert.equal(upstreamHits.length, 1);
	assert.equal(upstreamHits[0].auth, `Bearer ${CLIENT_TOKEN}`);
});

test('no CLAUDE_CONFIG_DIR: the default item is the right one, not a fallback — injected as before', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy({
		readCredCandidates: () => readCredCandidates(keychainServiceCandidates(''), fakeSecurity),
		scopedItem: null,
		configDir: '',
	});

	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(upstreamHits[0].auth, `Bearer ${SHARED_TOKEN}`);
	assert.equal(matching(/WARNING/).length, 0);
});

test('scoped item appears (/login in the core) → the next request injects it, and the change is logged', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy();
	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(upstreamHits[0].auth, `Bearer ${CLIENT_TOKEN}`);

	items[SCOPED] = item(SCOPED_TOKEN);
	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(upstreamHits[1].auth, `Bearer ${SCOPED_TOKEN}`);

	const selected = matching(/keychain service selected/);
	assert.equal(selected.length, 2, 'once at start, once on change');
	assert.ok(selected[0].endsWith('keychain service selected: none'), 'the shared item is not selected in its place');
	assert.ok(selected[1].includes(`'${SCOPED}'`));
	assertNoTokenLogged();
});

test('the selected service is named once at start, before any request', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN), [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy();
	const atStart = matching(/keychain service selected/);
	assert.equal(atStart.length, 1);
	assert.ok(atStart[0].includes(`'${SCOPED}'`));

	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(matching(/keychain service selected/).length, 1, 'not repeated per request');
	assertNoTokenLogged();
});

test('scoped item missing + no client credential → 401 from the proxy, upstream never contacted', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy();

	const r = await call(port);
	assert.equal(r.status, 401);
	assert.match(JSON.parse(r.body).error?.message ?? '', /credential-proxy: no login is stored/);
	assert.equal(upstreamHits.length, 0);
});

// --- a scoped token that goes bad: recovery stays inside the scoped item -------

test('(a) scoped token 401 with a refresh token → refreshed within the scoped item, retried once', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN), [DEFAULT]: item(SHARED_TOKEN) };
	refreshWorks = true;
	upstreamHandler = rejecting(SCOPED_TOKEN);
	const port = await startProxy();

	const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(r.status, 200);
	assert.deepEqual(upstreamHits.map((h) => h.auth), [`Bearer ${SCOPED_TOKEN}`, `Bearer ${REFRESHED_TOKEN}`]);
	assert.equal(refreshCalls, 1);
	assert.equal(JSON.parse(items[SCOPED]).claudeAiOauth.accessToken, REFRESHED_TOKEN, 'written back to the scoped item');
	assert.equal(JSON.parse(items[DEFAULT]).claudeAiOauth.accessToken, SHARED_TOKEN, 'the shared item is untouched');
	assert.equal(matching(/WARNING/).length, 0);
	assertNoTokenLogged();
});

test('(b) scoped token 401, no refresh token, client Authorization → client credential forwarded; shared never used; warned once', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN, now + HOUR, false), [DEFAULT]: item(SHARED_TOKEN) };
	upstreamHandler = rejecting(SCOPED_TOKEN);
	const port = await startProxy();

	for (let i = 0; i < 3; i++) {
		const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
		assert.equal(r.status, 200);
	}
	assert.deepEqual(upstreamHits.map((h) => h.auth), [
		`Bearer ${SCOPED_TOKEN}`, `Bearer ${CLIENT_TOKEN}`, `Bearer ${CLIENT_TOKEN}`, `Bearer ${CLIENT_TOKEN}`,
	]);
	assert.equal(refreshCalls, 0);
	const warnings = matching(/WARNING/);
	assert.equal(warnings.length, 1, 'one warning, not one per request');
	assert.ok(warnings[0].includes(SCOPED) && /rejected/.test(warnings[0]));
	assertNoTokenLogged();
});

test('(b2) scoped token 401, refresh fails, client Authorization → same: client forwarded, shared never used', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN), [DEFAULT]: item(SHARED_TOKEN) };
	upstreamHandler = rejecting(SCOPED_TOKEN);
	const port = await startProxy();

	for (let i = 0; i < 2; i++) {
		const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
		assert.equal(r.status, 200);
	}
	assert.deepEqual(upstreamHits.map((h) => h.auth), [
		`Bearer ${SCOPED_TOKEN}`, `Bearer ${CLIENT_TOKEN}`, `Bearer ${CLIENT_TOKEN}`,
	]);
	assert.equal(refreshCalls, 1, 'one attempt, then the failure backoff');
});

test('(c) scoped token rejected, no refresh token, no client credential → 401, shared never used', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN, now + HOUR, false), [DEFAULT]: item(SHARED_TOKEN) };
	upstreamHandler = rejecting(SCOPED_TOKEN, CLIENT_TOKEN);
	const port = await startProxy();

	// The client holds a dead token too: its 401 comes back, not a retry on the shared login.
	const first = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(first.status, 401);
	// A request with no credential at all gets the proxy's own 401.
	const second = await call(port);
	assert.equal(second.status, 401);
	assert.match(JSON.parse(second.body).error?.message ?? '', /credential-proxy/);

	assert.deepEqual(upstreamHits.map((h) => h.auth), [`Bearer ${SCOPED_TOKEN}`, `Bearer ${CLIENT_TOKEN}`]);
	assert.ok(!upstreamHits.some((h) => h.auth === `Bearer ${SHARED_TOKEN}`));
});

test('(d) scoped token expired + refresh fails → client credential forwarded, or 401 without one; shared never used', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN, now - 1000), [DEFAULT]: item(SHARED_TOKEN) };
	const port = await startProxy();

	const withCred = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(withCred.status, 200);
	const without = await call(port);
	assert.equal(without.status, 401);
	assert.match(JSON.parse(without.body).error?.message ?? '', /credential-proxy/);

	assert.deepEqual(upstreamHits.map((h) => h.auth), [`Bearer ${CLIENT_TOKEN}`]);
	assert.equal(refreshCalls, 1);
	const warnings = matching(/WARNING/);
	assert.equal(warnings.length, 1);
	assert.ok(/expired/.test(warnings[0]));
});

test('(d2) scoped token expired with no refresh token → same, and no refresh is attempted on the shared item', async () => {
	items = { [SCOPED]: item(SCOPED_TOKEN, now - 1000, false), [DEFAULT]: item(SHARED_TOKEN, now + 60_000) };
	const port = await startProxy();

	const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(r.status, 200);
	assert.deepEqual(upstreamHits.map((h) => h.auth), [`Bearer ${CLIENT_TOKEN}`]);
	assert.equal(refreshCalls, 0);
});

test('(e) no config dir: a rejected default token still recovers by refresh and is injected, as before', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN) };
	refreshWorks = true;
	upstreamHandler = rejecting(SHARED_TOKEN);
	const port = await startProxy({
		readCredCandidates: () => readCredCandidates(keychainServiceCandidates(''), fakeSecurity),
		scopedItem: null,
		configDir: '',
	});

	const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(r.status, 200);
	assert.deepEqual(upstreamHits.map((h) => h.auth), [`Bearer ${SHARED_TOKEN}`, `Bearer ${REFRESHED_TOKEN}`]);
	assert.equal(matching(/WARNING/).length, 0);
});

test('(e2) no config dir: recovery fails → the upstream 401 is returned, the client credential is not substituted', async () => {
	items = { [DEFAULT]: item(SHARED_TOKEN, now + HOUR, false) };
	upstreamHandler = rejecting(SHARED_TOKEN);
	const port = await startProxy({
		readCredCandidates: () => readCredCandidates(keychainServiceCandidates(''), fakeSecurity),
		scopedItem: null,
		configDir: '',
	});

	const r = await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(r.status, 401);
	assert.deepEqual(upstreamHits.map((h) => h.auth), [`Bearer ${SHARED_TOKEN}`]);
});

test('describeKeychainSelection names services and the config dir only', () => {
	const scoped = describeKeychainSelection(SCOPED, SCOPED, CORE_DIR);
	assert.equal(scoped.info, `keychain service selected: '${SCOPED}' (scoped to CLAUDE_CONFIG_DIR=${CORE_DIR})`);
	assert.equal(scoped.warning, null);

	const missing = describeKeychainSelection(null, SCOPED, CORE_DIR);
	assert.equal(missing.info, 'keychain service selected: none');
	assert.match(missing.warning ?? '', /^WARNING: no keychain item /);

	const plain = describeKeychainSelection(DEFAULT, null, '');
	assert.equal(plain.info, `keychain service selected: '${DEFAULT}' (shared default item; no CLAUDE_CONFIG_DIR set)`);
	assert.equal(plain.warning, null);
	assert.equal(describeKeychainSelection(null, null, '').warning, null);
});

test('ownCandidates: only the scoped item when a config dir is set, everything otherwise', () => {
	const at = (service: string) => ({ service, oauth: { accessToken: 't' } });
	assert.deepEqual(ownCandidates([at(SCOPED), at(DEFAULT)], SCOPED), [at(SCOPED)]);
	assert.deepEqual(ownCandidates([at(DEFAULT)], SCOPED), []);
	assert.deepEqual(ownCandidates([at(DEFAULT)], null), [at(DEFAULT)]);
});
