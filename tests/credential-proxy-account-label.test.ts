/**
 * A quota reading and a rejection name the account of the credential their request was sent with:
 * a token the proxy took from this config dir's own keychain item, with that dir's account unchanged
 * across the pick. A passed-through or otherwise unbound credential names none (usage unknown).
 */
import { test, afterEach } from 'node:test';
import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { accountLabel, boundAccount, createProxyServer, quotaStateFromHeaders, recordWithRejection, type ProxyDeps, type RejectionRecord, type StoredClaudeOAuth } from '../skills/quota-tracker/scripts/credential-proxy.ts';

const NOW = 1_700_000_000_000;
let upstreamHandler: (req: IncomingMessage, res: ServerResponse) => void = () => {};
const servers: Server[] = [];
afterEach(async () => {
	await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

function listen(s: Server): Promise<number> {
	servers.push(s);
	return new Promise((resolve) => s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port)));
}

type Seen = Array<{ model?: string; account?: string | null }>;

interface Setup {
	account: string | null;
	creds?: StoredClaudeOAuth[];
	rejections?: RejectionRecord[];
}

async function startProxy(seen: Seen, current: Setup): Promise<number> {
	const upstreamPort = await listen(createHttpServer((req, res) => upstreamHandler(req, res)));
	const deps: Partial<ProxyDeps> = {
		readCredCandidates: () => current.creds ?? [{ service: 'scoped', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: (_headers, model, account) => { seen.push({ model, account }); },
		readAccount: () => current.account,
		scopedService: () => 'scoped',
		recordRejection: (rej) => { current.rejections?.push(rej); },
		recordCredentialState: () => {},
		now: () => NOW,
		idleTimeoutMs: 5000,
	};
	return listen(createProxyServer(deps));
}

function call(port: number): Promise<number> {
	return new Promise((resolve, reject) => {
		const req = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST', headers: { authorization: 'Bearer x' } }, (res) => {
			res.resume();
			res.on('end', () => resolve(res.statusCode ?? 0));
		});
		req.on('error', reject);
		req.end('{"model":"claude-opus-5-5","messages":[]}');
	});
}

const headersOnly = (_req: IncomingMessage, res: ServerResponse) => {
	res.writeHead(200, { 'anthropic-ratelimit-unified-7d-utilization': '0.9' });
	res.end('{}');
};

test('a response that lands after an account switch keeps the account its request was sent with', async () => {
	const current: Setup = { account: 'acct_aaaaaa' };
	upstreamHandler = (req, res) => {
		current.account = 'acct_bbbbbb'; // the seat switches while A's request is in flight
		headersOnly(req, res);
	};
	const seen: Seen = [];
	const port = await startProxy(seen, current);
	assert.strictEqual(await call(port), 200);
	assert.deepStrictEqual(seen, [{ model: 'claude-opus-5-5', account: 'acct_aaaaaa' }]);
	assert.strictEqual(await call(port), 200);
	assert.strictEqual(seen[1].account, 'acct_bbbbbb', 'a request sent after the switch is B\'s');
});

test('a credential the proxy did not take from its own config dir\'s item names no account', async () => {
	upstreamHandler = headersOnly;
	const seen: Seen = [];
	// Pass-through: no keychain credential, the request's own auth goes out unchanged.
	let port = await startProxy(seen, { account: 'acct_aaaaaa', creds: [] });
	assert.strictEqual(await call(port), 200);
	// The vanilla item belongs to no config dir.
	port = await startProxy(seen, { account: 'acct_aaaaaa', creds: [{ service: 'vanilla', oauth: { accessToken: 't', expiresAt: NOW + 3600_000 } }] });
	assert.strictEqual(await call(port), 200);
	// A dead stored token over a client credential: passed through.
	port = await startProxy(seen, { account: 'acct_aaaaaa', creds: [{ service: 'scoped', oauth: { accessToken: 't', expiresAt: NOW - 1 } }] });
	assert.strictEqual(await call(port), 200);
	assert.deepStrictEqual(seen.map((x) => x.account), [null, null, null]);
});

