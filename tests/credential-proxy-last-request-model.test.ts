/**
 * The proxy is the one component that sees which model is consuming quota,
 * on every request. It stamps that model into quota-state.json beside the
 * rate-limit headers so the dashboard can show "the model in use" from what
 * was observed on the wire — not from a launch-time marker that a /model
 * switch leaves stale. A request naming no model carries the previous stamp.
 */
import { test, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { createProxyServer, withLastRequest, type ProxyDeps } from '../skills/quota-tracker/scripts/credential-proxy.ts';

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

async function startProxy(seen: Array<[Record<string, string>, string | undefined]>): Promise<number> {
	const upstreamPort = await listen(createHttpServer((req, res) => upstreamHandler(req, res)));
	const deps: Partial<ProxyDeps> = {
		readCredCandidates: () => [{ service: 'svc', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: (headers, model) => { seen.push([headers, model]); },
		recordRejection: () => {},
		now: () => NOW,
		idleTimeoutMs: 5000,
	};
	return listen(createProxyServer(deps));
}

function call(port: number, reqBody: string): Promise<number> {
	return new Promise((resolve, reject) => {
		const req = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST' }, (res) => {
			res.resume();
			res.on('end', () => resolve(res.statusCode ?? 0));
		});
		req.on('error', reject);
		req.end(reqBody);
	});
}

test('the request model travels with the rate-limit headers into the quota-state write', async () => {
	upstreamHandler = (_req, res) => {
		res.writeHead(200, { 'anthropic-ratelimit-unified-5h-utilization': '0.13' });
		res.end('{}');
	};
	const seen: Array<[Record<string, string>, string | undefined]> = [];
	const port = await startProxy(seen);
	assert.strictEqual(await call(port, '{"model":"claude-fable-5-1","messages":[]}'), 200);
	assert.strictEqual(seen.length, 1, 'one quota write for one response carrying rate-limit headers');
	assert.strictEqual(seen[0][0]['anthropic-ratelimit-unified-5h-utilization'], '0.13');
	assert.strictEqual(seen[0][1], 'claude-fable-5-1');
});

test('a request naming no model passes "" — the writer carries the previous stamp, not the reader', async () => {
	upstreamHandler = (_req, res) => {
		res.writeHead(200, { 'anthropic-ratelimit-unified-7d-utilization': '0.67' });
		res.end('{}');
	};
	const seen: Array<[Record<string, string>, string | undefined]> = [];
	const port = await startProxy(seen);
	await call(port, '{"messages":[]}');
	assert.strictEqual(seen[0][1], '');
});

test('withLastRequest: a named model stamps; an empty one carries a well-formed previous stamp only', () => {
	assert.deepStrictEqual(withLastRequest({}, 'claude-opus-5', 'T1'), { model: 'claude-opus-5', at: 'T1' });
	const prev = { last_request: { model: 'claude-fable-5-1', at: 'T0' } };
	assert.deepStrictEqual(withLastRequest(prev, '', 'T1'), { model: 'claude-fable-5-1', at: 'T0' });
	assert.deepStrictEqual(withLastRequest(prev, 'claude-opus-5', 'T1'), { model: 'claude-opus-5', at: 'T1' });
	assert.strictEqual(withLastRequest({}, '', 'T1'), undefined);
	assert.strictEqual(withLastRequest({ last_request: { model: '', at: 'T0' } }, '', 'T1'), undefined);
	assert.strictEqual(withLastRequest({ last_request: 'claude-x' }, '', 'T1'), undefined);
	assert.strictEqual(withLastRequest(null, '', 'T1'), undefined);
});
