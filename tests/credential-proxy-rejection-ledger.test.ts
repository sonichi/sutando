/**
 * The proxy must RECORD an upstream rejection (4xx/5xx other than the handled
 * 401) while forwarding it unchanged — the only place a credits/overage
 * rejection is visible is this response, and before #3790 it was dropped.
 * Upstream, keychain and clock are injected; no network, no real keychain.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { createProxyServer, appendRejection, requestModel, decodeRejectionBody, MAX_RECENT_REJECTIONS, type ProxyDeps, type RejectionRecord } from '../skills/quota-tracker/scripts/credential-proxy.ts';
import { gzipSync, brotliCompressSync, zstdCompressSync } from 'node:zlib';

const NOW = 1_700_000_000_000;
let upstreamHandler: (req: IncomingMessage, res: ServerResponse) => void = () => {};
let recorded: RejectionRecord[] = [];
let quotaWrites = 0;
const servers: Server[] = [];

beforeEach(() => { recorded = []; quotaWrites = 0; });
afterEach(async () => {
	await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

function listen(s: Server): Promise<number> {
	servers.push(s);
	return new Promise((resolve) => s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port)));
}

async function startProxy(): Promise<number> {
	const upstreamPort = await listen(createHttpServer((req, res) => upstreamHandler(req, res)));
	const deps: Partial<ProxyDeps> = {
		readCredCandidates: () => [{ service: 'svc', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: () => { quotaWrites += 1; },
		recordRejection: (rej) => { recorded.push(rej); },
		now: () => NOW,
		idleTimeoutMs: 5000,
	};
	return listen(createProxyServer(deps));
}

function call(port: number, reqBody = '{}', headers: Record<string, string> = {}): Promise<{ status: number; body: string }> {
	return new Promise((resolve, reject) => {
		const req = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/messages?beta=true', method: 'POST', headers }, (res) => {
			const chunks: Buffer[] = [];
			res.on('data', (c) => chunks.push(c));
			res.on('end', () => resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString() }));
		});
		req.on('error', reject);
		req.end(reqBody);
	});
}

const settle = () => new Promise((r) => setTimeout(r, 30));

test('a 429 credits rejection is forwarded unchanged AND recorded with status, path and body snippet', async () => {
	const body = '{"error":{"type":"overage","message":"You\'re out of usage credits. Run /usage-credits"}}';
	upstreamHandler = (_req, res) => { res.writeHead(429, { 'content-type': 'application/json' }); res.end(body); };
	const port = await startProxy();
	const r = await call(port);
	await settle();
	assert.equal(r.status, 429);
	assert.equal(r.body, body, 'the client still receives the whole upstream body');
	assert.equal(recorded.length, 1);
	assert.equal(recorded[0].status, 429);
	assert.equal(recorded[0].path, '/v1/messages?beta=true');
	assert.equal(recorded[0].ts, new Date(NOW).toISOString(), 'stamped from the injected clock');
	assert.match(recorded[0].snippet, /out of usage credits/);
});

test('a COMPRESSED rejection body is decoded before it is recorded', async () => {
	// The upstream compresses whatever the client's accept-encoding asked for, so an
	// uncompressed fixture cannot see this: every real snippet on a live host was mojibake.
	const body = '{"error":{"type":"invalid_request_error","message":"credit balance is too low"}}';
	for (const [enc, compress] of [['gzip', gzipSync], ['br', brotliCompressSync], ['zstd', zstdCompressSync]] as const) {
		recorded = [];
		upstreamHandler = (_req, res) => {
			res.writeHead(400, { 'content-type': 'application/json', 'content-encoding': enc });
			res.end(compress(Buffer.from(body)));
		};
		const port = await startProxy();
		await call(port);
		await settle();
		assert.equal(recorded.length, 1, enc);
		assert.match(recorded[0].snippet, /credit balance is too low/, `${enc} body must be readable`);
		assert.equal(recorded[0].content_encoding, enc, `${enc} must be recorded`);
	}
});

test('an undecodable body says so instead of emitting mojibake', () => {
	const garbage = Buffer.from([0x1f, 0x8b, 0x08, 0x00, 0xff, 0xfe]);   // gzip header, truncated
	assert.match(decodeRejectionBody(garbage, 'gzip'), /^<undecodable gzip body: 6 bytes>$/);
	assert.match(decodeRejectionBody(garbage, 'weird-codec'), /content-encoding: weird-codec/);
	assert.equal(decodeRejectionBody(Buffer.from('plain'), ''), 'plain', 'no encoding is still plain text');
	assert.equal(decodeRejectionBody(Buffer.from('plain'), 'identity'), 'plain');
});

test('the record attributes the rejection to the requesting client: model from the body, user-agent, peer port', async () => {
	upstreamHandler = (_req, res) => { res.writeHead(429); res.end('{"error":"credits"}'); };
	const port = await startProxy();
	await call(port, '{"model":"claude-fable-5-1","messages":[]}', { 'user-agent': 'claude-cli/9.9.9 (seat-3)' });
	await call(port, 'not json', { 'user-agent': 'other-client/1' });
	await settle();
	assert.equal(recorded.length, 2);
	assert.equal(recorded[0].model, 'claude-fable-5-1');
	assert.equal(recorded[0].user_agent, 'claude-cli/9.9.9 (seat-3)');
	assert.equal(typeof recorded[0].peer_port, 'number');
	assert.equal(recorded[1].model, '', 'an unparsable body records an empty model, never throws');
	assert.equal(recorded[1].user_agent, 'other-client/1');
	assert.equal(requestModel(Buffer.from('{"model":5}')), '', 'a non-string model is empty');
});

test('a 5xx is recorded too; a 2xx and a 401 are NOT (401 has its own auth path)', async () => {
	upstreamHandler = (_req, res) => { res.writeHead(529); res.end('overloaded'); };
	const port = await startProxy();
	assert.equal((await call(port)).status, 529);
	await settle();
	assert.equal(recorded.length, 1);
	assert.equal(recorded[0].status, 529);

	upstreamHandler = (_req, res) => { res.writeHead(200, { 'anthropic-ratelimit-unified-status': 'allowed' }); res.end('{"ok":true}'); };
	assert.equal((await call(port)).status, 200);
	upstreamHandler = (_req, res) => { res.writeHead(401); res.end('{"error":"auth"}'); };
	assert.equal((await call(port)).status, 401);
	await settle();
	assert.equal(recorded.length, 1, 'control: neither the 200 nor the 401 added a record');
	assert.ok(quotaWrites >= 1, 'the 200 still fed the quota-state writer');
});

test('a recorder that throws does not break forwarding', async () => {
	upstreamHandler = (_req, res) => { res.writeHead(429); res.end('x'); };
	const upstreamPort = await listen(createHttpServer((req, res) => upstreamHandler(req, res)));
	const port = await listen(createProxyServer({
		readCredCandidates: () => [{ service: 'svc', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: () => {},
		recordRejection: () => { throw new Error('disk full'); },
		now: () => NOW,
		idleTimeoutMs: 5000,
	}));
	const r = await call(port);
	assert.equal(r.status, 429);
	assert.equal(r.body, 'x');
});

test('appendRejection bounds the ledger, drops foreign entries, keeps the newest', () => {
	const mk = (i: number): RejectionRecord => ({ ts: new Date(NOW + i).toISOString(), status: 429, path: '/', snippet: String(i) });
	let ledger: unknown = ['garbage', { ts: 5 }, null];
	for (let i = 0; i < MAX_RECENT_REJECTIONS + 7; i++) ledger = appendRejection(ledger, mk(i));
	const out = ledger as RejectionRecord[];
	assert.equal(out.length, MAX_RECENT_REJECTIONS);
	assert.equal(out[0].snippet, '7', 'oldest 7 evicted');
	assert.equal(out[out.length - 1].snippet, String(MAX_RECENT_REJECTIONS + 6));
	assert.deepEqual(appendRejection(undefined, mk(0)), [mk(0)], 'a missing ledger starts fresh');
	assert.deepEqual(appendRejection('not-a-list', mk(0)), [mk(0)]);
});
