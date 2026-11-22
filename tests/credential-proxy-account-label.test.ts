/**
 * A quota reading names the account whose credential made its request, sampled when the request
 * is sent: a response that lands after an account switch must not read as the new account's.
 * runtime_profile.gather() trusts a reading only when this label is the signed-in account's.
 */
import { test, afterEach } from 'node:test';
import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { accountLabel, createProxyServer, quotaStateFromHeaders, type ProxyDeps } from '../skills/quota-tracker/scripts/credential-proxy.ts';

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

async function startProxy(seen: Seen, current: { account: string | null }): Promise<number> {
	const upstreamPort = await listen(createHttpServer((req, res) => upstreamHandler(req, res)));
	const deps: Partial<ProxyDeps> = {
		readCredCandidates: () => [{ service: 'svc', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: (_headers, model, account) => { seen.push({ model, account }); },
		readAccount: () => current.account,
		recordRejection: () => {},
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

test('a response that lands after an account switch keeps the account its request was sent with', async () => {
	const current = { account: 'acct_aaaaaa' as string | null };
	upstreamHandler = (_req, res) => {
		current.account = 'acct_bbbbbb'; // the seat switches while A's request is in flight
		res.writeHead(200, { 'anthropic-ratelimit-unified-7d-utilization': '0.9' });
		res.end('{}');
	};
	const seen: Seen = [];
	const port = await startProxy(seen, current);
	assert.strictEqual(await call(port), 200);
	assert.deepStrictEqual(seen, [{ model: 'claude-opus-5-5', account: 'acct_aaaaaa' }]);
	assert.strictEqual(await call(port), 200);
	assert.strictEqual(seen[1].account, 'acct_bbbbbb', 'a request sent after the switch is B\'s');
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
