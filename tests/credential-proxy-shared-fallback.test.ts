/**
 * The proxy must not put a client on the machine's SHARED login. When the
 * keychain item scoped to the proxy's CLAUDE_CONFIG_DIR is missing and only the
 * default `Claude Code-credentials` item exists, a request that carries its own
 * Authorization is forwarded untouched; the scoped item, when present, is
 * injected as before. The selected service is named once at start and on
 * change, by name only. Keychain (`security`) reads, the upstream and the clock
 * are injected — no network, no real keychain.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import {
	createProxyServer, readCredCandidates, keychainServiceCandidates, scopedKeychainService,
	isSharedFallback, describeKeychainSelection, type ProxyDeps,
} from '../skills/quota-tracker/scripts/credential-proxy.ts';

const CORE_DIR = '/ws/.claude-sutando';
const SCOPED = scopedKeychainService(CORE_DIR) as string;
const DEFAULT = 'Claude Code-credentials';
const HOUR = 3600_000;
const SCOPED_TOKEN = 'scoped-core-login-token-aaaa';
const SHARED_TOKEN = 'shared-machine-login-token-bb';
const CLIENT_TOKEN = 'client-own-login-token-cccccc';

let now = 1_700_000_000_000;
let items: Record<string, string> = {};
let securityReads: string[] = [];
let refreshCalls = 0;
let upstreamHits: Array<{ auth?: string }> = [];
let upstreamHandler: (req: IncomingMessage, res: ServerResponse) => void = () => {};
let quotaWrites: Array<Record<string, string>> = [];
let logs: string[] = [];
const servers: Server[] = [];
const realLog = console.log;
const realError = console.error;

const item = (accessToken: string, expiresAt = now + HOUR) =>
	JSON.stringify({ claudeAiOauth: { accessToken, refreshToken: 'refresh-token-never-logged-dd', expiresAt } });

// The fake `security` reader: throws for a missing item, as the real one does.
const fakeSecurity = (service: string): string => {
	securityReads.push(service);
	if (!(service in items)) throw new Error('SecKeychainSearchCopyNext: The specified item could not be found in the keychain.');
	return items[service];
};

beforeEach(() => {
	now = 1_700_000_000_000;
	items = {};
	securityReads = [];
	refreshCalls = 0;
	upstreamHits = [];
	quotaWrites = [];
	logs = [];
	upstreamHandler = (_req, res) => {
		res.writeHead(200, { 'content-type': 'application/json', 'anthropic-ratelimit-unified-status': 'allowed' });
		res.end('{"ok":true}');
	};
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
		scopedService: SCOPED,
		configDir: CORE_DIR,
		writeCred: () => true,
		refreshAccessToken: async () => { refreshCalls += 1; return null; },
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
	for (const secret of [SCOPED_TOKEN, SHARED_TOKEN, CLIENT_TOKEN, 'refresh-token-never-logged-dd']) {
		assert.ok(!logs.some((l) => l.includes(secret)), `a credential reached the log: ${secret.slice(0, 6)}…`);
	}
};

test('the scoped service is named from sha256(CLAUDE_CONFIG_DIR) and read before the shared one', () => {
	assert.match(SCOPED, /^Claude Code-credentials-[0-9a-f]{8}$/);
	items = { [SCOPED]: item(SCOPED_TOKEN), [DEFAULT]: item(SHARED_TOKEN) };
	const found = readCredCandidates(keychainServiceCandidates(CORE_DIR), fakeSecurity);
	assert.deepEqual(securityReads, [SCOPED, DEFAULT]);
	assert.equal(found[0].service, SCOPED);
	assert.equal(isSharedFallback(found[0], found, SCOPED), false);
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
	assert.ok(warnings[0].includes(`'${DEFAULT}'`), 'the warning names the shared item it fell back to');
	assert.ok(warnings[0].includes(CORE_DIR), 'the warning names the config dir the proxy was given');
	// The proxy still does its quota job on the passed-through request.
	assert.equal(quotaWrites.length, 3);
	assertNoTokenLogged();
});

test('shared fallback + client Authorization: the shared login is not refreshed either', async () => {
	// Near expiry would normally trigger a refresh, rotating another login's refresh token for nothing.
	items = { [DEFAULT]: item(SHARED_TOKEN, now + 60_000) };
	const port = await startProxy();

	await call(port, { authorization: `Bearer ${CLIENT_TOKEN}` });
	assert.equal(refreshCalls, 0);
	assert.equal(upstreamHits[0].auth, `Bearer ${CLIENT_TOKEN}`);
});

test('shared fallback: an upstream 401 on the client credential is returned as-is, never retried on the shared login', async () => {
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
		scopedService: null,
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
	assert.ok(selected[0].includes(`'${DEFAULT}'`));
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

test('describeKeychainSelection names services and the config dir only', () => {
	const scoped = describeKeychainSelection(SCOPED, SCOPED, false, CORE_DIR);
	assert.equal(scoped.info, `keychain service selected: '${SCOPED}' (scoped to CLAUDE_CONFIG_DIR=${CORE_DIR})`);
	assert.equal(scoped.warning, null);

	const fallback = describeKeychainSelection(DEFAULT, SCOPED, true, CORE_DIR);
	assert.equal(fallback.info, `keychain service selected: '${DEFAULT}' (shared default item)`);
	assert.match(fallback.warning ?? '', /^WARNING: no keychain item /);

	assert.equal(describeKeychainSelection(null, SCOPED, false, CORE_DIR).info, 'keychain service selected: none');
});

test('isSharedFallback: only the default item standing in for an ABSENT scoped item', () => {
	const at = (service: string) => ({ service, oauth: { accessToken: 't' } });
	assert.equal(isSharedFallback(at(DEFAULT), [at(DEFAULT)], SCOPED), true);
	assert.equal(isSharedFallback(at(SCOPED), [at(SCOPED), at(DEFAULT)], SCOPED), false);
	// A scoped item that exists but is unusable keeps selectCred's existing behaviour.
	assert.equal(isSharedFallback(at(DEFAULT), [at(SCOPED), at(DEFAULT)], SCOPED), false);
	assert.equal(isSharedFallback(at(DEFAULT), [at(DEFAULT)], null), false);
	assert.equal(isSharedFallback(null, [], SCOPED), false);
});