test('a late rejection carries its request\'s account and cannot end another account\'s overage', async () => {
	const current: Setup = { account: 'acct_aaaaaa', rejections: [] };
	upstreamHandler = (_req, res) => {
		current.account = 'acct_bbbbbb';
		res.writeHead(429, {});
		res.end('{"type":"error"}');
	};
	const port = await startProxy([], current);
	assert.strictEqual(await call(port), 429);
	await new Promise((r) => setTimeout(r, 20));
	assert.strictEqual(current.rejections?.[0]?.account, 'acct_aaaaaa');
	const overage = {
		account: 'acct_bbbbbb', last_checked: '2026-10-09T12:00:00.000Z', available: true, limit_state: 'overage',
		headers: { 'anthropic-ratelimit-unified-status': 'rejected', 'anthropic-ratelimit-unified-7d-status': 'rejected',
			'anthropic-ratelimit-unified-overage-status': 'allowed' },
	};
	const late = { ...(current.rejections as RejectionRecord[])[0], ts: '2026-10-09T12:00:05.000Z' };
	const next = recordWithRejection(overage, late);
	assert.deepStrictEqual([next.limit_state, next.available], ['overage', true], 'A\'s 429 is not B\'s limit');
	const own = recordWithRejection({ ...overage, account: 'acct_aaaaaa' }, late);
	assert.deepStrictEqual([own.limit_state, own.available], ['rejected', false], 'the record\'s own account');
});

test('boundAccount: the scoped item, and the same account on both sides of the pick', () => {
	assert.strictEqual(boundAccount('scoped', 'scoped', 'acct_aaaaaa', 'acct_aaaaaa'), 'acct_aaaaaa');
	assert.strictEqual(boundAccount('scoped', 'scoped', 'acct_aaaaaa', 'acct_bbbbbb'), null, 'switched during the pick');
	assert.strictEqual(boundAccount('vanilla', 'scoped', 'acct_aaaaaa', 'acct_aaaaaa'), null);
	assert.strictEqual(boundAccount('scoped', null, 'acct_aaaaaa', 'acct_aaaaaa'), null, 'no config dir');
	assert.strictEqual(boundAccount('scoped', 'scoped', null, null), null);
});

test('the record carries the label; a write without one names no account, even over a labeled one', () => {
	const h = { 'anthropic-ratelimit-unified-7d-utilization': '0.5' };
	const labeled = quotaStateFromHeaders({}, h, 'm', 'T1', 'acct_aaaaaa');
	assert.strictEqual(labeled.account, 'acct_aaaaaa');
	assert.ok(!('account' in quotaStateFromHeaders(labeled, h, 'm', 'T2')));
});

test('accountLabel is the digest core_limit_hold computes, and only from non-secret fields', () => {
	const acct = { accountUuid: 'u-1', organizationUuid: 'o-1', emailAddress: 'someone@example.com', planDisplayName: 'Claude Max' };
	const read = (path: string) => {
		assert.ok(path.endsWith('/cfg/.claude.json'), path);
		return JSON.stringify({ oauthAccount: acct, accessToken: 'never-read' });
	};
	const py = execFileSync('python3', ['-c', [
		'import json,sys; sys.path.insert(0, "src")',
		'import core_limit_hold as c, core_recovery_state as r',
		'print(r.account_label(c.account_digest(json.loads(sys.argv[1]))))',
	].join('\n'), JSON.stringify(acct)], { encoding: 'utf8', cwd: fileURLToPath(new URL('..', import.meta.url)) }).trim();
	assert.match(py, /^acct_[0-9a-f]{6}$/);
	assert.strictEqual(accountLabel('/x/cfg', read), py);
	assert.strictEqual(accountLabel('/x/cfg', () => JSON.stringify({ oauthAccount: { planDisplayName: 'Max' } })), null);
	assert.strictEqual(accountLabel('/x/cfg', () => '{ not json'), null);
	assert.strictEqual(accountLabel('/x/cfg', () => JSON.stringify({ oauthAccount: [] })), null);
	assert.strictEqual(accountLabel('', read), null);
});
