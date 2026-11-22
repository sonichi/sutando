/**
 * Hermetic tests for the credential proxy's per-request timing line: one line per
 * request on completion, upstream error, idle abort or client close, carrying a
 * monotonic id, socket reuse and durations, and never a query string or header.
 */
import { test, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, Agent, type Server, type IncomingMessage, type ServerResponse, type RequestOptions } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { createProxyServer, formatTimingLine, type ProxyDeps } from '../skills/quota-tracker/scripts/credential-proxy.ts';

const servers: Server[] = [];
const agents: Agent[] = [];
const held: ServerResponse[] = [];

afterEach(async () => {
	for (const r of held.splice(0)) r.destroy();
	for (const a of agents.splice(0)) a.destroy();
	await Promise.all(servers.splice(0).map((s) => {
		s.closeAllConnections();
		return new Promise((r) => s.close(r));
	}));
});

function listen(s: Server): Promise<number> {
	servers.push(s);
	return new Promise((resolve) =>
		s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port)));
}

function startUpstream(handler: (req: IncomingMessage, res: ServerResponse) => void): Promise<number> {
	return listen(createHttpServer(handler));
}

// Upstream requests go through a dedicated keep-alive agent so socket reuse is deterministic.
async function startProxy(upstreamPort: number, lines: string[], extra: Partial<ProxyDeps> = {}): Promise<number> {
	const agent = new Agent({ keepAlive: true, maxSockets: 1 });
	agents.push(agent);
	const request = ((opts: RequestOptions, cb: (r: IncomingMessage) => void) =>
		httpRequest({ ...opts, agent }, cb)) as unknown as typeof httpsRequest;
	return listen(createProxyServer({
		readCredCandidates: () => [{ service: 's', oauth: { accessToken: 'stored-secret-token-zzzz', expiresAt: Date.now() + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: () => {},
		recordRejection: () => {},
		recordCredentialState: () => {},
		idleTimeoutMs: 5000,
		logTiming: (line) => { lines.push(line); },
		...extra,
	}));
}

function call(port: number, path = '/v1/messages?beta=true'): Promise<{ status: number; body: string }> {
	return new Promise((resolve, reject) => {
		const req = httpRequest(
			{ hostname: '127.0.0.1', port, path, method: 'POST', headers: { authorization: 'Bearer client-token-xyz' }, agent: false },
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

function field(line: string, key: string): string {
	const m = line.match(new RegExp(`(?:^|\\s)${key}=(\\S+)`));
	assert.ok(m, `missing ${key} in: ${line}`);
	return m[1];
}

async function waitFor(pred: () => boolean, ms = 3000): Promise<void> {
	const end = Date.now() + ms;
	while (!pred()) {
		if (Date.now() > end) throw new Error('timed out waiting');
		await new Promise((r) => setTimeout(r, 10));
	}
}

function captureConsoleError(): { lines: string[]; restore: () => void } {
	const lines: string[] = [];
	const orig = console.error;
	console.error = (...args: unknown[]) => { lines.push(args.map(String).join(' ')); };
	return { lines, restore: () => { console.error = orig; } };
}

test('a completed request logs one ok line: method, path without query, status, durations, new then reused socket', async () => {
	const lines: string[] = [];
	const up = await startUpstream((_req, res) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"ok":true}'); });
	const port = await startProxy(up, lines);

	assert.equal((await call(port)).status, 200);
	assert.equal((await call(port)).status, 200);
	await waitFor(() => lines.length === 2);

	const [first, second] = lines;
	for (const l of lines) {
		assert.equal(field(l, 'outcome'), 'ok');
		assert.equal(field(l, 'status'), '200');
		assert.equal(field(l, 'method'), 'POST');
		assert.equal(field(l, 'path'), '/v1/messages');
		for (const k of ['dispatch_ms', 'socket_ms', 'ttfb_ms', 'total_ms']) assert.match(field(l, k), /^\d+$/);
		assert.ok(!l.includes('beta'), 'query string must not be logged');
		assert.ok(!/token|Bearer|authorization|"ok"/i.test(l), `no credential, header or body in: ${l}`);
	}
	assert.equal(field(first, 'reused'), 'false');
	assert.equal(field(second, 'reused'), 'true');
	assert.equal(Number(field(second, 'id')), Number(field(first, 'id')) + 1);
});

test('client closing before the response logs client_closed_before_response, and the upstream error line carries the same id', async () => {
	const lines: string[] = [];
	let upstreamGotIt = false;
	const up = await startUpstream((_req, res) => { upstreamGotIt = true; held.push(res); });
	const port = await startProxy(up, lines);
	const err = captureConsoleError();
	try {
		const req = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/messages/count_tokens?beta=true', method: 'POST', agent: false, headers: { authorization: 'Bearer c' } });
		req.on('error', () => {});
		req.end('{}');
		await waitFor(() => upstreamGotIt);
		req.destroy();
		await waitFor(() => lines.length === 1 && err.lines.some((l) => l.includes('Upstream error')));
	} finally { err.restore(); }

	const l = lines[0];
	assert.equal(field(l, 'outcome'), 'client_closed_before_response');
	assert.equal(field(l, 'path'), '/v1/messages/count_tokens');
	assert.equal(field(l, 'status'), '-');
	assert.equal(field(l, 'ttfb_ms'), '-');
	assert.match(field(l, 'socket_ms'), /^\d+$/);
	assert.match(field(l, 'total_ms'), /^\d+$/);
	const id = field(l, 'id');
	assert.ok(err.lines.some((e) => e.includes(`[Proxy] id=${id} Upstream error`)), err.lines.join('\n'));
	assert.equal(lines.length, 1, 'the later upstream error must not log a second timing line');
});

test('an idle upstream logs idle_abort with status 504, and the idle line carries the id', async () => {
	const lines: string[] = [];
	const up = await startUpstream((_req, res) => { held.push(res); });
	const port = await startProxy(up, lines, { idleTimeoutMs: 100 });
	const err = captureConsoleError();
	let r: { status: number };
	try { r = await call(port); } finally { err.restore(); }
	assert.equal(r.status, 504);
	await waitFor(() => lines.length === 1);
	assert.equal(field(lines[0], 'outcome'), 'idle_abort');
	assert.equal(field(lines[0], 'status'), '504');
	const id = field(lines[0], 'id');
	assert.ok(err.lines.some((e) => e.includes(`[Proxy] id=${id} Upstream idle >100ms`)), err.lines.join('\n'));
});

test('an unreachable upstream logs upstream_error with status 502', async () => {
	const lines: string[] = [];
	const closed = createHttpServer();
	const deadPort = await new Promise<number>((resolve) => closed.listen(0, '127.0.0.1', () => {
		const p = (closed.address() as { port: number }).port;
		closed.close(() => resolve(p));
	}));
	const port = await startProxy(deadPort, lines);
	const err = captureConsoleError();
	let r: { status: number };
	try { r = await call(port); } finally { err.restore(); }
	assert.equal(r.status, 502);
	await waitFor(() => lines.length === 1);
	assert.equal(field(lines[0], 'outcome'), 'upstream_error');
	assert.equal(field(lines[0], 'status'), '502');
	assert.equal(field(lines[0], 'ttfb_ms'), '-');
});

test('client closing mid-stream logs client_closed_during_response with the upstream status', async () => {
	const lines: string[] = [];
	const up = await startUpstream((_req, res) => { res.writeHead(200, { 'content-type': 'text/event-stream' }); res.write('data: 1\n\n'); held.push(res); });
	const port = await startProxy(up, lines);
	await new Promise<void>((resolve) => {
		const req = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST', agent: false, headers: { authorization: 'Bearer c' } }, (res) => {
			res.once('data', () => { req.destroy(); resolve(); });
		});
		req.on('error', () => {});
		req.end('{}');
	});
	await waitFor(() => lines.length === 1);
	assert.equal(field(lines[0], 'outcome'), 'client_closed_during_response');
	assert.equal(field(lines[0], 'status'), '200');
	assert.match(field(lines[0], 'ttfb_ms'), /^\d+$/);
});

test('formatTimingLine drops the query string and renders missing durations as -', () => {
	const l = formatTimingLine({ id: 7, method: 'GET', path: '/v1/models?key=secret', attempt: 0, totalMs: 12.6, status: 0, outcome: 'client_closed_before_response' });
	assert.equal(l, '[Proxy] id=7 timing method=GET path=/v1/models attempt=0 dispatch_ms=- socket_ms=- reused=- ttfb_ms=- total_ms=13 status=- outcome=client_closed_before_response');
});
