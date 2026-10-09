/**
 * The proxy rewrites a request body's `model` to the active fallback tier's
 * model, keeps updating quota state from the response headers, records the
 * tier in quota-state.json and tells the owner once per change. Upstream,
 * keychain, clock, config and the state/notify sinks are injected.
 */
import { test, afterEach } from 'node:test';
import assert from 'node:assert';
import { createServer as createHttpServer, request as httpRequest, type Server, type IncomingMessage } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { createProxyServer, type ProxyDeps } from '../skills/quota-tracker/scripts/credential-proxy.ts';
import { DEFAULT_FALLBACK_CONFIG, type FallbackConfig, type FallbackState } from '../skills/quota-tracker/scripts/quota-fallback-policy.ts';

const NOW = 1_700_000_000_000;
const servers: Server[] = [];
afterEach(async () => {
	await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

function listen(s: Server): Promise<number> {
	servers.push(s);
	return new Promise((resolve) => s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port)));
}

interface Seen { body: string; headers: IncomingMessage['headers'] }

interface Harness {
	port: number;
	seen: Seen[];
	quotaWrites: Array<[Record<string, string>, string | undefined]>;
	recorded: FallbackState[];
	notified: string[];
	setUpstreamHeaders: (h: Record<string, string>) => void;
}

// Projection off: these cases are about the rewrite plumbing, not the 5h fit.
const CFG: FallbackConfig = { ...DEFAULT_FALLBACK_CONFIG, projection5h: { ...DEFAULT_FALLBACK_CONFIG.projection5h, enabled: false } };

