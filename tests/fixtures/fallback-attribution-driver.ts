/**
 * Drives the REAL credential proxy, with its REAL quota-state writers, through
 * one model-fallback rewrite, so a Python test can read the file the proxy wrote.
 *
 * An Opus request is sent while the 7d window is over the level-3 line, so the
 * proxy rewrites it to Sonnet. The upstream answers once with 200 (quota headers)
 * and then with 400s (rejections). Every record goes through quotaStateFromHeaders
 * and appendRejection — the functions production uses — into argv[2].
 *
 * Mode `handoff` replays a same-tier handoff instead: 7d over its level2 line sets
 * tier 3, then 7d resets while 5h crosses ITS level2 line, so tier 3 holds on the 5h
 * window's account. The fallback state is persisted the way production does it.
 *
 * Usage: tsx tests/fixtures/fallback-attribution-driver.ts <quota-state.json> [rejections | handoff]
 */
import { createServer, request as httpRequest, type Server } from 'node:http';
import type { request as httpsRequest } from 'node:https';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import {
	createProxyServer, quotaStateFromHeaders, appendRejection, type ProxyDeps, type RejectionRecord,
} from '../../skills/quota-tracker/scripts/credential-proxy.ts';
import { DEFAULT_FALLBACK_CONFIG, type FallbackConfig } from '../../skills/quota-tracker/scripts/quota-fallback-policy.ts';

const out = process.argv[2];
const mode = process.argv[3] === 'handoff' ? 'handoff' : 'attribution';
const rejections = mode === 'attribution' ? Number(process.argv[3] ?? '5') : 0;
const REQUESTED = 'claude-opus-5-5';
const NOW = Date.now();

const read = (): Record<string, unknown> =>
	existsSync(out) ? JSON.parse(readFileSync(out, 'utf8')) as Record<string, unknown> : {};
const write = (s: Record<string, unknown>): void => writeFileSync(out, JSON.stringify(s, null, 2));

// 7d at 99% is over the level-3 line, so every request above Sonnet is rewritten.
const reset7d = String(Math.floor(NOW / 1000) + 86_400);
const reset5h = String(Math.floor(NOW / 1000) + 3_600);
const HEADERS = {
	'anthropic-ratelimit-unified-status': 'allowed',
	'anthropic-ratelimit-unified-7d-utilization': '0.99',
	'anthropic-ratelimit-unified-7d-reset': reset7d,
	'anthropic-ratelimit-unified-5h-utilization': '0.20',
	'anthropic-ratelimit-unified-5h-reset': reset5h,
};
const CFG: FallbackConfig = {
	...DEFAULT_FALLBACK_CONFIG, projection5h: { ...DEFAULT_FALLBACK_CONFIG.projection5h, enabled: false },
};

let status = 200;
let headers: Record<string, string> = HEADERS;
const servers: Server[] = [];
const listen = (s: Server): Promise<number> => new Promise((resolve) => {
	servers.push(s);
	s.listen(0, '127.0.0.1', () => resolve((s.address() as { port: number }).port));
});

const sent: string[] = [];
const upstreamPort = await listen(createServer((req, res) => {
	const chunks: Buffer[] = [];
	req.on('data', (c) => chunks.push(c));
	req.on('end', () => {
		sent.push(JSON.parse(Buffer.concat(chunks).toString()).model);
		res.writeHead(status, { 'content-type': 'application/json', ...headers });
		res.end(status === 200 ? '{"ok":true}' : '{"error":"invalid_request_error"}');
	});
}));

let tierState: unknown = null;
const deps: Partial<ProxyDeps> = {
	readCredCandidates: () => [{ service: 'svc', oauth: { accessToken: 'tok', expiresAt: NOW + 3600_000 } }],
	writeCred: () => true,
	refreshAccessToken: async () => null,
	request: httpRequest as unknown as typeof httpsRequest,
	upstreamUrl: new URL(`http://127.0.0.1:${upstreamPort}`),
	updateQuotaState: (headers, model, effectiveModel) => {
		write(quotaStateFromHeaders(read(), headers, model ?? '', new Date().toISOString(), effectiveModel));
	},
	recordRejection: (rej: RejectionRecord) => {
		const prev = read();
		write({ ...prev, recent_rejections: appendRejection(prev.recent_rejections, rej) });
	},
	recordCredentialState: () => {},
	now: () => NOW,
	idleTimeoutMs: 5000,
	fallbackConfig: () => CFG,
	readFallbackState: () => tierState as never,
	// As production's recordFallback: the whole state under `fallback`.
	recordFallback: (s) => { tierState = s; write({ ...read(), fallback: s }); },
	notifyOwner: () => {},
	readHistorySamples: () => [],
};
const proxyPort = await listen(createProxyServer(deps));

const call = (): Promise<number> => new Promise((resolve, reject) => {
	const req = httpRequest({
		hostname: '127.0.0.1', port: proxyPort, path: '/v1/messages', method: 'POST',
		headers: { authorization: 'Bearer client' },
	}, (res) => { res.resume(); res.on('end', () => resolve(res.statusCode ?? 0)); });
	req.on('error', reject);
	req.end(JSON.stringify({ model: REQUESTED, messages: [] }));
});

if (mode === 'handoff') {
	const sec = Math.floor(NOW / 1000);
	// 7d at 96% (over its 0.95 level2 line) on a window whose reset is already
	// behind us by the time anyone reads the record.
	headers = {
		'anthropic-ratelimit-unified-status': 'allowed',
		'anthropic-ratelimit-unified-7d-utilization': '0.96', 'anthropic-ratelimit-unified-7d-reset': String(sec - 60),
		'anthropic-ratelimit-unified-5h-utilization': '0.20', 'anthropic-ratelimit-unified-5h-reset': String(sec + 1800),
	};
	await call();
	// The 7d window resets (new epoch, usage drops) as 5h crosses its 0.97 line.
	headers = {
		'anthropic-ratelimit-unified-status': 'allowed',
		'anthropic-ratelimit-unified-7d-utilization': '0.10', 'anthropic-ratelimit-unified-7d-reset': String(sec + 7 * 86_400),
		'anthropic-ratelimit-unified-5h-utilization': '0.98', 'anthropic-ratelimit-unified-5h-reset': String(sec + 3600),
	};
	await call();
	await call();
} else {
	// The first response establishes the tier; the rest are rewritten under it.
	await call();
	await call();
	status = 400;
	for (let i = 0; i < rejections; i++) await call();
}
// A rejection is written when the upstream body ends, just after the client's.
await new Promise((r) => setTimeout(r, 200));

console.log(JSON.stringify({ requested: REQUESTED, sent }));
await Promise.all(servers.map((s) => new Promise((r) => s.close(r))));