async function start(cfg: FallbackConfig = CFG, initial: FallbackState | null = null): Promise<Harness> {
	let upstreamHeaders: Record<string, string> = {};
	const seen: Seen[] = [];
	const upstreamPort = await listen(createHttpServer((req, res) => {
		const chunks: Buffer[] = [];
		req.on('data', (c) => chunks.push(c));
		req.on('end', () => {
			seen.push({ body: Buffer.concat(chunks).toString(), headers: req.headers });
			res.writeHead(200, { 'content-type': 'application/json', ...upstreamHeaders });
			res.end('{"ok":true}');
		});
	}));
	const h: Harness = {
		port: 0, seen, quotaWrites: [], recorded: [], notified: [],
		setUpstreamHeaders: (x) => { upstreamHeaders = x; },
	};
	const deps: Partial<ProxyDeps> = {
		readCredCandidates: () => [{ service: 'svc', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
		writeCred: () => true,
		refreshAccessToken: async () => null,
		request: httpRequest as unknown as typeof httpsRequest,
		upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
		updateQuotaState: (headers, model) => { h.quotaWrites.push([headers, model]); },
		recordRejection: () => {},
		recordCredentialState: () => {},
		now: () => NOW,
		idleTimeoutMs: 5000,
		fallbackConfig: () => cfg,
		readFallbackState: () => initial,
		recordFallback: (s) => { h.recorded.push(s); },
		notifyOwner: (line) => { h.notified.push(line); },
		readHistorySamples: () => [],
	};
	h.port = await listen(createProxyServer(deps));
	return h;
}

function call(port: number, body: string, headers: Record<string, string> = {}): Promise<number> {
	return new Promise((resolve, reject) => {
		const req = httpRequest({ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST', headers: { authorization: 'Bearer client', ...headers } }, (res) => {
			res.resume();
			res.on('end', () => resolve(res.statusCode ?? 0));
		});
		req.on('error', reject);
		req.end(body);
	});
}

const H7 = (u: string) => ({
	'anthropic-ratelimit-unified-status': 'allowed',
	'anthropic-ratelimit-unified-7d-utilization': u,
	'anthropic-ratelimit-unified-7d-reset': '1700600000',
	'anthropic-ratelimit-unified-5h-utilization': '0.20',
	'anthropic-ratelimit-unified-5h-reset': '1700018000',
});

test('at 0.50 the request goes upstream unchanged; at 0.86 the next Fable request is rewritten to the level-2 model and content-length follows', async () => {
	const h = await start();
	const fable = '{"model":"claude-fable-5-1[1m]","messages":[{"role":"user","content":"hi"}]}';
	h.setUpstreamHeaders(H7('0.50'));
	assert.strictEqual(await call(h.port, fable), 200);
	assert.strictEqual(JSON.parse(h.seen[0].body).model, 'claude-fable-5-1[1m]', 'below every line: untouched');
	assert.strictEqual(h.recorded.length, 1, 'the first observation records the (primary) tier once');
	assert.strictEqual(h.recorded[0].tier, 1);
	assert.deepStrictEqual(h.notified, [], 'primary from the start: nothing to tell the owner');

	h.setUpstreamHeaders(H7('0.86'));
	assert.strictEqual(await call(h.port, fable), 200);
	assert.strictEqual(JSON.parse(h.seen[1].body).model, 'claude-fable-5-1[1m]', 'the response that crossed the line was already sent');
	assert.strictEqual(h.recorded.at(-1)?.tier, 2);
	assert.strictEqual(h.notified.length, 1);
	assert.match(h.notified[0], /7 天窗口 86% 超过 85% 阈值/);

	assert.strictEqual(await call(h.port, fable), 200);
	const sent = h.seen[2];
	assert.strictEqual(JSON.parse(sent.body).model, 'claude-opus-5-5[1m]', 'rewritten to the tier-2 model, variant kept');
	assert.deepStrictEqual(JSON.parse(sent.body).messages, [{ role: 'user', content: 'hi' }]);
	assert.strictEqual(Number(sent.headers['content-length']), Buffer.byteLength(sent.body), 'content-length matches the rewritten body');
	assert.strictEqual(h.quotaWrites.at(-1)?.[1], 'claude-opus-5-5[1m]', 'the quota stamp names the model that consumed quota');
	assert.strictEqual(h.quotaWrites.at(-1)?.[0]['anthropic-ratelimit-unified-7d-utilization'], '0.86', 'response headers still update state');
	assert.strictEqual(h.notified.length, 1, 'one DM per tier change, not per request');

	// Opus is already at the tier: never touched. Sonnet is below it: never raised.
	await call(h.port, '{"model":"claude-opus-5-5","messages":[]}');
	assert.strictEqual(JSON.parse(h.seen[3].body).model, 'claude-opus-5-5');
	await call(h.port, '{"model":"claude-sonnet-5","messages":[]}');
	assert.strictEqual(JSON.parse(h.seen[4].body).model, 'claude-sonnet-5');
});

test('above the 7d level2 line every Claude request above Sonnet runs on the level-3 model', async () => {
	const h = await start();
	h.setUpstreamHeaders(H7('0.96'));
	await call(h.port, '{"model":"claude-opus-5-5","messages":[]}');
	await call(h.port, '{"model":"claude-opus-5-5","messages":[]}');
	await call(h.port, '{"model":"claude-fable-5-1","messages":[]}');
	assert.strictEqual(JSON.parse(h.seen[1].body).model, 'claude-sonnet-5');
	assert.strictEqual(JSON.parse(h.seen[2].body).model, 'claude-sonnet-5');
	assert.match(h.notified[0], /已切到 claude-sonnet-5 兜底/);
});

test('rejected: no model is swapped, the Codex runtime-switch request is recorded, and the owner is told once', async () => {
	const h = await start();
	h.setUpstreamHeaders({ ...H7('0.99'), 'anthropic-ratelimit-unified-status': 'rejected' });
	await call(h.port, '{"model":"claude-fable-5-1","messages":[]}');
	await call(h.port, '{"model":"claude-fable-5-1","messages":[]}');
	assert.strictEqual(JSON.parse(h.seen[1].body).model, 'claude-fable-5-1', 'all Claude models share the rejected quota');
	assert.strictEqual(h.recorded.at(-1)?.runtime_switch?.to, 'codex');
	assert.strictEqual(h.recorded.at(-1)?.tier, 1);
	assert.strictEqual(h.notified.length, 1);
	assert.match(h.notified[0], /Codex/);
});

test('a persisted tier survives a proxy restart, the priority header is consumed by the proxy, and the low ladder is off by default', async () => {
	const seeded = await start();
	seeded.setUpstreamHeaders(H7('0.86'));
	await call(seeded.port, '{"model":"claude-opus-5-5","messages":[]}');
	const persisted = seeded.recorded.at(-1)!;
	assert.strictEqual(persisted.tier, 2);

	const h = await start(CFG, persisted);
	h.setUpstreamHeaders(H7('0.84'));
	await call(h.port, '{"model":"claude-fable-5-1","messages":[]}', { 'x-sutando-priority': 'low' });
	assert.strictEqual(JSON.parse(h.seen[0].body).model, 'claude-opus-5-5', 'tier 2 held across the restart (0.84 is inside the hysteresis band)');
	assert.strictEqual(h.seen[0].headers['x-sutando-priority'], undefined, 'our marker never travels upstream');
	assert.deepStrictEqual(h.notified, [], 'no transition: no DM');

	const low = await start({ ...CFG, lowPriorityEnabled: true });
	low.setUpstreamHeaders(H7('0.61'));
	await call(low.port, '{"model":"claude-fable-5-1","messages":[]}');
	await call(low.port, '{"model":"claude-fable-5-1","messages":[]}', { 'x-sutando-priority': 'low' });
	await call(low.port, '{"model":"claude-fable-5-1","messages":[]}');
	assert.strictEqual(JSON.parse(low.seen[1].body).model, 'claude-opus-5-5', 'low priority demotes from 0.60 when the ladder is on');
	assert.strictEqual(JSON.parse(low.seen[2].body).model, 'claude-fable-5-1', 'normal traffic is untouched at 0.61');
});
